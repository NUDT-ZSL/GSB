# 实际验证结果记录

验证环境（本地，无网络/无容器）：

- 解释器：项目虚拟环境 `.venv`，Python 3.11.18
- 依赖：numpy 2.4.6（满足 `>=1.26`），tkinter 9.0（标准库）
- 数据集：代码内固定种子确定性生成，无外部图像/模型

复现命令：

```bash
.venv/bin/python -m unittest discover -s tests      # 单元与集成测试
.venv/bin/python scripts/run_demo.py --workspace <dir>   # 端到端演示
DISPLAY= .venv/bin/python scripts/run_ui.py --workspace <dir>  # 无头工作台
```

## 1. 自动化测试

`python -m unittest discover -s tests` 结果：**28 个测试全部通过（OK）**。

覆盖映射：

| 需求 | 测试 |
| --- | --- |
| 确定性/逐字节一致 | `test_page_pixels_are_seed_deterministic`、`test_node_outputs_byte_identical_across_runs` |
| 缓存键缺项/损坏/复用 | `test_missing_components_raise_invalid_key`、`test_commit_then_hit_returns_same_entry`、`test_corrupt_payload_detection`、`test_key_details_export_roundtrip` |
| 增量失效传播 | `test_append_only_new_page_recomputes`、`test_truth_correction_only_recomputes_aggregate`、`test_threshold_change_invalidates_pixel_suffix` |
| 模板整体替换/回退/损坏 | `test_bump_replaces_whole_snapshot`、`test_unchanged_version_is_reused_not_rewritten`、`test_rollback_is_rejected`、`test_corrupt_snapshot_reports_version` |
| 分块一致性/差异定位 | `test_all_chunk_sizes_match_whole_page`、`test_chunk_mismatch_locates_first_bad_position` |
| 并发写/资源上限 | `test_concurrent_commit_single_winner_and_busy_error`、`test_line_limit_raises_resource_error`、`test_cache_capacity_limit`、`ResourceGuardTest` |
| 自检/异常归因/拓扑 | `test_self_checks_pass_on_healthy_run`、`test_inconsistent_partition_detected`、`TopologyTest`、`test_detect_empty_is_attributed`、`test_match_failure_is_attributed` |

## 2. 端到端演示（实测输出）

演示 8 个步骤全部成功，落盘 `demo_report.json`：

1. `cold_run`：3 页冷启动，六节点全部重算。
2. `warm_run`：二次运行六节点全部 `reused`，识别串与指标逐字节不变。
3. `incremental_plan` / `incremental_run`：
   - 追加 `page-004`：其六节点全部 `recomputed`；
   - 修正 `page-001` 第 0 行真值：仅 `aggregate` 为 `recomputed`，
     `preprocess/detect/features/match/recognize` 全部 `reused`；
   - 未受影响的 `page-002`/`page-003` 全部节点 `reused`，负载哈希不变。
4. `threshold_plan`：检测阈值变更后全部页面从 `preprocess` 起重算，无复用节点。
5. `template_bump`：模板 1→2 整库替换；每页
   `recomputed=['match','recognize','aggregate']`，
   `reused=['preprocess','detect','features']`，验证选择性失效。
6. `chunk_consistency`：chunk 尺寸 1/2/3 下与整页逐行逐字符一致（`identical=true`）。
7. `self_checks`：键↔哈希互核、指标独立重算、拓扑无环全部通过。

实测汇总指标（4 页 / 27 行）：

- 字符准确率 `char_accuracy = 0.9955`（量化值 995/1000）
- 行准确率 `line_accuracy = 0.9630`（量化值 963/1000）
- 演示结束缓存条目数：37

## 3. 错误码链路实测

演示实际触发并归因的业务错误码（去重）：

`E_CACHE_CORRUPT`、`E_CACHE_KEY_INVALID`、`E_CHUNK_MISMATCH`、
`E_DETECT_EMPTY`、`E_PARSE_FAILED`、`E_RESOURCE_LIMIT`、
`E_SNAPSHOT_ROLLBACK`、`E_STAGE_BUSY`。

另有测试覆盖的 `E_INCONSISTENT`、`E_MATCH_FAILED`、`E_GRAPH_CYCLE`、
`E_GRAPH_CONFIG`，合计覆盖设计中的全部错误码，且每条均带 `page_id`/`stage`
或对应定位字段。

## 4. 工作台

- 存在显示（`DISPLAY=:0`）时 `scripts/run_ui.py` 启动 Tkinter 窗口并进入
  事件循环（交互型，需人工关闭）。
- 无显示环境（`DISPLAY=`）自动回退为无头文本摘要，已实测可完整输出
  拓扑状态、失效覆盖、准确率、模板版本与错误码，无需任何外部服务。
