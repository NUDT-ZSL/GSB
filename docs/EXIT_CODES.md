# 业务码与整数退出码约定

所有 CLI 错误都会向 stderr 输出 JSON，结构为
`{"code": <业务码>, "message": <说明>, "details": <结构化上下文>}`，
并返回下表中的整数退出码；成功输出写入 stdout 且退出码为 0。

| 业务码 | 退出码 | 触发场景 |
| --- | --- | --- |
| `E_OK` | 0 | 成功 |
| `E_OP_UNKNOWN` | 10 | 未知算子或未知激活种类 |
| `E_AXIS_INVALID` | 11 | 轴置换不是合法排列、维度不匹配或最终语义轴序与声明不符 |
| `E_PARAM_INVALID` | 12 | eps 非正/非有限、block_size 非正、形状非法、JSONL 行损坏 |
| `E_DTYPE_UNSUPPORTED` | 13 | dtype 不在 float64/float32 受控集合内 |
| `E_TOLERANCE_EXCEEDED` | 14 | 分块结果超出与 float64 整段参考的声明容差 |
| `E_LATENCY_BUDGET` | 15 | 实测循环耗时超过单次查询延迟预算 |
| `E_INCONSISTENT` | 16 | 查询/批量偏差、自检不变量破坏、重放不一致 |
| `E_RESOURCE_LIMIT` | 17 | 驻留内存/缓存条目/并行重算超过配置上限且不可降级 |
| `E_SNAPSHOT_CORRUPT` | 18 | 快照不可解析或校验和不符 |

`validate` 子命令逐行校验任务文件：只要存在非法行，stdout 摘要的 `code` 为
`E_HAS_INVALID_ROWS`，进程退出码取首个非法行对应的业务退出码。
