# 实际验证记录

验证时间：2026-11-27；环境：WSL Ubuntu 22.04，Python 3.10.12，仅使用标准库。

## 1. 自动化测试

执行：

```bash
python3 -m unittest discover -s tests -v
```

实际结果：

```text
Ran 8 tests in 0.882s

OK
```

完整输出保存在 `artifacts/test-results.txt`。

覆盖点：

- 样本加载、分区映射和基线水位；
- 六类非法输入的独立错误码，且失败时不创建 SQLite；
- 补丁重复刷新逐字节一致；
- 预算不足与并发上限两类调度结局；
- v2 增量包、失效闭包、未受影响分区哈希不变和版本回退码；
- 真实 `SIGKILL` 后的 prepared 检查点恢复与 `verify`；
- 四进程并行处理不同订阅者；
- `load/merge/diagnose/patch` 三种命令次序产生相同稳定状态 JSON。

## 2. 一键验收

执行：

```bash
python3 scripts/run_acceptance.py
```

实际输出：

```json
{
  "ok": true,
  "report": ".../artifacts/acceptance-report.json"
}
```

报告见 `artifacts/acceptance-report.json`。

## 3. 基线水位

默认注入合并时钟 `5000ms`，诊断 as-of 时钟 `8000ms` 时，实际结论为：

| 分区 | watermark | 结论 | 阻塞码 |
|---|---:|---|---|
| `inventory/0` | 2 | deliverable | — |
| `inventory/1` | 1 | blocked | `WB_WM_MISSING_SEMANTICS` |
| `orders/0` | 2 | deliverable | — |
| `orders/1` | 1 | blocked | `WB_WM_TOMBSTONE` |
| `orders/2` | 1 | blocked | `WB_WM_UPSTREAM_WATERMARK` |
| `payments/0` | 2 | deliverable | — |
| `payments/1` | 2 | deliverable | — |

基线生成 13 个分组补丁；4 个订阅者游标分别推进到主题变更序列末端。重复执行 `patch` 返回 `replayed=true` 和同一组补丁 ID/JSON。

## 4. 增量失效闭包

应用 `samples/packages/pkg-002-invalidation` 并以 `--clock-ms 9000` 诊断后，实际闭包根为：

```text
inventory/1/1
payments/0/0
payments/1/1
```

实际受影响分区：

```text
inventory/1
orders/0
orders/1
orders/2
payments/0
payments/1
```

实际复用分区：

```text
inventory/0
```

`inventory/0` 的 `content_hash` 在补丁前后相同；受影响分区结论变为：

- `payments/0=0`，`WB_WM_TOMBSTONE`；
- `payments/1=1`，`WB_WM_STALE_GENERATION`；
- `inventory/1=2`，缺失语义被补齐；
- `orders/2=2`，上游 `inventory/1` 依赖恢复；
- `orders/0=0`，被 `payments/0` 墓碑依赖拖低；
- `orders/1=1`，自身墓碑仍阻塞。

回退包 `samples/packages/pkg-009-rollback` 的实际结果：

```text
exit_code=3
error.code=WB_SNAPSHOT_VERSION_ROLLBACK
```

`pkg-003-full-replacement` 验证 `decision=replace`、`mode=full`。

## 5. 调度约束

默认预算 `100000ms`、每分区并发上限 `8`：

```json
{"started_succeeded": 5, "started_failed": 5, "budget_unstarted": 0, "voluntarily_abandoned": 0}
```

预算 `250ms`、并发上限 `8`：

```json
{"started_succeeded": 1, "started_failed": 1, "budget_unstarted": 1, "voluntarily_abandoned": 7}
```

预算充足、每分区并发上限 `1`：

```json
{"started_succeeded": 4, "started_failed": 3, "budget_unstarted": 0, "voluntarily_abandoned": 3}
```

所有已启动任务均为 `non_preemptive=true`，按完整成本扣费并运行到结束。

## 6. SIGKILL 恢复

真实执行：

```bash
WB_CRASH_POINT=checkpoint_after_prepare \
  python3 workbench diagnose --db state/acceptance-crash.sqlite3
```

进程实际返回 `SIGKILL` 退出码 `-9` / shell 中常见 `137`。此时 `status` 显示：

```json
{
  "has_half_committed_record": true,
  "prepared_checkpoint_ids": ["CP-..."]
}
```

执行：

```bash
python3 workbench resume --db state/acceptance-crash.sqlite3
python3 workbench verify --db state/acceptance-crash.sqlite3
```

实际恢复 1 个 prepared 检查点，随后 verify：

```json
{"ok": true, "field_diff_count": 0}
```

恢复后有 7 条水位、4 条游标、13 条补丁和 1 个诊断运行，没有重复计数。

## 7. 多进程一致性

测试同时启动 4 个 `diagnose --subscriber-id ...` 进程，实际提交结果：

```text
partition_commit_ledger = 7
cursor_commit_ledger = 4
subscription_cursors = 4
cursor_revision <> 1 的记录数 = 0
```

同一订阅者并发执行 4 个相同 `patch --subscriber-id sub-payments` 时，只产生 1 条游标账本和 `cursor_revision=1`。

## 8. verify 与次序一致性

执行：

```bash
python3 workbench verify
```

会在 `state/verify-*` 临时库重跑 `load -> diagnose -> patch`，逐字段比较水位、游标、补丁和诊断任务，并复查当前 SQLite 中历史视图重算结果。实际结果：

```json
{
  "ok": true,
  "field_diff_count": 0,
  "hash_mismatches": [],
  "current_database": {"ok": true, "mismatch_count": 0}
}
```

`test_command_orders_produce_bitwise_same_status` 对三种合法命令次序的完整规范化状态 JSON 做相等断言，结果通过。
