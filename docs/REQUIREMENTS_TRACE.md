# 需求追踪矩阵

| 要求 | 实现位置 | 验证 |
| --- | --- | --- |
| v1/v2 严格解析、固定观测时钟、拒绝清单继续处理 | `sbom_audit/parser.py` | `test_invalid_records_continue_and_have_codes`；验收器检查 6 类业务码 |
| 规范键归并、代数/时间权威、墓碑覆盖、冲突双来源 | `sbom_audit/model.py`, `sbom_audit/merge.py` | 41 项验收检查；基线报告含 4 条冲突 |
| 算法白名单、位宽、跨算法同步推导 | `sbom_audit/hashing.py`, `sbom_audit/merge.py` | `lib-net` 的 `hmac-over-digest` 推导一致；非法摘要样本返回 `E_HASH_ALGO` |
| 依赖图、环、反向闭包、版本约束标注 | `sbom_audit/graph.py` | `test_cycle_detection_lists_keys`；真值闭包 8 个构件 |
| 增量等价、重复计数、单事务、分歧回滚 | `sbom_audit/engine.py` | `verify_sample` 比较增量与从零全量核心字段；重复批次 `duplicate_count>=1` |
| 并发、真实墙钟超时、队列上限、计算量上限 | `Budget`, `BatchScheduler`, `prepare_batch` in `sbom_audit/engine.py` | `test_timeout_queue_and_work_budget`；CLI 超时实测 `E_TIMEOUT` |
| 检查点双持久化、原子写、版本重建、半写残留恢复 | `sbom_audit/checkpoint.py`, `sbom_audit/storage.py` | `test_checkpoint_policy_change_forces_rebuild`；验收器模拟半写文件 |
| 四类 CLI、退出码、JSON 报告、固定种子真值 | `sbom_audit/cli.py`, `sbom_audit/samplegen.py`, `sbom_audit/verify.py` | `python -m sbom_audit.verify`；README/VALIDATION 中命令 |

## 等价性边界

增量批次应用时，`AuditEngine` 在同一 SQLite 写事务内读取全部已提交原始记录，构造全量报告，并与基于缓存视图/冲突的局部重算结果做规范 JSON 比较。若两者任一核心或包装字段不同，返回 `E_RECOMPUTE_DIVERGED` 并回滚事务。

跨进程或并发导入时，调度器并发执行解析与摘要工作量，再通过共享锁和同一引擎顺序提交批次事务；SQLite 连接仍为每个批次事务独立创建，写事务使用 `BEGIN IMMEDIATE`。

## 真值使用

`truth.json` 由固定种子生成，显式包含：

- `sender_digests` 与每个构件的 `sender_original_digest`
- `sync_marker`
- `expected_seeds`
- `expected_closure`
- `expected_reverse_dependents`
- `incremental_expected_closure`

验收器只读取该显式真值文件，不导入生成器内存对象或中间计算结果。
