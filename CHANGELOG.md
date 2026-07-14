# Changelog

## v1.1.0 (2026-07-14)

### 🐛 Bug Fixes

- **P0: 404 fallback 用错 headers** — `auto_save_memories()` 的 append→create 回退路径使用了 `SEARCH_HEADERS`（不含 API Key），导致 create 写入永远失败。改为 `_WRITE_HEADERS`（含 X-API-Key）。同时将 fallback timeout 从硬编码 10s 对齐为 `REQUEST_TIMEOUT`（5s）。

- **SQLite 连接泄漏** — `_get_last_session_summary()` 在异常路径下未关闭 `db` 连接。改用 `try/except/finally` 兜底，与 `auto_save_memories()` 的写法保持一致。

### 🔄 功能优化

- **自反馈循环过滤** — auto-save 将当前对话写入 `viking://resources/hermes-auto-memories/` 后，后续轮次的记忆搜索会将其搜回并注入上下文，形成无效的「自己存自己看」循环。修复：`_build_memory_context()` 新增 `exclude_session_id` 参数，三重闸门过滤（非空检查 → URI 前缀匹配 → session_id 文本匹配），确保当前会话的 auto-save 内容不会被注入。

### 🔧 代码质量

- **日志级别整理** — 正常流程的 4 处 `logger.warning` 改为 `logger.debug`，避免运维日志被调试信息淹没。保留 8 处 `warning` 用于真实异常路径。
- **`datetime.now()` 单次调用** — 合并两次调用为一次 `now_dt` 变量复用，避免跨午夜边界的时间不一致。
