# CTR 精排管线验证报告

- 生成时间：`2026-11-17T02:46:11.434467+00:00`
- Python：`3.11.0rc1`
- numpy：`2.4.6`
- PyYAML：`5.4.1`
- 总体结果：`PASS`

## 验收检查

| 检查项 | 结果 | 说明 |
|---|---|---|
| `serial_parallel_digest` | **PASS** | 串行与并发报告逐字段一致性摘要相同。 cc60b0e61126959124ad88863111dfb7dee114143a601d4693d68e84f98bb2b2 |
| `configured_width_fields` | **PASS** | 配置位宽下候选、排序、贡献分解和阶段指纹逐字段一致。 {"mismatch_count": 0, "mismatches": [], "request_count": 8} |
| `high_width_fields` | **PASS** | 高一档位宽下候选、排序、贡献分解和阶段指纹逐字段一致。 {"mismatch_count": 0, "mismatches": [], "request_count": 8} |
| `serial_parallel_artifacts` | **PASS** | 串行与并发的四阶段快照产物版本一致。 {"request_count": 16} |
| `budget_exhaustion` | **PASS** | 全局预算精确耗尽并记录预算终止失败。 {"consumed": 52, "global_budget": 52, "remaining": 0} |
| `quota_exhaustion` | **PASS** | 单请求配额超限被隔离并以 E_REQ_QUOTA_EXCEEDED 记录。 {"code": "E_REQ_QUOTA_EXCEEDED", "exception_type": "QuotaExceeded", "injection_ids": [], "message": "request candidates 9 exceed per-request quota 7", "ready_queue_position": 3,... |
| `injection_hits` | **PASS** | 三类异常注入全部命中并进入失败清单。 [{"candidate_id": "item-004-02", "error_codes": ["E_NON_FINITE"], "exception_types": ["PipelineError"], "fault_type": "non_finite_feature", "hit": true, "hit_request_id": "req-0... |
| `boundary_sensitive` | **PASS** | 高位宽排序翻转样本及其贡献差异被独立记录。 {"boundary_sensitive_sample_count": 1, "fusion": {"max_abs_delta": 0.0007714200000000782, "mean_request_max_abs_delta": 0.0005187475000000413, "request_count": 8}, "normalizatio... |
| `graceful_exit` | **PASS** | 可处理退出在提交快照后以 0 退出，并从游标续跑。 {"partial_snapshot_version": 3, "partial_status": "interrupted"} |
| `forced_termination_resume` | **PASS** | 强制终止后从最后原子清单恢复且结果一致。 {"crash_returncode": 137, "cursor": 3, "manifest_version": 3} |
| `config_mismatch_rejected` | **PASS** | 配置哈希变化时拒绝续跑并输出 E_CONFIG_MISMATCH。 {"code": "E_CONFIG_MISMATCH", "exception_type": "ConfigMismatch", "injection_ids": [], "message": "persisted configuration hash does not match the current input", "request_id": ... |
| `unknown_group_code` | **PASS** | 未知特征组输出 E_GROUP_UNKNOWN。 {"code": "E_GROUP_UNKNOWN", "exception_type": "ValueError", "injection_ids": [], "message": "feature group 'unregistered_group' is not declared in known_feature_groups", "reques... |
| `width_invalid_code` | **PASS** | 配置位宽越界输出 E_WIDTH_INVALID。 {"code": "E_WIDTH_INVALID", "exception_type": "PipelineError", "injection_ids": [], "message": "fixed_point.width=99 is outside [0, max_width=12]", "request_id": "__global__", "... |
| `global_loader_codes` | **PASS** | 主样本包含 E_REQ_EMPTY 与 E_JSON_INVALID 失败隔离。 ["E_BUDGET_EXHAUSTED", "E_JSON_INVALID", "E_NON_FINITE", "E_REQ_EMPTY", "E_REQ_QUOTA_EXCEEDED", "E_WEIGHT_SHAPE", "E_WIDTH_INVALID"] |
| `atomic_no_temp_files` | **PASS** | 运行目录不残留原子写临时文件。 {"tmp_count": 0} |

## 关键结果

- 串行一致性摘要：`cc60b0e61126959124ad88863111dfb7dee114143a601d4693d68e84f98bb2b2`
- 并发一致性摘要：`cc60b0e61126959124ad88863111dfb7dee114143a601d4693d68e84f98bb2b2`
- 快照版本：`16`
- 成功请求数：`8`
- 失败请求数：`8`
- 预算消耗 / 总预算：`52 / 52`
- 边界敏感样本数：`1`
- 业务码集合：`E_BUDGET_EXHAUSTED, E_CONFIG_MISMATCH, E_GROUP_UNKNOWN, E_JSON_INVALID, E_NON_FINITE, E_REQ_EMPTY, E_REQ_QUOTA_EXCEEDED, E_WEIGHT_SHAPE, E_WIDTH_INVALID`

## 复现命令

```bash
python3 scripts/generate_samples.py
python3 scripts/verify.py
python3 -m unittest -v tests.test_pipeline
```

验证报告由 `scripts/verify.py` 以临时文件加 `os.replace` 原子写入。
