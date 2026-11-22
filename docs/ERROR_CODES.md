# 业务错误码

非法输入不会写入连接运行状态。CLI 输出 `{"ok":false,"error":{...}}`，FastAPI 同时映射到 HTTP 4xx/5xx。

## 场景

| 错误码 | 含义 |
|---|---|
| `SCENARIO_JSON_001` | 场景 JSON 无法解析或不是对象 |
| `SCENARIO_SCHEMA_001` | 场景字段缺失、类型错误或引用未声明流 |
| `STREAM_ID_001` | 流 ID 非法、重复或非严格递增 |
| `STREAM_PARITY_001` | 流 ID 奇偶性与发起方角色不一致 |
| `FLOW_INITIAL_001` | 初始窗口非正、上限小于初始值 |
| `WEIGHT_001` | 权重不在 `1..256` |
| `CLOCK_001` | 逻辑时钟为负或场景/事件/补丁时钟回退 |
| `EVENT_SEQ_001` | 帧序号非从 1 连续递增 |
| `HEADER_ORDER_001` | CONTINUATION 缺失、错序、跨流或打断其他事件 |
| `HEADER_INCOMPLETE_001` | 场景结束时 HEADERS 块仍未出现 END_HEADERS |
| `WINDOW_UPDATE_001` | 增量为负、层级不匹配或余额超过上限 |
| `SETTINGS_001` | SETTINGS 字段或窗口调整非法 |
| `SETTINGS_ACK_001` | ACK 没有对应待确认 SETTINGS |
| `PING_001` | PING 载荷不是 8 字节或 ACK 不匹配 |
| `GOAWAY_001` | GOAWAY 最后流 ID 越界或 GOAWAY 后新建流 |
| `FRAME_001` | 帧类型、必需字段或长度非法 |
| `FRAME_STATE_001` | 当前流状态不允许该帧 |
| `FLOW_001` | DATA 超出流级或连接级窗口 |
| `EXPECTED_STATE_001` | 模拟最终状态与声明不一致 |

## 补丁

| 错误码 | 含义 |
|---|---|
| `PATCH_JSON_001` | 补丁 JSON 无法解析或不是对象 |
| `PATCH_SCHEMA_001` | 格式、版本、模式或结构非法 |
| `PATCH_DIGEST_001` | `content_digest` 与实际规范化摘要不一致 |
| `PATCH_REF_001` | 补丁引用未声明流 |
| `PATCH_VERSION_001` | 目标版本回退 |
| `PATCH_BASE_001` | `base_version` 不是当前版本，或提交时版本已变化 |
| `PATCH_CONFLICT_001` | 同版本/同 patch_id 内容冲突 |
| `PATCH_OP_001` | 操作类型、方向、side 或参数非法 |
| `PATCH_REPLAY_001` | 补丁改变已提交事件前缀或更换连接 |
| `PATCH_REPLACE_001` | 完整替换包含无法从场景重建的运行态操作 |
| `PATCH_SETTINGS_001` | SETTINGS 补丁导致窗口不变量失败 |

## 提交、恢复与校验

| 错误码 | 含义 |
|---|---|
| `CONNECTION_EXISTS_001` | 连接已存在且场景摘要不同 |
| `CONNECTION_NOT_FOUND_001` | 连接不存在 |
| `IDEMPOTENCY_001` | 相同帧键出现不同内容或越过提交边界 |
| `DB_001` | 持久化结构或临时表暂存不一致 |
| `LOCK_001` | 文件锁超时 |
| `RESUME_001` | 恢复前置条件失败 |
| `VERIFY_001` | 验收入参或重算路径前置条件失败 |
