# 实际验证报告

验证日期：2026-11-17（Asia/Shanghai）

## 环境

```text
Python 3.11.0rc1
SQLite 3.37.2
Linux 6.18.33.2-microsoft-standard-WSL2
Tkinter: 当前镜像未安装（ModuleNotFoundError）
```

目标运行环境是 Windows 官方 Python 3.11+。本镜像用于验证无界面核心、真实回环多进程、SQLite、快照和重放；Tkinter 入口已通过 `py_compile` 和独立模型层测试，但未能在无 Tk/无显示环境启动窗口。

## 自动化测试

命令：

```bash
python3 -m unittest discover -s tests -v
```

实际输出末尾：

```text
Ran 24 tests in 5.405s

OK
```

完整输出：`artifacts/verification/tests.txt`。

## 三阶段演示

命令：

```bash
python3 -m edgecache.demo --base-port 28180 \
  --state-dir artifacts/verification/runtime \
  --json-out artifacts/verification/demo-output.json
```

结果：进程退出码 `0`，耗时 `5.623s`，14/14 断言通过，stderr 为空。

```text
PASS stage1_capacity_and_eviction
PASS stage1_hit_and_ttl_decrement
PASS stage2_stable_topological_propagation
PASS stage2_entry_versions_incremented
PASS stage2_all_nodes_replaced_affected_entries
PASS stage3_policy_full_replacement
PASS stage3_all_old_entries_removed
PASS snapshot_restore_detected_version_difference
PASS snapshot_restore_whole_cache_signature
PASS snapshot_replay_verified
PASS forced_node_recovery_marks_unknown
PASS forced_node_recovery_keeps_committed_entries
PASS forced_source_recovery_from_ledger
PASS forced_termination_no_exit_report
```

### 阶段 1：冷启动

- 三个节点均运行：`edge-a`、`edge-b`、`edge-c`。
- 每节点填充 6 次，容量 4，最终缓存条目数均为 4。
- 每节点产生 2 条容量逐出事件。
- 命中 `/assets/app.css` 后，三个节点的剩余 TTL 均小于原始 `30000ms`，证明命中按经过毫秒递减。

### 阶段 2：内容演进与派生失效

触发：`/assets/base.css` 的 ETag `"base-v1" -> "base-v2"`。

稳定拓扑顺序：

```text
0  /assets/base.css          trigger
1  /assets/app.css           direct-dependent
2  /assets/app.manifest.json indirect-dependent
```

三个节点均把受影响条目替换为新版本；`/assets/base.css` 在三节点上的条目版本均从 `1` 递增到 `2`，派生路径 `/assets/app.css` 与 `/assets/app.manifest.json` 的条目版本也递增到 `2`。

### 阶段 3：策略整体替换

从 `policy-v1` 切换到 `policy-v2`：

- 键字段：`path + tenant` -> `path + tenant + encoding`。
- 容量：4 -> 2。
- 逐出：LRU -> FIFO。
- 旧账本版本上的 12 个条目全部移除。
- 三个节点最终均为 2 个条目，策略版本均为 `policy-v2`。

## 快照整体替换与重放

阶段 2 结束时捕获 `checkpoint-ledger-2.json`；阶段 3 后恢复该快照。

版本差异：

```text
ledger_version: current=3, snapshot=2
policy_version: current=policy-v2, snapshot=policy-v1
content_set_hash: 相同
```

恢复操作：

```text
strategy: full_replace
incremental_merge_used: false
derivations_replaced: true
removed_current_entries: 6
inserted_snapshot_entries: 12
replaced_entries: 18
new monotonically increasing ledger version: 4
```

恢复后缓存签名与阶段 2 逐条一致（节点、缓存键、路径、ETag、条目版本），说明没有增量合并残留。

重放校验：

```text
verified_batches: 1
verified_traces: 3
verified_cache_entries: 12
verified_content_paths: 5
```

## 强制终止恢复

先在 `edge-c` 创建未提交写入意图，再强杀进程并重启：

```text
node kill: exit_code=-9, graceful=false, exit_report_generated=false
restart: restored_entries=4, rerun_required=true
unknown intent status=unknown
```

源站强杀并重启：

```text
source kill: exit_code=-9, graceful=false, exit_report_generated=false
ledger version before=4, after=4
```

重启源站时故意传入两个不存在的启动文件路径；源站仍从 SQLite 账本恢复成功，证明已有账本时不依赖样本文件。

最后正常关闭的四个进程均返回 `exit_code=0`、`graceful=true`。

## 错误注入

### E_POLICY

```bash
python3 -m edgecache.demo --scenario bad-policy
```

进程码 `21`：

```json
{
  "error": {
    "code": "E_POLICY",
    "location": "$.key_fields[2]",
    "message": "duplicate cache key field 'tenant'"
  },
  "ok": false,
  "returned_code": "E_POLICY"
}
```

### E_CYCLE

```bash
python3 -m edgecache.demo --scenario cycle
```

进程码 `22`：

```json
{
  "error": {
    "code": "E_CYCLE",
    "location": "$.contents[3].derived.sources",
    "message": "derived relationship contains a cycle: /assets/app.css -> /assets/app.manifest.json -> /assets/app.css"
  },
  "ok": false,
  "returned_code": "E_CYCLE"
}
```

### E_PORT

```bash
python3 -m edgecache.demo --scenario port-occupied --base-port 28190
```

进程码 `20`。结构日志记录：

```json
{"code":"E_PORT","component":"source","details":{"errno":98,"host":"127.0.0.1","os_error":"Address already in use","port":28190},"event":"business_error","level":"ERROR","location":"127.0.0.1:28190","message":"failed to bind loopback TCP port; no alternate port will be tried"}
```

`alternate_port_retry_attempted=false`。

## 证据文件

- `artifacts/verification/demo-output.json`：完整演示输出（含三阶段节点状态、快照恢复、强杀恢复）。
- `artifacts/verification/acceptance-summary.json`：验收指标摘要。
- `artifacts/verification/demo-stdout.txt`：演示 stdout 原样捕获。
- `artifacts/verification/demo-stderr.txt`：空文件，表示正常演示无 stderr。
- `artifacts/verification/tests.txt`：24 项测试完整输出。
- `artifacts/verification/checkpoint-ledger-2.json`：阶段 2 快照。
- `artifacts/verification/errors/*.json`：三种错误注入结果。
- `artifacts/verification/logs/*.jsonl`：源站、三个节点及端口失败场景的结构化日志。
