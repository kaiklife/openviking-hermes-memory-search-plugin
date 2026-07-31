"""hermes-memory-search: 自动从 OpenViking 拉取相关记忆 + 重启检测。

在每轮 LLM 调用前自动搜索 OpenViking 中与当前对话相关的记忆，
以 context 形式注入系统提示。

重启感知：通过 boot.json PID 对比检测进程重启，首次检测时注入重启通知。"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
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


def _extract_restart_context_from_session(session_id: str, max_msgs: int = 5) -> str:
    """从 session jsonl 文件自动提取重启前上下文。
    
    读取最后几条 assistant 消息，提取包含"重启"关键词的内容。
    """
    if not session_id:
        return ""
    
    sessions_dir = Path.home() / ".hermes" / "sessions"
    session_file = sessions_dir / f"{session_id}.jsonl"
    
    if not session_file.exists():
        logger.debug("Session file not found: %s", session_file)
        return ""
    
    try:
        # 读最后 N 行（消息）
        lines = []
        with open(session_file, "rb") as f:
            # 从文件末尾往前读
            f.seek(0, 2)
            file_size = f.tell()
            pos = file_size
            while pos > 0 and len(lines) < max_msgs * 2:  # 多读一些，跳过 tool 消息
                pos = max(0, pos - 4096)
                f.seek(pos)
                chunk = f.read(min(4096, file_size - pos))
                lines.extend(chunk.split(b"\n"))
                if len(lines) >= max_msgs * 2:
                    break
        
        # 解析最后几条 assistant 消息
        restart_hints = []
        for line in lines[-20:]:  # 看最后20行
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            
            if msg.get("role") != "assistant":
                continue
            
            content = msg.get("content", "")
            if not content:
                continue
            
            # 找包含"重启"相关关键词的消息
            lower = content.lower()
            if any(kw in lower for kw in ["重启", "restart", "修复", "改了一下", "修改了", "修好了"]):
                # 提取前200字符作为摘要
                hint = content[:200].replace("\n", " ").strip()
                if hint:
                    restart_hints.append(hint)
        
        if restart_hints:
            # 取最后2条最相关的
            return "\n".join(f"• {h}" for h in restart_hints[-2:])
        
    except Exception as e:
        logger.warning("Failed to extract restart context from session: %s", e)
    
    return ""


# ─── 记忆搜索（pre_llm_call） ───────────────────────────────


def register(ctx) -> None:
    """Register the hermes-memory-search plugin with the Hermes runtime."""
    ctx.register_hook("pre_llm_call", inject_relevant_memories)


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


# ─── pre_llm_call 主入口 ───────────────────────────────


def inject_relevant_memories(
    session_id: str,
    user_message: str = "",
    is_first_turn: bool = False,
    **kwargs,
) -> dict | None:
    """注入相关记忆 + 重启通知。

    搜索 OpenViking 中与当前对话相关的记忆注入到系统提示。
    自我循环防护：自动过滤掉当前会话自己之前存到 OpenViking 的摘要。
    重启检测：通过 boot.json PID 对比判断进程是否重启，首次检测时注入重启通知。
    """
    context_parts = []

    # ── 重启检测：boot.json PID 对比 ──────────────────────────────
    boot_path = Path(__file__).parent / "boot.json"
    try:
        if boot_path.exists():
            boot_data = json.loads(boot_path.read_text())
            current_pid = os.getpid()
            saved_pid = boot_data.get("pid")
            notified = boot_data.get("notified", False)
            if saved_pid is not None and saved_pid != current_pid:
                boot_time = boot_data.get("boot_time", "unknown")
                logger.info(
                    "Restart detected: pid %d → %d, injecting notification",
                    saved_pid, current_pid,
                )
                # 读取重启前上下文：优先手动存的文件，否则自动从 session 提取
                restart_context = ""
                restart_context_file = Path.home() / ".hermes" / "restart_context.json"
                if restart_context_file.exists():
                    try:
                        ctx_data = json.loads(restart_context_file.read_text())
                        restart_context = (
                            f"\n\n**重启前上下文：**\n"
                            f"- 干了啥: {ctx_data.get('what', '未知')}\n"
                            f"- 为啥重启: {ctx_data.get('why', '未知')}\n"
                            f"- 接下来: {ctx_data.get('next', '未知')}\n"
                            f"- 时间: {ctx_data.get('time', '未知')}"
                        )
                        restart_context_file.unlink()  # 读完删掉，避免重复注入
                    except Exception as e:
                        logger.warning("Failed to read restart context: %s", e)
                else:
                    # 自动从 session jsonl 提取重启上下文
                    auto_ctx = _extract_restart_context_from_session(session_id)
                    if auto_ctx:
                        restart_context = f"\n\n**重启前上下文（自动提取）：**\n{auto_ctx}"
                
                context_parts.append(
                    f"🔄 **系统重启通知**\n"
                    f"• 重启时间: {boot_time}\n"
                    f"• 旧PID: {saved_pid} → 新PID: {current_pid}\n"
                    f"• 状态: 记忆库已重新加载{restart_context}"
                )
                # 更新 boot.json
                boot_data["pid"] = current_pid
                boot_data["notified"] = True
                boot_path.write_text(
                    json.dumps(boot_data, ensure_ascii=False, indent=2)
                )
    except Exception as e:
        logger.warning("Restart detection failed: %s", e)
    # ──────────────────────────────────────────────────────────────

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
