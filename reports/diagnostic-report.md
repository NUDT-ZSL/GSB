# 转写治理组件诊断报告

- 总体结果：通过
- 检查项：25/25 通过，失败 0
- Python：3.10.12；NumPy：1.26.4
- 裁决结论统计：覆盖 1，冲突 3，补充 1

## 检查项

| 结果 | 检查 | 业务码 |
| --- | --- | --- |
| PASS | `v1_sample_loads` | `OK` |
| PASS | `v2_sample_loads_with_sqlite` | `OK` |
| PASS | `pcm_is_deterministic_16k_mono` | `OK` |
| PASS | `frame_grid_alignment` | `OK` |
| PASS | `difference_attribution_reuses_frame_grid` | `OK` |
| PASS | `v1_missing_field.v1.txt_business_code` | `E_FIELD_MISSING` |
| PASS | `v1_duplicate_id.v1.txt_business_code` | `E_DUPLICATE_UTTERANCE_ID` |
| PASS | `v1_time_regression.v1.txt_business_code` | `E_TIME_REGRESSION` |
| PASS | `v1_invalid_interval.v1.txt_business_code` | `E_INVALID_INTERVAL` |
| PASS | `v1_bad_header.v1.txt_business_code` | `E_FORMAT_HEADER` |
| PASS | `v2_missing_field.json_business_code` | `E_FIELD_MISSING` |
| PASS | `v2_duplicate_id.json_business_code` | `E_DUPLICATE_UTTERANCE_ID` |
| PASS | `v2_time_regression.json_business_code` | `E_TIME_REGRESSION` |
| PASS | `v2_invalid_interval.json_business_code` | `E_INVALID_INTERVAL` |
| PASS | `migration_boundaries_preserved` | `OK` |
| PASS | `cross_version_adjudication_rules` | `OK` |
| PASS | `refresh_invalidation_and_millisecond_equality` | `OK` |
| PASS | `canonical_consistency_passes` | `OK` |
| PASS | `overlap_returns_e_overlap` | `E_OVERLAP` |
| PASS | `gap_returns_e_gap` | `E_GAP` |
| PASS | `content_cache_same_keys_hit` | `OK` |
| PASS | `transcript_migration_adjudication_artifacts_cached` | `OK` |
| PASS | `config_change_targeted_interval_invalidation` | `OK` |
| PASS | `point_query_and_dependency_replay` | `OK` |
| PASS | `snapshot_version_checksum_and_e_stage_busy` | `OK` |

## 产物

- JSON：`reports/diagnostic-report.json`
- 样本目录：`samples/`
- 内容缓存：`reports/content-cache/`
- 快照存储：`reports/snapshot-store/`

