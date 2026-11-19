# 实际验证记录

验证环境：

- Linux/WSL2 工作区，Python 3.10.12
- SQLite 3.37.2
- 无第三方 Python 包
- 所有领域时间只来自样本逻辑时钟字段

## 主样本端到端

命令：

```bash
python -m obs_workbench --db data/final.sqlite3 load --topology samples/topology.json
python -m obs_workbench --db data/final.sqlite3 ingest --events samples/events.json
python -m obs_workbench --db data/final.sqlite3 status --output /tmp/final_before.json
python -m obs_workbench --db data/final.sqlite3 apply --patch samples/patch.json
python -m obs_workbench --db data/final.sqlite3 status --output /tmp/final_after.json
python -m obs_workbench --db data/final.sqlite3 verify
```

结果摘要：

```text
load:  topology v1
ingest: 20 events, 0 skipped
status before patch:
  published topology = 1
  closed windows = 7
  open windows = 6
  late ledger entries = 3
  canonical status SHA-256 =
    7499bb26e0aae9ad189e9259b8240df596c2c2ea5cd858f4480eab460f1ac473
apply: invalidations_processed = 6
status after patch:
  published topology = 2
  closed windows = 7
  open windows = 6
  late ledger entries = 3
  invalidation records = 10
  canonical status SHA-256 =
    6cc5a353938444d212e278bbf5ca177a656a134388e32725367b4b73aad2b70e
verify: ok = true
```

已关闭窗口的最终结论：

| 实例 | 规则 | 窗口起点 | 当前版本 | 历史版本 | 结论 | 原始/去重/抑制 | 代表事件 | 再裁决迟到事件 |
|---|---|---:|---:|---|---|---:|---:|---|
| bill-1 | QueueDepth | 0 | 1 | 1 | ACTIONABLE | 1/1/0 | 50 | - |
| edge-1 | RuleThreshold | 0 | 2 | 1,2 | ACTIONABLE | 1/1/0 | 42 | - |
| fe-1 | RequestError | 0 | 2 | 1,2 | ACTIONABLE | 1/1/0 | 40 | - |
| inv-1 | DiskLatency | 0 | 2 | 1,2 | ACTIONABLE | 3/3/0 | 31 | 31 |
| pay-1 | CPUThrottle | 0 | 2 | 1,2 | ACTIONABLE | 1/1/0 | 60 | - |
| pay-2 | CPUThrottle | 0 | 2 | 1,2 | ACTIONABLE | 5/4/0 | 11 | 13 |
| pay-2 | CPUThrottle | 60000 | 2 | 1,2 | SUPPRESSED_ONLY | 2/2/2 | null | 23 |

关键预期：

- `pay-2/CPUThrottle/[0,59999]` 的重复事件 14/15 被合并，原始 4 条、去重 3 条。
- `inv-1/DiskLatency/[0,59999]` 在 v1 中代表事件是 30，事件 32 被静默抑制；撤销静默并用迟到事件 31 再裁决后，代表事件变为 31，而不是直接提升 32。
- `pay-2/CPUThrottle/[60000,119999]` 新增静默窗口后两条事件均被抑制，结论为 `SUPPRESSED_ONLY`。
- `bill-1/QueueDepth/[0,59999]` 未进入任何失效闭包，当前结论和证据仍为拓扑 v1，历史只有一条修订。
- `edge-1` 与 `fe-1` 的依赖证据被重建；其结论动作是 `REUSE`，迟到簿记动作是 `REUSE`。

失效动作分布：

```text
edge-1 closed: conclusion=REUSE, evidence=REBUILD, late=REUSE
fe-1   closed: conclusion=REUSE, evidence=REBUILD, late=REUSE
inv-1  closed: conclusion=REBUILD, evidence=REBUILD, late=RECHECK
pay-1  closed: conclusion=REBUILD, evidence=REBUILD, late=RECHECK
pay-2  closed: conclusion=REBUILD, evidence=REBUILD, late=RECHECK
open windows: DEFER_TO_CLOSE
```

## 强制终止恢复

### 补丁传播中断

```bash
OBS_FAILPOINT=apply_after_invalidation_2 \
  python -m obs_workbench --db data/interrupted.sqlite3 apply --patch samples/patch.json
```

进程退出码为 `97`。中断后：

```text
published topology = 1
current_complete = false
pending job = apply:v2:46c6dc4e6dc2d71a
可见结论版本 = {1}
2 个失效项 DONE，4 个失效项 PENDING
```

执行：

```bash
python -m obs_workbench --db data/interrupted.sqlite3 resume
python -m obs_workbench --db data/interrupted.sqlite3 verify
```

恢复后的 `status` 与无中断运行逐字节一致，`verify.ok=true`。

### 窗口关闭中断

```bash
OBS_FAILPOINT=ingest_after_window_close \
  python -m obs_workbench --db data/close-crash.sqlite3 ingest --events samples/events.json
```

进程退出码为 `97`。中断后数据库中没有部分事件、水位或窗口：

```text
windows = 0
watermarks = 0
late_ledger = 0
```

随后执行 `resume` 和相同 `ingest`，20 条事件全部重新处理一次；最终 `status` 与干净运行逐字节一致，没有重复计入。

## 自动化测试

```text
$ python -m unittest discover -s tests -v
test_crash_during_patch_propagation_resumes_to_identical_output ... ok
test_crash_during_window_close_rolls_back_and_replays_without_double_count ... ok
test_parallel_apply_and_ingest_are_idempotent ... ok
test_bucket_isolation_lets_other_instances_progress ... ok
test_invalid_inputs_have_distinct_codes_and_no_writes ... ok
test_invalid_topology_does_not_create_database ... ok
test_window_digest_mismatch_is_detected ... ok
test_event_time_windows_dedup_evidence_and_verify ... ok
test_patch_invalidation_history_reuse_and_late_reaudit ... ok
test_reingest_is_idempotent ... ok
test_removed_silence_releases_flag_bound_suppression ... ok
test_late_event_after_patch_does_not_rewrite_closed_revision ... ok

Ran 12 tests in 10.207s

OK
```

```text
$ python -m compileall -q obs_workbench tests workbench.py
compile-ok
```

## 重复执行稳定性

- 同一事件文件重复 ingest：`processed_events=0`，`skipped_events=20`，`status` 字节不变。
- 同一补丁重复 apply：返回 `idempotent=true`，`status` 字节不变。
- 两个进程同时 ingest 同一文件：处理后数量为 `{0, 20}`，无重复事件。
- 两个进程同时 apply 同一补丁：一个执行传播，一个返回幂等；最终 `verify.ok=true`。
- 固定样本在全新数据库中重复执行，状态 JSON SHA-256 保持一致。
