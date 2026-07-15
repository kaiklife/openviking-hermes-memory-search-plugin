"""hermes-memory-search: 自动从 OpenViking 拉取相关记忆 + 重启无感感知 + 自动存记忆。

在每轮 LLM 调用前自动搜索 OpenViking 中与当前对话相关的记忆，
以 context 形式注入系统提示。每轮 LLM 调用后自动保存关键信息到 OpenViking。

重启感知：插件通过 boot.json 标记文件自动检测进程重启。
检测到重启后，从 state.db 查询上一个会话的内容，自动注入摘要。
无需手动保存状态，无需额外步骤，完全无感。
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 确认性短消息——搜了也白搜，跳过
_SKIP_MESSAGES = frozenset({
    "好的", "嗯", "好", "ok", "OK", "Ok", "是的", "对", "行",
    "可以", "嗯嗯", "好的好的", "收到", "了解", "搞", "要搞",
    "好的，", "好嘞", "好的没问题", "没问题没问题", "好嘞好嘞",
    "明白", "知道了", "收到收到",
})

OPENVIKING_URL = "http://127.0.0.1:1933"
SEARCH_ENDPOINT = f"{OPENVIKING_URL}/api/v1/search/search"
CONTENT_WRITE_ENDPOINT = f"{OPENVIKING_URL}/api/v1/content/write"
SEARCH_HEADERS = {
    "X-OpenViking-Account": "fanwenkai",
    "X-OpenViking-User": "fanwenkai",
    "Content-Type": "application/json",
}

SCORE_THRESHOLD = 0.3
MAX_RESULTS_FIRST_TURN = 5
MAX_RESULTS_SUBSEQUENT = 2
MAX_QUERY_LENGTH = 200
ABSTRACT_MAX_LENGTH = 150
REQUEST_TIMEOUT = 5
MIN_MSG_LENGTH_FIRST = 2
MIN_MSG_LENGTH_SUBSEQUENT = 5

# 自动存记忆的最小内容长度 + 冷却时间
_AUTO_SAVE_MIN_LENGTH = 30
_AUTO_SAVE_COOLDOWN = 120  # 同一 session 两次保存最短间隔（秒）
_AUTO_SAVE_LAST_SAVED: dict[str, float] = {}  # session_id -> 上次保存时间

HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
STATE_DB = HERMES_HOME / "state.db"

# OpenViking 写操作
_OV_API_KEY = "ov-root-key-2026"
_WRITE_HEADERS = {
    "X-OpenViking-Account": "fanwenkai",
    "X-OpenViking-User": "fanwenkai",
    "X-API-Key": _OV_API_KEY,
    "Content-Type": "application/json",
}


# ─── 记忆搜索（pre_llm_call） ───────────────────────────────


def register(ctx) -> None:
    """Register the hermes-memory-search plugin with the Hermes runtime."""
    ctx.register_hook("pre_llm_call", inject_relevant_memories)
    ctx.register_hook("post_llm_call", auto_save_memories)


def _should_search(user_message: str, is_first_turn: bool) -> bool:
    """判断是否需要搜索——跳过短消息和确认性回复。"""
    msg = user_message.strip()
    if not msg:
        return False
    if msg in _SKIP_MESSAGES:
        return False
    min_len = MIN_MSG_LENGTH_FIRST if is_first_turn else MIN_MSG_LENGTH_SUBSEQUENT
    if len(msg) < min_len:
        return False
    return True


def _search_openviking(query: str, limit: int) -> list[dict]:
    """Search OpenViking and return relevant memory items."""
    import urllib.request

    safe_limit = max(1, limit)

    payload = json.dumps({
        "query": query[:MAX_QUERY_LENGTH],
        "limit": safe_limit * 2,  # 多取一些用于去重
        "score_threshold": SCORE_THRESHOLD,
    }).encode("utf-8")

    req = urllib.request.Request(
        SEARCH_ENDPOINT,
        data=payload,
        headers=SEARCH_HEADERS,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("OpenViking search failed: %s", e)
        return []

    if body.get("status") != "ok":
        return []

    result = body.get("result", {})
    memories = result.get("memories", [])
    if not memories:
        return []

    # 二次过滤：score 必须有且 >= 阈值，score=None 直接排除
    filtered = [
        m for m in memories
        if m.get("score") is not None and m["score"] >= SCORE_THRESHOLD
    ]

    # 去重：按 abstract 文本去重（相同内容只保留一次）
    seen_abstracts: set[str] = set()
    deduped = []
    for m in filtered:
        abstract = (m.get("abstract") or "").strip()
        if abstract and abstract not in seen_abstracts:
            seen_abstracts.add(abstract)
            deduped.append(m)

    return deduped[:safe_limit]


def _build_memory_context(items: list[dict], exclude_session_id: str = "") -> str | None:
    """Format search results into a compact context string.

    过滤掉 hermes-auto-memories 路径下当前 session 自己存的内容（自反馈循环防护）。
    """
    lines = []
    for item in items:
        abstract = (item.get("abstract") or "").strip()
        ctype = item.get("context_type", "memory")
        uri = item.get("uri", "")

        # 自反馈过滤：跳过 auto-memories 中当前 session 的内容
        if exclude_session_id and "hermes-auto-memories" in uri:
            sid_prefix = exclude_session_id[:12]
            if sid_prefix in abstract or sid_prefix in uri:
                logger.debug(
                    "Skipping auto-saved memory from current session %s",
                    sid_prefix,
                )
                continue

        if abstract:
            label = ctype.capitalize()
            lines.append(f"• [{label}] {abstract[:ABSTRACT_MAX_LENGTH]}")

    if not lines:
        return None

    return (
        "📌 **相关记忆（自动从 OpenViking 检索）**\n"
        + "\n".join(lines)
    )


# ─── 自动存记忆（post_llm_call） ────────────────────────────


def auto_save_memories(
    session_id: str,
    model: str = "",
    platform: str = "",
    **kwargs,
) -> None:
    """LLM 调用后自动保存关键对话内容到 OpenViking。

    读取 state.db 中当前会话的最新一条用户消息和助手回复，
    如果内容有意义，写入 OpenViking content API。
    包含冷却窗口、去重、SQLite 连接安全等防护。
    """
    if not STATE_DB.exists():
        return

    # 冷却检查：同一 session 不要太频繁
    now = time.time()
    last_saved = _AUTO_SAVE_LAST_SAVED.get(session_id, 0.0)
    if now - last_saved < _AUTO_SAVE_COOLDOWN:
        return

    import sqlite3

    db = None
    try:
        db = sqlite3.connect(str(STATE_DB), timeout=3.0)
        db.row_factory = sqlite3.Row

        # 取最后一条 user 消息 + 最后一条 assistant 消息
        last_user = db.execute(
            """SELECT content, timestamp FROM messages
               WHERE session_id = ? AND role = 'user'
               ORDER BY timestamp DESC LIMIT 1""",
            (session_id,),
        ).fetchone()

        last_assistant = db.execute(
            """SELECT content, timestamp FROM messages
               WHERE session_id = ? AND role = 'assistant'
               ORDER BY timestamp DESC LIMIT 1""",
            (session_id,),
        ).fetchone()

    except Exception as e:
        logger.warning("auto_save[%s] state.db query failed: %s", session_id[:12], e)
        return
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass

    if not last_user or not last_assistant:
        return

    user_text = (last_user["content"] or "").strip()
    assistant_text = (last_assistant["content"] or "").strip()

    # 跳过短消息和确认性回复
    if len(user_text) < _AUTO_SAVE_MIN_LENGTH:
        return
    if user_text in _SKIP_MESSAGES:
        return
    if not assistant_text or len(assistant_text) < 10:
        return

    # 构造概要
    user_summary = user_text[:80].replace("\n", " ")
    assistant_summary = assistant_text[:120].replace("\n", " ")
    if len(assistant_text) > 120:
        assistant_summary += "..."

    today = datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    content = (
        f"## 对话摘要\n\n"
        f"- 时间: {now_str}\n"
        f"- 会话: {session_id[:12]}\n"
        f"- 模型: {model}\n"
        f"- 用户: {user_summary}\n"
        f"- 助手: {assistant_summary}\n\n"
    )

    # 用日期做文件名，append 模式累积
    uri = f"viking://resources/hermes-auto-memories/{today}.md"
    payload = json.dumps({
        "uri": uri,
        "content": content,
        "mode": "append",
    }).encode("utf-8")

    req = urllib.request.Request(
        CONTENT_WRITE_ENDPOINT,
        data=payload,
        headers=_WRITE_HEADERS,
        method="POST",
    )

    try:
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if body.get("status") == "ok":
                    _AUTO_SAVE_LAST_SAVED[session_id] = time.time()
                    return
                err_code = body.get("error", {}).get("code", "")
                logger.warning(
                    "auto_save[%s] append failed: %s",
                    session_id[:12], err_code,
                )
                return
        except urllib.error.HTTPError as he:
            if he.code == 404:
                # 404 = 文件不存在 -> fallback to create
                payload_create = json.dumps({
                    "uri": uri,
                    "content": content,
                    "mode": "create",
                }).encode("utf-8")
                req_create = urllib.request.Request(
                    CONTENT_WRITE_ENDPOINT,
                    data=payload_create,
                    headers=_WRITE_HEADERS,
                    method="POST",
                )
                with urllib.request.urlopen(req_create, timeout=10) as cr:
                    create_body = json.loads(cr.read().decode("utf-8"))
                    if create_body.get("status") == "ok":
                        _AUTO_SAVE_LAST_SAVED[session_id] = time.time()
                        return
                    else:
                        logger.warning(
                            "auto_save[%s] create also failed: %s",
                            session_id[:12],
                            create_body.get("error", {}).get("code", "?"),
                        )
            else:
                logger.warning(
                    "auto_save[%s] append HTTP %d", session_id[:12], he.code,
                )
    except Exception as e:
        logger.warning("auto_save[%s] write error: %s", session_id[:12], e)


# ─── pre_llm_call 主入口 ───────────────────────────────


def inject_relevant_memories(
    session_id: str,
    user_message: str = "",
    is_first_turn: bool = False,
    **kwargs,
) -> dict | None:
    """注入相关记忆。

    搜索 OpenViking 中与当前对话相关的记忆注入到系统提示。
    自我循环防护：自动过滤掉当前会话自己之前存到 OpenViking 的摘要。
    """
    context_parts = []

    # 搜索 OpenViking 记忆
    if _should_search(user_message, is_first_turn):
        limit = MAX_RESULTS_FIRST_TURN if is_first_turn else MAX_RESULTS_SUBSEQUENT
        items = _search_openviking(user_message, limit)
        memory_ctx = _build_memory_context(items, exclude_session_id=session_id)
        if memory_ctx:
            context_parts.append(memory_ctx)

    if not context_parts:
        return None

    separator = "\n" + "━━━━━━━━━━━━━━━━━━━━━━━━" + "\n"
    merged = separator.join(context_parts)
    full_context = merged + "\n" + "━━━━━━━━━━━━━━━━━━━━━━━━"

    return {"context": full_context}
