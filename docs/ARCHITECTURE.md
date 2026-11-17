# 架构与一致性说明

## 1. 持久化模型

SQLite 数据库 `session.sqlite3` 包含以下核心表：

| 表 | 用途 |
| --- | --- |
| `meta` | `next_txid`、全局 `state_version`、恢复标记 |
| `tables_meta` | 表名与列结构 JSON |
| `transactions` | 事务当前状态、开始快照、结束水位 |
| `txn_events` | `begin/commit/rollback` 历史事件，带 `state_version` |
| `versions` | 物理行版本：`xmin`、`xmax`、tombstone、值、前后指针、创建水位、`xmax` 设置水位 |
| `row_heads` | 每个键的链头、链尾与链长 |
| `history` | 每次视觉判定快照、规范结果、来源、延迟与降级原因 |

视图 `version_rows_with_status` 将每个版本行与创建/删除事务连接，直接暴露
`xmin_commit_state` 和 `xmax_commit_state`；权威判定仍以不可变事务事件为源。

版本链按“旧版本 → 新版本”追加。新版本写入时，旧链头的 `xmax` 指向新事务号，
并记录 `xmax_set_version`。这一水位列使历史查询能够忽略查询之后才发生的
`xmax` 变更。

## 2. 事务与快照

`begin` 在单个 `BEGIN IMMEDIATE` 事务中：

1. 读取并验证单调 `next_txid`；
2. 捕获当前活跃事务集合；
3. 设置 `snapshot_xmax = allocated_txid + 1`；
4. 写入事务表与 `begin` 事件；
5. 增加全局 `state_version`。

事务的 `VIEW` 判定使用该事务固定的活跃集合与 `snapshot_xmax`。独立
`LATEST` 查询在单一 `state_version` 上由事件日志重建活跃集合、提交状态和
下一事务号。

## 3. 可见性规则

对候选版本：

- `xmin` 为当前事务创建时，按 read-your-writes 可见；
- 否则 `xmin` 必须已提交，且 `xmin < snapshot_xmax`、`xmin` 不在快照活跃集合中；
- `xmax` 为空时没有可见删除；
- `xmax` 为当前事务时，删除对当前事务可见；
- 否则 `xmax` 已提交、位于快照边界内且不在活跃集合中时，删除可见；
- 最后可见版本是 tombstone 时，键在当前视角下不存在。

输出逐版本给出 `xmin_committed`、`xmin_in_snapshot`、`xmin_visible`、
`xmax_state`、`xmax_in_snapshot`、`xmax_visible`、`decision` 与 `reason`。
候选和依据均按链位置稳定排序。

## 4. 快速路径与权威路径

快速路径：

- 从内存增量索引读取该键的追加版本；
- 使用事件水位重建提交状态；
- 查询结果缓存键包含 `view_version`、快照边界、活跃集合、读事务号和键；
- 任一写操作增加 `state_version` 后，缓存 `invalidate()` 清空全部条目。

权威路径：

- 从 `txn_events` 取 `state_version <= view_version` 的最新状态；
- 从 SQLite 读取目标键版本并按 `prev_version_id` 反向遍历；
- 检查环、前后指针一致、链头/链尾/链长、事务号严格递增；
- 使用与快速路径相同的纯判定函数生成规范结果。

`verify` 对两路规范 JSON 做逐字节比较。不一致时抛出 `E_VISIBLE`，
并递归定位到首个字段或 basis/candidate 元素位置。

## 5. 并发与确定性

- 每个 SQLite 写操作使用 `BEGIN IMMEDIATE`。
- 服务层用可重入状态锁把“写事务提交 + 索引更新 + 缓存失效”做成一个逻辑原子段。
- 查询捕获单一 `view_version`，其活跃集合与提交状态都只从该水位及更早事件计算。
- 后台提交器使用配置数量的工作线程，但单次事务是原子 `begin/write/commit`，
  因而不存在跨事务的链号乱序或半提交视图。

在 SQLite 单文件与单进程实验边界内，这提供了唯一确定结果。它优先保证实验一致性，
不尝试替代服务型数据库的多进程并发控制。

## 6. 延迟观测

每次快速判定使用 `time.perf_counter_ns()` 的单调计数起止。样本量化为整数微秒，
报告：

- `mean_fixed_us`；
- 最近秩 `p95_fixed_us`（无插值）；
- `max_fixed_us`；
- 量化误差上界 `1 µs`；
- 时钟基准和真值来源说明。

预算阈值由 `--budget-ms` 指定。超预算只记录 `fast_path_budget_exceeded` 和结构化日志，
不会根据耗时改写可见性结论。

## 7. 命名快照与整体替换

命名快照是带 SHA-256 的 JSON 文件，包含：

- 全部持久化表；
- 活跃事务快照；
- 缓存绑定版本；
- 历史判定完整记录与查询序列摘要；
- 会话版本和 schema 版本。

保存默认拒绝覆盖已有名称，旧快照文件不被恢复操作修改。恢复步骤：

1. 校验格式、checksum、缓存水位、事务号单调性和完整版本链；
2. 在未修改当前数据库的前提下完成全部检查；
3. 在一个 SQLite 写事务中删除当前事务/版本/历史并导入快照内容；
4. 记录快照版本与恢复前版本的差异，整体重建内存索引并清空缓存。

因此不存在事务表或版本链的增量合并路径。启动时若发现活动事务，只依据已持久化
事务表将其标记为 `rolled_back`，追加恢复事件并记录
`exit_report_generated=false`。

## 8. 历史重放与异常注入

历史记录保存查询捕获的快照和权威规范结果。恢复后逐项重新读取当时水位的事务事件
与版本链，重新执行判定并比较规范字节。不一致时抛出 `E_REPLAY`，附查询号、键、
事务号、视图版本和首个分歧字段。

异常注入只读取并复制输入快照文件：

| 注入 | 修改 | 预期业务码 |
| --- | --- | --- |
| `chain` | 首条链头 `prev_version_id` 指向自身 | `E_CHAIN` |
| `cache` | `cache_version` 与 `session_version` 错位 | `E_CACHE` |
| `txn` | `next_txid` 回退到最大已用事务号 | `E_TXN` |

注入后重新计算文件 checksum，不修改运行时代码或数据库。每项结果以结构化事件写入
`logs/events.jsonl`。
