# 实际验证记录

验证日期：2026-11-23。所有数据由固定 seed 本地生成，无外部数据集或服务。

## 1. 环境

主验证环境：

```text
Python 3.11.18
NumPy 2.4.6
SciPy 1.17.1
Linux 6.18.33.2-microsoft-standard-WSL2 x86_64
```

兼容性复验环境：

```text
Python 3.10.12
NumPy 1.26.4
SciPy 1.13.1
```

隔离环境创建：

```bash
uv python install 3.11
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

## 2. 单元与集成测试

命令：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

结果：

```text
Ran 38 tests in 1.943s
OK
```

覆盖项：

- 5 类 CSR/形状/dtype/nnz 校验错误，以及非整数 indptr 不得静默截断。
- `E_WRITE_STALE`、`E_WRITE_CORRUPT`、`E_WRITE_RANGE`、`E_WRITE_CONFLICT`。
- 拒绝批次不改变账本、行状态、结果快照。
- 每次事件的分区覆盖与互斥。
- 未触及块对象身份和摘要复用。
- 重复内容写入推进版本但结果摘要不变。
- 失效行变空行。
- SpMM/SpMSpM 与 float64 稠密参考一致。
- 自检可检测篡改块并返回 `E_INCONSISTENT`。
- 快照回退、损坏分类、整体前进替换、幂等导入。
- 非阻塞 `E_WRITE_BUSY`、读者等待原子换入、驻留批次/行缓冲/并行上限。
- 进程内精确恢复。
- 无检查点恢复返回 `E_RECOVERY_FAILED`，损坏检查点返回 `E_SNAPSHOT_CORRUPT`。
- 真实 `SIGKILL` 后恢复与全量执行检查点逐字节一致。

## 3. 完整 CLI 演示

命令：

```bash
.venv/bin/python -m sparse_inc.cli demo \
  --samples samples --reports reports --runs runs --seed 20261123
```

结果：

```text
business_code: OK
passed: true
checks: 26
checks_passed: 26
```

### 数值偏差

| 案例 | 最大绝对误差 | 容差 | 结果 |
| --- | ---: | --- | --- |
| `spmm_inc_demo` | 8.881784197001252e-16 | abs 1e-11, rel 1e-11 | 通过 |
| `spspmm_inc_demo` | 4.440892098500626e-16 | abs 1e-11, rel 1e-11 | 通过 |

### 增量区间

SpMM 最终分区：

```text
重算过（被写入触及）:
  [0,4) [4,8) [12,16)
从未触及（持续复用）:
  [8,12) [16,20)
```

SpMSpM 最终分区：

```text
重算过（被写入触及）:
  [0,4) [4,8) [12,16)
从未触及（持续复用）:
  [8,12) [16,18)
```

重复写入批次 `spmm_w05` / `spspmm_w04` 被接受，事件记录 `result_changed=false`。

## 4. 拒绝写入路径

`reports/spmm_case_report.json`：

```text
E_WRITE_STALE   ledger_unchanged=true  result_unchanged=true
E_WRITE_CORRUPT ledger_unchanged=true  result_unchanged=true
```

`reports/component_checks.json` 还记录了：

- `E_SHAPE_INVALID`
- `E_DTYPE_UNSUPPORTED`
- `E_CSR_STRUCT`（非单调 indptr）
- `E_CSR_STRUCT`（列索引越界）
- `E_NNZ_MISMATCH`

## 5. 快照与资源

```text
E_SNAPSHOT_ROLLBACK: version 2 -> 当前 version 5，拒绝且状态不变
E_SNAPSHOT_CORRUPT:  row_results v5 校验和损坏，类别/版本明确
E_RESOURCE_LIMIT:    strict 行缓冲上限 1，账本不变
degraded:            row_buffer_limit_chunked
E_WRITE_BUSY:        非阻塞立即返回，账本不变
```

测试还验证了 `parallel_interval_limit_capped`。

## 6. 强制终止与恢复

`reports/recovery_demo.json`：

```text
process_return_code: -9
sigkill_observed: true
uncommitted_artifact_left: true
complete_exit_report_generated: false
discarded_pending: true
pending_artifact_removed: true
loaded_version: 2
loaded_next_write_index: 2
checkpoint_bytes_equal: true
ledger_equal: true
result_equal: true
snapshot_envelopes_equal: true
```

全量执行与“SIGKILL 后恢复继续”生成的 `checkpoint.json` 原始字节一致：

```text
bundle_checksum:
sha256:4506da4431e455762f7880a5227b9dc1faaae91bc995bf805fb8e21bb533892e
```

该摘要对应本次固定 seed 验证；不同 NumPy/SciPy 版本可能产生不同但各自确定的结果。

## 7. 关键产物

- `samples/cases.jsonl`
- `samples/write_batches.jsonl`
- `reports/demo_report.json`
- `reports/spmm_case_report.json`
- `reports/spspmm_case_report.json`
- `reports/component_checks.json`
- `reports/recovery_demo.json`
- `runs/recovery_full/checkpoint.json`
- `runs/recovery_crash/checkpoint.json`

## 8. 复验命令

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m sparse_inc.cli demo \
  --samples samples --reports reports --runs runs --seed 20261123
.venv/bin/python scripts/recovery_demo.py \
  --manifest samples/cases.jsonl --reports reports --runs runs
```
