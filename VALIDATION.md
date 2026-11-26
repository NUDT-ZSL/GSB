# 实际验证记录

验证时间：2026-11-27（Asia/Shanghai）。

## 1. 环境

```text
Python 3.10.12
NumPy 2.0.0（满足 numpy>=1.24）
pytest 8.2.0
SQLite 3.37.2
Linux/WSL Ubuntu 工作区
```

环境输出留档：`artifacts/validation/environment.log`。

## 2. 自动化测试

命令：

```bash
PYTHONPATH=. python3 -m pytest -q
```

实际结果：

```text
...............                                                          [100%]
15 passed in 2.34s
```

日志：`artifacts/validation/pytest.log`。

测试覆盖：

- 固定种子逐字节确定性；
- `E_STRUCT` 结构违规；
- 三格式读取与语义一致；
- 六方向格式互迁、补全来源和 v3 -> v1 丢弃规则；
- `E_SEMANTIC_DRIFT` 首个行列差异；
- 写入哈希、版本回退、区间重叠；
- 增量重算/复用分区；
- 独立磁盘裁决；
- 缓存篡改后的 `E_CROSS_VIEW_MISMATCH`；
- 14 项异常注入业务码；
- 真实子进程 `SIGKILL` 强制终止恢复；
- CSR 已发布但阶段快照未提交时的写入回滚恢复；
- SQLite 位图覆盖错误。

## 3. 一键验收

命令：

```bash
bash scripts/verify.sh
```

实际结果：退出码 `0`，日志：`artifacts/validation/verify-script.log`。

该脚本依次执行编译检查、14 个 pytest 测试、样本生成、完整演示、独立裁决和自检。

## 4. 固定种子样本

命令：

```bash
PYTHONPATH=. python3 -m truth_ledger.cli generate --out samples --cases 2
```

产物：

- `samples/task_manifest.json`
- `samples/cases/case-001/`
- `samples/cases/case-002/`
- 每个 case 包含 `initial_csr.json`、`rhs_dense_b.npy`、`rhs_sparse.json`、两个写入事件和 v1/v2/v3 初始账本。

同一 `case_id=case-byte`、`seed=20261127`、同一配置连续生成两次：

```text
same_initial_csr_bytes = true
byte_count = 3951
initial_csr_sha256 = sha256:ed60e4471348925430dea1ab896ddd2c16b38c1dcca861d933aa0d84df8eea8c
```

## 5. 三格式互迁

演示案例在阶段 2 实际执行 6 个有向转换：

```text
1 -> 2, 1 -> 3
2 -> 1, 2 -> 3
3 -> 1, 3 -> 2
```

实际结果：

```text
conversion_count = 6
semantic_equal = true
```

补全来源包含：

```text
shard_boundaries
bitmap_digest
column_block_mapping
structural_algebra_defaults
block_bitmap_digest
row_major_flat_table
```

v3 -> v1 显式丢弃：

```text
v3_to_v1:expand_column_blocks_to_flat_rows_and_drop_compression
v3_to_v1:drop_bitmap_verification_metadata
```

报告：`artifacts/demo/normal-run/case-001/committed/reports/migration-w002.json`。

## 6. 写入失效与增量重算

默认 4 个行区间：

```text
R00:0-3, R01:3-6, R02:6-9, R03:9-12
```

写入版本 1 影响 `R01:3-6`。冷缓存下四个区间均计算。

写入版本 2 影响 `R02:6-9`、`R03:9-12`，实际结果：

```text
recomputed_intervals = ["R02:6-9", "R03:9-12"]
reused_intervals     = ["R00:0-3", "R01:3-6"]
trigger_write_version = 2
```

重算与复用无交集，并集覆盖全部区间。阶段提交后的旧缓存 GC：

```text
removed_uncommitted_objects = 6
removed_uncommitted_fragments = 2
```

报告：`artifacts/demo/normal-run/case-001/committed/reports/recompute-w002.json`。

## 7. 跨视图裁决与 float64 自检

独立裁决命令：

```bash
PYTHONPATH=. python3 -m truth_ledger.cli adjudicate \
  --case-dir artifacts/demo/normal-run/case-001
```

实际结果：

```json
{
  "ok": true,
  "struct_equal": true,
  "timestamp_aligned": true,
  "numeric_equal": true,
  "cache_hashes_equal": true,
  "max_abs_spmm_diff": 0.0,
  "max_abs_spmspm_diff": 0.0
}
```

自检确认：

```text
cache_hashes_reproduced = true
spmm_elementwise_equal = true
spmspm_elementwise_equal = true
spmm_max_abs_diff = 0.0
spmspm_max_abs_diff = 0.0
float_dtype = float64
```

日志：

- `artifacts/validation/adjudication.log`
- `artifacts/validation/self-check.log`

## 8. 强制终止恢复

演示先完整提交写入版本 1 的四个阶段，然后在独立子进程中执行写入版本 2，并在第一个新区间重算对象落盘后触发 `SIGKILL`。

实际结果：

```text
crash_returncode = -9
pre_recovery_snapshot_version = 6
pre_recovery_stage = migrate_semantic
```

恢复时启动 GC：

```text
removed_uncommitted_objects = 1
removed_uncommitted_fragments = 1
```

恢复阶段状态：

```text
(1, parse_validate,          skipped_committed)
(2, migrate_semantic,        skipped_committed)
(3, incremental_recompute,   executed)
(4, result_summary,          executed)
```

最终快照版本为 8，自检通过。演示总报告：`artifacts/demo/demo_report.json`。

另外，`test_sigkill_after_uncommitted_write_publish_rolls_back` 覆盖更严格的崩溃窗口：子进程在阶段 1 已写出 `committed/csr.json`、但尚未原子提交快照时被 `SIGKILL`。重启后从快照 4（写入版本 1）恢复 CSR/lineage，重新执行写入版本 2 的阶段 1/2/3/4，测试通过。

## 9. 异常注入

命令：

```bash
PYTHONPATH=. python3 -m truth_ledger.cli inject --out artifacts/injections-final
```

实际结果：

```text
total = 14
passed_count = 14
failed_count = 0
```

已验证业务码：

```text
E_STRUCT
E_VERSION_UNSUPPORTED
E_FORMAT
E_INDEX_DUP
E_SHARD_BOUND
E_BITMAP_COVER
E_FIELD_MISSING
E_PARSE
E_WRITE_CONFLICT
```

每条注入的结构化错误和阶段记录在：

- `artifacts/injections-final/injection_records.json`
- 各失败案例工作区的 `stage_records.jsonl`

## 10. 复现说明

以上 `artifacts/` 中的报告和日志均由当前代码在本地实际生成。删除 `samples/` 或 `artifacts/demo/` 后重新运行 `bash scripts/verify.sh` 可确定性复现。
