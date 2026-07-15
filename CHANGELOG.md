# Changelog

## [4.3.0] — 2026-07-15

### ✨ 新功能

- **重启通知恢复** — 在 `inject_relevant_memories` 中恢复 boot.json PID 检测逻辑。进程重启后首次调用时注入 "🔄 系统已重启，记忆库已重新加载" 通知。boot.json 格式统一为 `{boot_time, pid, notified}`。

## [4.2.0] — 2026-07-14

### 🐛 Bug Fixes

- **P0: 404 fallback create 请求鉴权失败** — append 模式 404 后 fallback 到 create 时，使用了不带 API Key 的 `SEARCH_HEADERS`，导致请求永远被 OpenViking 拒绝。改为 `_WRITE_HEADERS`。同时修正 fallback 超时从硬编码 10s 统一为 `REQUEST_TIMEOUT`（5s）。

### 🔄 性能 & 可靠性

- **自反馈循环防护** — 新增 `exclude_session_id` 过滤机制。`_build_memory_context()` 在搜索结果中跳过本会话 auto-save 路径下的内容，防止「当前会话存 → 下轮搜回来 → 重复注入」的恶性循环。
- **SQLite 连接泄漏修复** — `_get_last_session_summary()` 改用 `try/except/finally`，确保异常路径下 `db.close()` 也能被调用（之前仅 `auto_save_memories()` 正确实现了 finally 关闭）。
- **`datetime.now()` 单次调用** — 修复两次 `datetime.now()` 调用可能导致跨午夜时日期不一致的问题，改为一次 `now_dt` 后复用。

### 🧹 代码质量

- **日志级别规范化** — 正常流程调试信息（函数入口、返回结果、空结果、过滤跳过）从 `logger.warning` 降级为 `logger.debug`，保留 `warning` 仅用于真实异常/错误路径。减少日志噪声，避免运维告警被淹没。

## [4.1.0] — 2026-07-12

### ✨ 新功能

- `post_llm_call` 自动存记忆：每轮 LLM 调用后自动将对话概要写入 OpenViking

## [4.0.0] — 2026-07-08

### ✨ 新功能

- 初始版本：pre_llm_call 记忆检索 + 重启无感感知（boot.json PID 标记）
