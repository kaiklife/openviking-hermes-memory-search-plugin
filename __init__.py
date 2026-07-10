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

# 插件目录 + boot 标记
HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
PLUGIN_DIR = HERMES_HOME / "plugins" / "hermes-memory-search"
BOOT_MARKER = PLUGIN_DIR / "boot.json"
STATE_DB = HERMES_HOME / "state.db"

# 写操作需要 API Key
_OV_API_KEY = "ov-root-key-2026"
_WRITE_HEADERS = {
    "X-OpenViking-Account": "fanwenkai",
    "X-OpenViking-User": "fanwenkai",
    "X-API-Key": _OV_API_KEY,
    "Content-Type": "application/json",
}

# 超过此秒数的标记差异视为「重启」
_RESTART_THRESHOLD_SECONDS = 30


# ─── 重启无感感知 ─────────────────────────────────────


def _detect_restart() -> tuple[bool, float, float]:
    """检测是否是重启后的首次加载。

    返回 (is_restart, prev_boot_ts, current_boot_ts)。
    prev_boot_ts 用于后续查询重启前那个会话。
    current_boot_ts 是当前进程的启动时间戳，用于 SQL 查询边界。

    判断逻辑：
    1. PID 变化 -> 绝对就是新进程（OOM/crash/systemctl 都会触发）
    2. 时间差 > 30 秒 -> 补充判断（boot.json 被删等极端情况兜底）
    没有 boot.json -> 首次安装或已清理，不触发
    """
    now = time.time()
    is_restart = False
    prev_boot_ts = 0.0
    current_pid = os.getpid()

    try:
        PLUGIN_DIR.mkdir(parents=True, exist_ok=True)

        if BOOT_MARKER.exists():
            try:
                data = json.loads(BOOT_MARKER.read_text(encoding="utf-8"))
                prev_boot_ts = data.get("ts", 0.0)
                old_pid = data.get("pid")

                # PID 不同 -> 新进程，必是重启
                if old_pid is not None and old_pid != current_pid:
                    is_restart = True
                    logger.info(
                        "Restart detected (pid changed: %s -> %s)",
                        old_pid, current_pid,
                    )
                # 时间差 > 阈值（兜底：boot.json 被删重建等）
                elif now - prev_boot_ts > _RESTART_THRESHOLD_SECONDS:
                    is_restart = True
                    logger.info(
                        "Restart detected (last boot: %.1fs ago, pid: %s)",
                        now - prev_boot_ts, current_pid,
                    )
            except (json.JSONDecodeError, OSError, TypeError):
                pass

        # 保存当前 PID 和时间戳
        marker = {"ts": now, "pid": current_pid}
        if is_restart:
            marker["prev_ts"] = prev_boot_ts

        BOOT_MARKER.write_text(
            json.dumps(marker, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("Failed to write boot marker: %s", e)

    return is_restart, prev_boot_ts, now


# 模块级：插件被导入时立即检测
_RESTART_DETECTED, _RESTART_PREV_BOOT_TS, _RESTART_CURRENT_TS = _detect_restart()
# per-session 追踪：每个 session 独立获得一次重启通知，不互相抢
# 避免 Gateway 多 session 环境下第一个 session 消费掉全局 flag
_RESTART_NOTIFIED_SESSIONS: set[str] = set()


def _get_last_session_summary(before_ts: float, current_ts: float) -> str | None:
    """从 state.db 查询重启前最后一个会话摘要。

    在重启感知场景中：
    - before_ts 是上一次进程 boot 的时间戳（prev_boot_ts）
    - current_ts 是当前进程 boot 的时间戳（当前 now）

    SQL 使用 started_at < current_ts 找到当前进程启动前
    最新完成的那个会话，排除重启后可能产生的新会话。
    """
    if not STATE_DB.exists() or before_ts <= 0:
        return None

    try:
        import sqlite3

        db = sqlite3.connect(str(STATE_DB), timeout=5.0)
        db.row_factory = sqlite3.Row

        # 获取 current_ts 之前最后一个会话（即重启前活跃的那个）
        session = db.execute(
            """
            SELECT id, source, model, started_at, message_count
            FROM sessions
            WHERE source != 'cron'
              AND started_at < ?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (current_ts,),
        ).fetchone()

        if not session:
            db.close()
            return None

        # 取前 2 条 user 消息看看在聊什么
        messages = db.execute(
            """
            SELECT role, content
            FROM messages
            WHERE session_id = ? AND role = 'user'
            ORDER BY timestamp ASC
            LIMIT 2
            """,
            (session["id"],),
        ).fetchall()

        db.close()

        parts = [f"上一个会话（{session['source']}）"]
        for msg in messages:
            content = (msg["content"] or "").strip()[:120]
            if content:
                display = content[:117] + "..." if len(content) > 120 else content
                parts.append(f"对话：{display}")

        return "\n".join(parts)

    except Exception as e:
        logger.warning("Failed to query state.db: %s", e)
        return None


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
        "limit": safe_limit,
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

    # 客户端侧二次过滤：score 缺失时默认通过，显式低分才剔除
    filtered = [
        m for m in memories
        if m.get("score") is None or m["score"] >= SCORE_THRESHOLD
    ]

    return filtered[:safe_limit]


def _build_memory_context(items: list[dict]) -> str | None:
    """Format search results into a compact context string."""
    lines = []
    for item in items:
        abstract = (item.get("abstract") or "").strip()
        ctype = item.get("context_type", "memory")
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
                    headers=SEARCH_HEADERS,
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
    """注入重启通知 + 相关记忆。

    首轮：如果检测到重启，从 state.db 查上一个会话摘要，注入通知。
          同时搜索记忆。
    后续轮：仅搜索记忆。
    完全无感——不需要手动写任何状态文件。
    """
    context_parts = []

    # 第 1 层：重启通知（每个 session 只注入一次，不依赖 is_first_turn）
    if _RESTART_DETECTED and session_id not in _RESTART_NOTIFIED_SESSIONS:
        _RESTART_NOTIFIED_SESSIONS.add(session_id)
        lines = [
            "🔄 **系统重启通知**",
            "━━━━━━━━━━━━━━━━━━━━━━━━",
            "Hermes 网关刚刚重启了。",
        ]
        # 从 state.db 查重启前在干什么
        last = _get_last_session_summary(_RESTART_PREV_BOOT_TS, _RESTART_CURRENT_TS)
        if last:
            lines.append(last)
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        context_parts.append("\n".join(lines))

    # 第 2 层：OpenViking 记忆搜索
    if _should_search(user_message, is_first_turn):
        limit = MAX_RESULTS_FIRST_TURN if is_first_turn else MAX_RESULTS_SUBSEQUENT
        items = _search_openviking(user_message, limit)
        memory_ctx = _build_memory_context(items)
        if memory_ctx:
            context_parts.append(memory_ctx)

    if not context_parts:
        return None

    separator = "\n" + "━━━━━━━━━━━━━━━━━━━━━━━━" + "\n"
    merged = separator.join(context_parts)
    full_context = merged + "\n" + "━━━━━━━━━━━━━━━━━━━━━━━━"

    return {"context": full_context}
