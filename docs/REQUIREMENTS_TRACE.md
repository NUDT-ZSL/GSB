# 需求追溯矩阵

| 业务要求 | 实现位置 | 关键测试 |
| --- | --- | --- |
| 1. 确定性桥接展开与排序、结构化错误码 | `xrel/expansion.py` | `tests/test_expansion.py` |
| 2. 置信度加权 BM25、合并规则、明细可重算 | `xrel/scoring.py` | `tests/test_pipeline.py::test_score_replay_from_contribution_detail` |
| 3. 三精度统一算子语义、配置错误码 | `xrel/numerics.py`, `xrel/config.py` | `tests/test_numerics.py`, `tests/test_config_validation.py` |
| 4. 四级误差谱、主导归因、定点重算校验 | `xrel/error_spectrum.py` | `tests/test_error_spectrum.py` |
| 5. top-k 门限策略、次正规数保护、保护/跳过记录 | `xrel/topk.py` | `tests/test_topk_threshold.py` |
| 6. 版本化快照、整体替换、增量失效与复用 | `xrel/snapshot.py`, `xrel/concurrency.py`, `xrel/pipeline.py` | `tests/test_pipeline.py` 增量用例 |
| 7. query_id 查询、缓存键、血缘、过期拒绝与重放 | `xrel/lineage.py`, `xrel/pipeline.py` | `test_stale_result_rejected_after_bridge_change`, `test_lineage_break_detected_on_tampered_result` |
| 8. 并发闸门、资源上限、降级、查询/批量一致性自检 | `xrel/concurrency.py`, `xrel/scoring.py`, `xrel/pipeline.py` | `test_stage_lock_is_non_blocking`, `test_resource_limit_raises_when_degrade_disabled`, `test_query_batch_consistency_self_check` |

## 业务错误码对照

| 代码 | 触发条件 |
| --- | --- |
| `E_CONFIG_INVALID` | 定点 scale 非正、舍入模式未知、精度档未知等配置错误 |
| `E_CONFIDENCE_OUT_OF_RANGE` | 桥接置信度不在 `[0,1]` |
| `E_DIRECTION_MISSING` | 语言对方向未在方向表声明 |
| `E_ERR_SPECTRUM_INVALID` | 误差区间顺序非法或定点偏差无法由 scale/舍入重算 |
| `E_THRESHOLD_STRATEGY_INVALID` | 未知门限比较策略 |
| `E_THRESHOLD_TOLERANCE_INVALID` | 非法（负/非有限）容差 |
| `E_STALE_RESULT` | 结果依赖版本与当前快照不一致 |
| `E_LINEAGE_BROKEN` | 血缘缺失、校验和不符或重放不一致 |
| `E_STAGE_BUSY` | 提交进行中，后到者立即被拒绝 |
| `E_VERSION_ROLLBACK` | 已发布版本被旧版本覆盖 |
| `E_RESOURCE_LIMIT` | 资源预算超限且不允许降级 |
