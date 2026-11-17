# 实际验证记录

验证日期：2026-11-17。

## 环境

- Python：`3.11.0rc1`（满足 3.11+ 要求）。
- 运行方式：本地命令行，无容器、无网络访问、无第三方运行时依赖。
- 当前验收容器没有安装 `tkinter`，因此验证了可选导入和 `E_UI` 降级路径；Tkinter 可视化代码面向装有标准 `tkinter` 的 Windows Python。

## 自动化测试

执行：

```text
python3 -m unittest discover -v
```

结果：`Ran 26 tests in 0.723s`，`OK`。完整输出见 `artifacts/validation/unittest.txt`。

覆盖内容：

- 规则、拓扑、事件强校验及 `E_SPEC`/`E_TIME` 文件与行号。
- 乱序输入聚类字节稳定、不同 `PYTHONHASHSEED` 摘要一致。
- 同服务时间桥接、跨服务拓扑可达合并、非可达服务不合并。
- 根因排序、支撑告警、服务名最终决胜和传播告警折叠。
- 规则修改局部失效、未影响簇复用、全量等价、计数闭合。
- 边新增、边权重更新、规则删除、事件保留但失活。
- 删除仍被依赖服务的 `E_DEPEND`。
- 注入式 `E_INCR` 首个差异簇报告。
- SQLite 快照只读保留、版本差异、整体替换和逐簇重放。
- 快照版本缺失 `E_SNAP`、游标超前/结论篡改 `E_REPLAY`。
- 三场景演示重复运行结果字节一致及异常注入日志。

## 冷加载与全量聚类

命令：

```text
python3 -m alarm_domain demo
```

稳定结果：

```text
rules_version        d0551d1715c8862f93742c894de6d6bbf9ae5bbf972bfc41b4a55ac3aaf380da
topology_version     7598469f71141c2c7179fd6317e4ecad8128435ae256dec71c040444ef9abd6c
cluster_summary_hash 6d90a84c434734c52fbf7cace4dd093b0259a31be63f774d30890fbb956f8410
analysis_hash        ce3284ab388f94c84d6f4d406c387f961c71e1d78360ae80e2d0dda90c408a67
cluster_count        3
```

三个簇：

1. `cl-e275514f98f04dff`：7 个事件，服务 `gateway/inventory/ledger/orders/payments`，根因 `ledger`，折叠 5 条传播告警。
2. `cl-d76f280eb472e734`：2 个事件，服务 `mailer/smtp`，根因 `smtp`，折叠 1 条传播告警。
3. `cl-435883ada260ac22`：1 个事件，服务 `mailer`，根因 `mailer`。

冷加载快照：`snap-0001-5e9c05b662af`。

## 增量规则修改与局部重算

变更集 `samples/changeset-pay-severity.json` 仅把 `R-PAY-5XX` 从 `WARNING` 提升为 `CRITICAL`。

结果：

```text
affected_services     ["payments"]
invalidated_clusters  ["cl-e275514f98f04dff"]
recomputed_clusters   ["cl-e275514f98f04dff"]
reused_clusters       ["cl-435883ada260ac22", "cl-d76f280eb472e734"]
recomputed_count      1
reused_count          2
total_cluster_count   3
counts_consistent     true
reused_hashes_verified true
full_equivalence      true
root before           ledger
root after            payments
```

`recomputed_count + reused_count == total_cluster_count` 为真；未失效两个簇的规范对象保持不变；局部结果与全量重算无差异。增量后快照：`snap-0002-28a84bff01d9`。

## 快照恢复与重放

恢复冷加载快照 `snap-0001-5e9c05b662af`：

```text
version_changed_any  true
changed dimension    rules
whole_state_replaced true
incremental_merge    false
replay match         true
clusters compared    3
event cursor         10
cold snapshot        current=true, read_only=true
warm snapshot        current=false, read_only=true
```

恢复过程按快照规则、拓扑和游标重新聚类推导，3 个簇逐簇匹配；旧的两个快照均保留且可辨识版本，没有增量合并。

## 异常注入

### 时间戳回退

```text
python3 -m alarm_domain demo --inject-time-regression --work-dir artifacts/validation/time
```

结果：

```text
code    E_TIME
file    events.time-regression.ndjson
line    4
detail  event_id=e-led-001, previous_timestamp=1250, timestamp=1249
```

结构化日志：`artifacts/validation/time/anomaly-structured.jsonl`。

### 快照版本缺失

```text
python3 -m alarm_domain demo --inject-missing-snapshot-version --work-dir artifacts/validation/snapshot
```

结果：

```text
code    E_SNAP
file    demo-e-snap.sqlite3
detail  missing_fields=["rules_version", "topology_version"]
```

结构化日志：`artifacts/validation/snapshot/anomaly-structured.jsonl`。

## Tkinter 降级验证

```text
python3 -m alarm_domain ui --work-dir artifacts/validation/ui
```

当前容器缺少 `tkinter`，命令先生成正常演示产物，随后以 `E_UI` 报告：

```text
{"code":"E_UI","message":"tkinter is unavailable; install the standard Windows Python distribution","status":"error"}
```

退出码为 2。核心 API 和 `demo` 不依赖该模块，均正常通过。完整输出见 `artifacts/validation/ui-attempt.txt`。

## 产物索引

- `artifacts/demo/demo-output.json`：正常三场景完整稳定结果。
- `artifacts/demo/diagnostic.json`：最终诊断投影。
- `artifacts/demo/demo.sqlite3`：两个只读快照及版本元数据。
- `artifacts/demo/structured.jsonl`：正常演示结构化日志。
- `artifacts/validation/unittest.txt`：26 项测试完整记录。
- `artifacts/validation/summary.json`：机器可读的关键验收摘要。
- `artifacts/validation/demo-stdout.json`：验收运行 stdout。
- `artifacts/validation/time-injection.json`、`snapshot-injection.json`：异常注入结果。
