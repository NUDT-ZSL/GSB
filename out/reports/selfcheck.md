# TII 自检报告

- 总体结果: **PASS**
- 输入目录: `input`
- 输出目录: `out`
- 固定种子: `42`
- 检查点模式: `both`
- 任务/已完成: `10` / `10`
- 缓存产物数: `59`
- 报告校验和: `66f2a40fea6a327465200266ba419d0c012d9aeb2d60128c9d9a13a9da87a16c`

## 检查项

- `PASS` source_ingestion_and_duplicate_accounting
- `PASS` versioned_ledger_integrity
- `PASS` checkpoint_content_and_monotonic_version
- `PASS` cache_keys_reconstruct_from_task_hash_and_config
- `PASS` cache_artifact_integrity
- `PASS` dual_mode_shared_forward_consistency
- `PASS` logical_clock_order_and_memory_budget
- `PASS` targeted_invalidation_sets_and_suffixes
- `PASS` no_uncommitted_pending_artifacts

## 失效传播场景

- 仅配置依赖: `{'unchanged': ['case-001', 'case-002', 'case-003', 'case-004'], 'config_only': ['case-005', 'case-006', 'case-007', 'case-008', 'case-009', 'case-010']}`
- 仅数据依赖: `{'unchanged': ['case-001', 'case-002', 'case-004', 'case-005', 'case-006', 'case-007', 'case-009', 'case-010'], 'data_only': ['case-003', 'case-008']}`
- 配置与数据同时变化: `{'unchanged': ['case-001', 'case-002', 'case-003', 'case-004'], 'config_only': ['case-005', 'case-006', 'case-009', 'case-010'], 'both': ['case-007', 'case-008']}`

报告由固定输入、固定种子和已提交内容生成，不包含墙钟时间。
