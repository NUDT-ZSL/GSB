# 实际验证结果

环境：Python 3.10.12、NumPy 1.26.4，纯本地执行，无网络/容器。

## 单元测试

命令：`python -m unittest discover -s tests`

结果：`Ran 24 tests ... OK`，覆盖确定性、码点偏移、最长优先、双路径一致、
剪枝统计、规模取舍、流式分批、双重预算、增量失效、快照回退、标签退休、
输入/配置校验与追溯重放。

## 端到端报告（96 条确定性工单，`data/report.json`）

| 验收项 | 实测结果 |
| --- | --- |
| v1 路径取舍 | `path=exact`、`streaming=false`、`batch_size=96`；观测含工单数 96、关键词 32、平均查询码点 9.645833、平均关键词码点 2.71875、启用意图 9 |
| 双路径一致性 | 意图一致率 `1.0`，分歧 `0`；置信度偏差谱 max/mean/p50/p90/p99 全为 `0.0`，96 条全部落在 `eq_0` 桶 |
| 重放校验 | `all_passed=true`，96 条全部按缓存键+规则/门限版本重放成功 |
| v2 配置演进增量 | 全量与增量指纹逐字节一致 `byte_identical=true`；重算 18 条、复用 78 条；受影响规则 `logistics_query/payment_issue/refund_request` |
| 标注修正增量 | 仅重算 `TK00003` 一条，其余复用；全量与增量指纹一致 |
| v3 标签退休 | 4 条历史投诉流量命中退休标签，按回退意图处理并标记 `fallback_category=retired_label`；无回退场景单测抛出 `E_LABEL_RETIRED` |
| 比较预算 | 紧预算下 `budget_downgrade`，准入 64、拒绝 32，未准入工单不进结论集；零准入抛 `E_MATCH_BUDGET` |
| 内存预算 | 驻留上限 1 字节时抛 `E_RESOURCE_LIMIT` |
| 剪枝分歧注入 | 近似路径强制漏掉 `快递单号` 触发 `E_PATH_DIVERGENCE`，归因 `candidate_pruning_miss`，首个偏差工单与关键词入报告 |
| 确定性自证 | 两次独立运行报告与缓存文件集合逐字节一致（69 个缓存产物，报告 sha256 前缀相同） |

## 规模自适应对照

| 工单数 | 选择路径 | 流式 | 精确估计 | 近似估计 |
| --- | --- | --- | --- | --- |
| 48 | exact | 否 | 15744 | 461 |
| 400 | approximate | 是（7 批，每批 64） | 121952 | 3514 |

大规模运行 400 条全部覆盖，双路径意图一致率仍为 `1.0`。

## 异常链路业务码

报告 `error_chain` 实测包含：`E_EMPTY_KEYWORD`、`E_LABEL_NOT_CONTROLLED`、
`E_SNAPSHOT_ROLLBACK`、`E_THRESHOLD_RANGE`、`E_UNKNOWN_CONFIG_VERSION`；
输入校验包含 `E_DUPLICATE_TICKET_ID`、`E_EMPTY_QUERY`。
