# 实际验证记录

验证日期：2026-11-26  
环境：Ubuntu 22.04 / WSL，Python 3.10.12，SQLite 3.37.2。

## 1. 确定性样本

```bash
python3 generate_samples.py --out samples
python3 generate_samples.py --out /tmp/samples-repro-424
```

两份样本目录的全部文件 SHA-256 一致。样本包含 3 张表、12 列、16 行、
5 个索引、12 个事务和 19 个业务操作。

策略摘要：

```text
v1: 7014942e412469baf5954ddd193ce6c834a83db3dc6143d9d7166b570afb5668
v2: ca3668cdc898a6c7887ff5edaa94db1e4d8869d58841b2ca8522947daa7e48bf
```

## 2. 正常执行

```bash
python3 main.py load --samples samples/
python3 main.py run --samples samples/
python3 main.py status --samples samples/
```

结果为 `COMPLETED`，提交水位 71，活动策略为 v2，账本版本 2：

```json
{
  "active_ledger_version": 2,
  "archived_ledger_versions": [1],
  "closed_entry_count": 6,
  "entry_count": 6,
  "ledger_versions": [1, 2],
  "max_cumulative_error_bound_ns": 1500,
  "open_entry_count": 0,
  "total_wait_ms": "4.5514",
  "total_wait_ns": 4551000
}
```

最终事务裁决：

```text
T01 COMMITTED
T02 COMMITTED
T03 COMMITTED
T04 DEADLOCK_VICTIM_ROLLBACK
T05 COMMITTED
T06 ABANDONED_WAIT_TIMEOUT
T07 COMMITTED
T08 COMMITTED
T09 MANUAL_ROLLBACK
T10 COMMITTED
T11 COMMITTED
T12 NOT_STARTED_WITHIN_BUDGET
```

关键事实：

- T04/T03 在 `700,400 ns` 形成死锁环，当前 3 位标度牺牲 T04；
  提升到 4 位会牺牲 T03，`flip = true`。
- T06 在 `1,999,500 ns` 达到 2 ms 定点预算并超时；自身误差上界 500 ns，
  持有依赖上界 500 ns，累计 1,000 ns。
- T10 在 v2 生效后绑定策略代 2，等待 500,400 ns，预算 1,000,000 ns，
  精度敏感但 `flip = false`。
- T12 的 `start_at_ms = 75` 超出 10 ms 总逻辑预算，分类为
  `NOT_STARTED_WITHIN_BUDGET`。

执行摘要：

```text
state_digest:  f4dfe7ab5d3f33a8f0430b5edf51c2b11dafbb8ac0202ce8d32524d16ded0e31
ledger_digest: de9719d4e1dbe95f11b3b7f691c06b9b497460d4558f373259cb52134000f3b7
```

## 3. 真实 SIGKILL 恢复

```bash
python3 main.py load --samples samples/ --db /tmp/kill.sqlite3
python3 main.py run --samples samples/ --db /tmp/kill.sqlite3 \
  --crash-after-step 20
python3 main.py resume --samples samples/ --db /tmp/kill.sqlite3
python3 main.py verify --samples samples/ --db /tmp/kill.sqlite3
```

`run` 在第 20 步检查点提交后收到 `SIGKILL`，shell 返回码 137。恢复结果为：

```json
{
  "status": "COMPLETED",
  "resume_count": 1,
  "committed_step": 71,
  "state_digest": "f4dfe7ab5d3f33a8f0430b5edf51c2b11dafbb8ac0202ce8d32524d16ded0e31",
  "ledger_digest": "de9719d4e1dbe95f11b3b7f691c06b9b497460d4558f373259cb52134000f3b7",
  "verify_ok": true,
  "recovery_all_match": true
}
```

微步日志为连续的 `0..71`，无重复步；恢复登记为
`(resume_seq=1, resumed_from_step=20, journal_count=21)`。

另在策略账本整体替换微步 `replace_policy_ledger`（第 48 步）之后执行同一
`SIGKILL` 测试，恢复后仍为 `COMPLETED`，22 项 `verify` 检查全部通过，
`state_digest` 同为
`f4dfe7ab5d3f33a8f0430b5edf51c2b11dafbb8ac0202ce8d32524d16ded0e31`。

## 4. verify 子命令

```bash
python3 main.py verify --samples samples/
```

全部检查通过：

```text
run_completed
journal_contiguous
snapshot_bundle_fingerprint
ledger_state_matches_sqlite
ledger_entries_closed
fixed_point_recalculation
no_open_ledger_at_completion
active_policy_pointer
ledger_version_chain
policy_commit_monotonicity
transaction_policy_bindings_valid
policy_summaries_recomputed
decision_sequence_contiguous
all_transactions_terminal
no_locks_after_completion
normalized_transaction_snapshot
normalized_precision_snapshot
normalized_policy_commit_snapshot
rollback_classification_consistency
precision_record_consistency
deterministic_fresh_replay
durable_checkpoint_recovery
```

恢复探针在第 `2, 18, 36, 54, 71` 步后模拟持久化崩溃并重新打开数据库，
五个探针的 `state_digest`、`ledger_digest` 和 `decision_digest` 均与全新
重放一致。

## 5. 单元测试

```text
python3 -m unittest discover -v

test_actual_sigkill_then_resume_and_verify ... ok
test_durable_recovery_probe_matches_fresh_run ... ok
test_end_to_end_ledger_and_rollback_decisions ... ok
test_repeated_runs_are_bitwise_identical ... ok
test_duplicate_table_reports_code_and_first_field ... ok
test_generator_is_byte_reproducible ... ok
test_negative_relative_budget_is_rejected ... ok

Ran 7 tests in 2.663s
OK
```
