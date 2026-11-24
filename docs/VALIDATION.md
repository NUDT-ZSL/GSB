# 实际验证记录

验证时间环境：WSL2 / Ubuntu 工作区；默认 `python` 为 Python 3.10.12，另有 `python3.11` 为 Python 3.11.18，SQLite 3.37.2，pytest 8.2.0。题述 Windows 原生 Python 未通过当前沙箱的 `python.exe`/`py.exe` WSL 互操作探测到；Windows 锁实现使用 `msvcrt`，并提供 `scripts/validate.ps1`。

## 1. 单元与端到端测试

命令：

```bash
python -m pytest -v
```

结果：

```text
tests/test_guard.py::test_truth_markers_adjudication_and_report PASSED
tests/test_guard.py::test_parser_rejections_continue_valid_records PASSED
tests/test_guard.py::test_policy_time_tombstone_and_conflict PASSED
tests/test_guard.py::test_precision_uncertainty_has_two_paths PASSED
tests/test_guard.py::test_closure_is_order_independent_and_detects_cycle PASSED
tests/test_guard.py::test_lock_conflict_returns_e_lock PASSED
tests/test_guard.py::test_parameter_error_exit_code_two PASSED
tests/test_guard.py::test_layout_change_forces_full_rebuild PASSED
tests/test_guard.py::test_forced_kill_recovers_from_last_complete_credential PASSED
tests/test_guard.py::test_successful_module_rollback_after_recovery PASSED

10 passed in 0.82s
```

Python 3.11 兼容性冒烟：

```bash
python3.11 -m compileall -q memguard scripts/generate_sample.py
```

结果：`PY311_COMPILE_OK`。

## 2. 固定样本导入与处置

```bash
python -m memguard generate-sample --workdir .
python -m memguard import --workdir .
python -m memguard process --workdir .
```

实测摘要：

```text
import:
  events_valid=10
  regions_valid=10
  policy_candidates=27
  policy_keys=9
  policy_conflicts=3
  rejections=17

process:
  start_watermark=0
  final_watermark=10
  adjudicated_events=10
  skipped_events=0
  credential_count=7
  exit_code=1   # 样本故意包含非法记录，属于“部分成功”
```

按 `truth.json` 的 `sync_marker` 对照，10 个有效事件分别为：

| sync marker | 结论 | 业务码 | 凭证 |
| --- | --- | --- | --- |
| `sync-001-ro-append-w` | unauthorized | `E_OWNER_POLICY_DENIED` | `CRED-E00001` |
| `sync-002-text-remove-x` | unauthorized | `E_OWNER_POLICY_DENIED` | `CRED-E00002` |
| `sync-003-data-legal-downgrade` | compliant | `E_OK` | 无 |
| `sync-004-stack-external-x` | unauthorized | `E_EXTERNAL_INJECTION` | `CRED-E00004` |
| `sync-005-heap-append-x` | unauthorized | `E_OWNER_POLICY_DENIED` | `CRED-E00005` |
| `sync-006-cycle-a-write` | unauthorized | `E_OWNER_POLICY_DENIED` | `CRED-E00006` |
| `sync-007-cycle-b-write` | unauthorized | `E_OWNER_POLICY_DENIED` | `CRED-E00007` |
| `sync-008-boundary-uncertain` | uncertain | `E_ADJUDICATION_UNCERTAIN` | 无 |
| `sync-009-legacy-tombstone` | indeterminate | `E_POLICY_TOMBSTONED` | 无 |
| `sync-010-conflict-v2-authority` | unauthorized | `E_OWNER_POLICY_DENIED` | `CRED-E00010` |

## 3. 报告与全量重放

```bash
python -m memguard report --workdir .
python -m memguard verify-checkpoint --workdir . --full-replay
```

实测：

```text
report summary:
  adjudicated_events=10
  compliant=1
  unauthorized=7
  uncertain=1
  indeterminate=1
  credentials=7
  input_rejections=17
  policy_conflicts=3
  credential_cycles=1

verify:
  business_code=E_OK
  full_replay.consistent=true
  compared_events=10
  credential_count=7
```

报告文件：`reports/guard-report.json`。

## 4. 闭包与回滚

`app.exe` 模块闭包包含：

```text
CRED-E00001
CRED-E00002
```

执行：

```bash
python -m memguard rollback --workdir . \
  --credential-id CRED-E00001 --scope module
```

结果：退出码 `0`，两张凭证状态变为 `rolled_back`，`r-rdata` 的 `current_prot_mask` 恢复为 `1`。

环依赖：

```bash
python -m memguard rollback --workdir . \
  --credential-id CRED-E00006 --scope module
```

结果：退出码 `4`，业务码 `E_CRED_CYCLE`，环为 `["CRED-E00006", "CRED-E00007"]`。

## 5. 强杀恢复

测试 `test_forced_kill_recovers_from_last_complete_credential` 使用故障钩子在第 2 张凭证 SQLite 提交后、快照原子改名前发送 `SIGKILL`。实测断言：

```text
subprocess returncode in (-9, 137)
checkpoint.json.tmp exists
verify-checkpoint -> E_HALF_WRITE_RESIDUE, exit 4
process recovery:
  start_watermark=2
  adjudicated_events=8
  skipped_events=2
  credential_count=7
verify-checkpoint --full-replay -> E_OK, consistent=true
```

这证明强杀后从最后完整 SQLite 凭证水位继续，不重新裁决前 2 个事件，且恢复结果与全量重放一致。

## 6. 锁冲突

终端 A：

```bash
python -m memguard lock-hold --state-dir state --seconds 5
```

终端 B：

```bash
python -m memguard process --state-dir state
```

实测终端 B：

```text
exit_code=3
code=E_LOCK
message=workspace is locked by another memguard writer
```

pytest 中也有同进程持锁/第二获取方冲突的回归测试。
