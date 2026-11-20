# 设计与一致性说明

## 模块边界

| 模块 | 职责 |
| --- | --- |
| `dbpool.validation` | JSON 解析、字段校验、回环地址检查、摘要计算、租户标签模型 |
| `dbpool.storage` | SQLite schema、事务上下文、Windows/POSIX 文件锁 |
| `dbpool.scheduler` | 精确队列、分层桶队列、成本估计、切换决策 |
| `dbpool.engine` | 租约、配额、排队、回收、切换、恢复和状态报告 |
| `dbpool.replay` | 注入逻辑时钟的确定性轨迹回放 |
| `dbpool.cli` | `load/acquire/release/resume/status/verify` |
| `tools.echo_server` | 仅绑定回环地址的本地 TCP 回显/畸形/拒绝模式服务 |

## 租约发放事务

新物理连接需要外部 socket 副作用，因此流程拆为“意图 + 最终账本事务”：

1. 在文件锁内预检容量、租户配额、池标签和端点能力。
2. 若需要新建连接，单独提交一条 `operation_intents(state=OPENING)` 审计意图。
3. 执行回环建链与探针；失败则把意图标为 `ABORTED`，不产生租约或配额扣减。
4. 开启 SQLite `BEGIN IMMEDIATE`，在同一事务中写：
   - `connections` 行；
   - `leases` 行；
   - `requests` 申请水位；
   - `quota_accounts`（由连接明细在事务内重算）；
   - `pool_state`（逻辑时钟、状态版本、空闲摘要、总数、队列深度）；
   - 审计事件，并把意图更新为 `COMMITTED`。
5. 提交后才返回租约。进程在提交前被杀死时，SQLite 回滚整笔事务，OS 关闭已建
   socket；遗留 `OPENING` 意图会在下一次写操作或 `resume` 中识别并中止。

租约发放、连接存在性和配额账目因此不存在可见的中间态。`quota_accounts` 与
`pool_state` 的总数从连接明细重算，而不是依赖易重复的增量更新。

## 精确模式与分层模式

精确模式排序键：

```text
(idle_since_ms, endpoint_name, conn_id)
```

分层桶索引：

```text
age = now_ms - idle_since_ms
bucket_index = age // bucket_width_ms
```

分层模式按 `(pool_label, bucket_index)` 从大桶到小桶扫描；桶内仍使用与精确模式
相同的 `(idle_since_ms, endpoint_name, conn_id)` 排序。由于较大桶中所有连接的
空闲时长都严格大于较小桶中的连接，最终连接选择序列与精确排序完全相同。分层
模式只改变定位成本模型，不改变业务选择。

## 成本估计与切换

```text
exact_cost  = n * log2(n + 2) + 0.5 * queue_depth + 0.75 * label_span
tiered_cost = occupied_buckets + 0.2 * n + 0.25 * queue_depth + 0.4 * label_span
```

- 当前为 `exact`：仅当 `tiered_cost < exact_cost * (1 - margin)` 且
  `idle_count >= tiered_min_idle` 时切到 `tiered`。
- 当前为 `tiered`：若空闲数低于阈值，或 `exact_cost < tiered_cost * (1 - margin)`，
  切回 `exact`。
- 相同逻辑时钟序列下规则完全确定，不读取墙钟或随机数。

`mode_switch` 记录触发指标、两模式成本、切换点逻辑时钟、被否决模式及原因。
`tolerance.idle_age_ms` 在成本估算时从空闲时长中扣除，用于抑制微小年龄抖动，
不改变精确/分层两种模式的实际选择顺序。

## 回收规则

- 到期条件：`idle_since_ms + idle_timeout_ms <= now_ms`。
- 候选只来自 `state=IDLE`，按最老优先排序。
- 仅当全局空闲数大于 `min_idle` 时继续关闭。
- 主动关闭使用 `--close`，关闭原因 `ACTIVE_CLOSE`。
- 配置升级降低租户配额或总上限时，只关闭超出新边界的空闲连接，原因
  `QUOTA_SHRINK`；活动租约等待正常释放。
- 每次关闭都更新连接状态和明细计数，配额通过重算释放，状态原子提交。

## 恢复不变量

`resume` 在一个事务中执行：

1. 把遗留 `OPENING` 意图标为 `ABORTED`。
2. 对每个 `ACTIVE` 租约确保恰好存在一个 `IN_USE` 连接；缺失则按租约字段修复。
3. 对没有活动租约的 `IN_USE` 连接转为 `IDLE` 并清除孤立租约引用。
4. 从连接明细重算每租户和全局水位，不执行任何增量扣减。
5. 写恢复审计并递增状态版本。

事务提交后，按逻辑连接重建进程本地回环句柄；该外部重建不改变账目，失败会记录在
恢复报告的 `rebuild_errors` 中。

## 并发与幂等

- 所有写操作先获取 `<db>.lock` 排他文件锁，再执行 SQLite `BEGIN IMMEDIATE`。
- `requests(app_seq, app_version)` 主键和 `leases(app_seq, app_version)` 唯一约束
  保证同申请只发放一次。
- `release_receipts(app_seq, app_version)` 主键保证重复释放返回首次回执。
- 队列按严格 `position` FIFO 派发；每次派发前从连接明细重算容量和配额。
- SQLite WAL 下，读取方只会看到完整提交后的账本；未提交的 `OPENING` 意图不属于
  池水位，只在诊断字段中可见。

## 已知边界

- 物理 socket 无法跨进程共享。数据库表示逻辑物理连接槽；同一进程持有活动句柄，
  崩溃后由 `resume` 重建本地句柄。账目不会因重建重复计数。
- 配置升级允许携带新的端点/轨迹摘要；同版本内容变化或显式摘要不匹配会拒绝写入。
- 空闲回收在注入时钟事件、显式 `advance/status --at-clock/resume` 时执行，不读取
  真实时间。
