# 技术设计

## 组件

```text
JSON 声明/样本
   │ 强校验、整体加载
   ▼
Declaration ───────────────┐
   │                       │
   │ DispatchScheduler     │ 本地权威重算
   │ 全局/每集群双配额      │ compute_aggregate
   ▼                       ▼
DispatchClient ── JSONL ──> ClusterSimulationServer（每集群一个子进程）
   │                       │ 按 config_version 聚合
   └──────── SQLite Ledger ┘
              │
              ├─ Dashboard 快速缓存视图
              ├─ AuthorityVerifier / ReplayVerifier
              └─ Snapshot + Recovery
```

核心模块：

| 模块 | 职责 |
|---|---|
| `config.py` | 声明和样本强校验、字段路径、声明指纹 |
| `deterministic.py` | 规范化 JSON、SHA-256/sum/count 聚合、任务级摘要 |
| `server.py` / `client.py` | 回环 JSONL 服务与带超时/重试的客户端 |
| `service_manager.py` | 子进程启动探活、端口检查、清理 |
| `scheduler.py` | 双配额门槛调度、分发交互记录 |
| `ledger.py` | SQLite 事务、版本纪元、校验和、恢复状态 |
| `dashboard.py` | 修订号快速路径与整版缓存 |
| `verifier.py` | 本地重算、账本完整性、视图一致性 |
| `metrics.py` | 定点延迟统计和量化误差上界 |
| `snapshot.py` | 原子快照、恢复、重放、整库替换 |
| `diagnostics.py` | 三类异常注入 |
| `ui.py` | Tkinter 界面 |

## 确定性计算

聚合输入只包括集群标识、节点集合、配置版本、配置键、聚合方式和样本对象。节点与配置键先排序，使用 `json.dumps(..., sort_keys=True, separators=(',', ':'), allow_nan=False)` 规范化，再计算 SHA-256。请求响应中的耗时不属于摘要。

`components` 以配置键为粒度。权威重算发现差异时，按排序后的键找第一个不同项，因此报告能给出稳定的“首个差异键”。若组件完全相同但总摘要不同，则首差键为 `$summary`。

服务端启动时重新加载同一份声明和样本，仅服务声明中的当前集群版本。旧版本真值重算由本地验证器按账本行版本执行；这样版本升级后仍能核验历史行。

## JSONL 与 TCP 生命周期

每次分发建立一条短回环连接，发送一行规范化 JSON，接收一行 JSON 后结束。客户端发送完成后半关闭写方向；服务端写完响应后消费该 FIN 再关闭，使服务端监听端口避免进入 TIME_WAIT，支持快速重启。

客户端超时按单次尝试应用，`retries` 是额外重试次数。服务端版本不匹配返回 `E_VERSION` 且客户端不重试；网络错误和超时会继续尝试直到上限。服务端所有异常均转为结构化 JSON 错误行。

## 调度与配额

调度器先建立 `(task, target_cluster)` 单元队列。调度循环只在同时满足以下条件时分派：

```text
active_global < limits.global_concurrency
active_for_cluster < min(limits.per_cluster_concurrency, cluster.per_cluster_concurrency, global)
```

不满足时单元留在队列，绝不预先占用配额。测试和报告同时记录观测峰值；样本声明应满足每集群峰值不高于配额。

每次成功分发把服务摘要写入账本；失败写入 `dispatch` 交互并标记该单元恢复状态为 unknown。`started/ended` 时间覆盖客户端首次尝试到最后一次，队列等待单独放在交互详情中。

## SQLite 事务与版本纪元

主要表：

- `results`：当前与历史结果，唯一键为任务+集群+配置版本。
- `interactions`：dispatch/query/verify 的起止纳秒、数据版本、超预算标志和结构化详情。
- `declaration_state`：每个集群最后接受的版本。
- `version_transitions`：稳定记录 `old -> new`。
- `recovery_state`：未提交单元为 `unknown`，要求重跑。
- `meta`：修订号、声明指纹、延迟口径版本。

`reconcile` 在一个 `BEGIN IMMEDIATE` 事务内检查回退；版本增加时先设置旧结果和旧交互 `is_stale=1`，写迁移记录和新声明状态，再提升修订号。提交后查询无法看到中间状态。

结果写入同样事务化。每行校验和覆盖任务、集群、版本、聚合方式、摘要、组件、耗时、起止时刻和声明版本，不覆盖 `is_stale`，因此定期陈旧化无需修改校验和。条目标记为陈旧后再修改摘要而不更新校验和时，权威重算首先得到 `E_LEDGER`。

## 快速视图与权威路径

快速路径只执行一个修订号查询。命中时返回完整缓存的深拷贝；未命中时用只读事务一次性读取结果、迁移和恢复状态，构建完整不可变视图。数据版本为 `declaration_version:r<revision>`。

版本迁移后修订号改变，缓存整版重建。视图仅接受“未陈旧且版本等于当前声明”的行，因此即使在迁移事务与下一次分发之间查询，也只会出现新版本 pending/unknown 状态，不会显示旧摘要。

权威路径不读界面摘要作为真值。它按账本行内版本重算，再额外比对视图结果，区分账本损坏与展示不一致。延迟统计从 `interactions` 读取；当前统计过滤陈旧交互，历史统计保留全部并单列超预算事件。

## 快照、恢复、重放

快照写入路径：

```text
build payload -> canonical checksum -> temp file -> flush/fsync -> os.replace -> fsync directory (best effort)
```

恢复遵守以下规则：

- 当前声明指纹与快照相同：SQLite 中完整提交的当前版本结果才算完成，不信任快照的“已完成”声明。
- 当前声明不同：快照状态整体替换账本，返回 `merged=false`；随后以快照声明重建工作模型。
- 每个缺失单元写 `unknown`，报告 `clean_shutdown=false` 和空 `exit_report`。
- 重放不调用集群服务，直接用快照配置版本重算；同时重建任务级摘要和延迟统计。

## 异常码边界

- `E_CONFIG`：输入契约问题，发生在任何持久化之前。
- `E_PORT`：绑定/探活失败。
- `E_VERSION`：历史回退、服务版本不符、快照声明差异告警。
- `E_LEDGER`：SQLite 行或快照校验和失败。
- `E_VERIFY`：按版本重算与账本/视图不一致。
- `E_LATENCY`：查询成功但超过显式预算，属于已记录的可观测事件。
- `E_REPLAY`：恢复后重放不可复现。

