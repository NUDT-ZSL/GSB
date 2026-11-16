# 设计说明

## 1. 组成

- `internal/store`：SQLite 表、事务写入入口、只读快照与版本整替换。
- `internal/engine`：分区状态机。每个分区拥有独立互斥锁；写入成功后才替换内存状态。
- `internal/service`：回环 TCP、JSON 行分帧、协议校验与结构化请求日志。
- `internal/verify`：不依赖内存状态的独立重放验证器。
- `internal/tools`：验证器与故障注入的命令行封装，供两个入口共用。
- `cmd/orderqueue`：服务进程与样本生产/消费/查询/验证/故障工具。
- `cmd/orderverify`：可选的独立验证与故障注入兼容入口。

## 2. SQLite 数据模型

| 表 | 作用 |
| --- | --- |
| `meta` | 日志格式版本、账本格式版本、最大重投次数。 |
| `messages` | 分区追加日志，主键 `(partition_key, message_offset)`。 |
| `producer_sessions` | 生产者会话的全局最大序列号。 |
| `partitions` | 分区账本：尾位点、已提交/已确认位点、窗口、重投状态、阻塞状态、版本。 |
| `retry_counts` | 每个 `(partition, offset)` 的当前重投次数。 |
| `events` | 持久化事件日志：追加、发窗、提交、失败、超时、恢复重投、毒点、解阻、版本重建。 |

SQLite 使用 WAL、`synchronous=FULL`、`foreign_keys=ON` 和 `busy_timeout`。`database/sql` 连接池固定为 1，分区互斥锁决定业务事件顺序，SQLite 事务提供原子提交。

## 3. 追加状态机

1. 校验分区键、生产者会话、正序列号和消息体字节上限。
2. 获取该生产者会话的锁，要求 `producer_seq > last_seq`。
3. 获取目标分区锁，读取当前 `next_offset`。
4. 在同一个事务中插入 `messages`、更新 `producer_sessions`、更新 `partitions.next_offset`、插入 `append` 事件。
5. 事务提交成功后再更新内存状态并返回回执。

因此非法请求不会消耗位点；返回的位点与事务中持久化的 `message_offset` 完全一致。

## 4. 投递窗口

窗口字段为 `[window_start, window_end]`、持有者会话、墙钟期限、TTL 和恢复标记。正常运行时的到期判定使用 `time.Now()` 的单调时钟分量；持久化只保存墙钟期限用于恢复报告和执行审计。

拉取规则：

- 分区已阻塞：`E_POISON`。
- 存在未到期窗口：`E_FENCE`，返回持有者。
- 窗口已到期：先原子写入 `timeout` 事件和重投计数，再释放窗口。
- 窗口来自上一进程：记录 `recovery_detected`，下一次拉取先执行 `recovery_redelivery`，从窗口最早位点整体计数并重新发窗。
- 无窗口：从 `committed_offset + 1` 开始，最多返回 `limit` 条，窗口起点与账本一致。

## 5. 提交语义

提交值表示“已成功处理并确认到该位点”。服务端只接受当前窗口内、严格大于已提交位点的连续前缀：

- 消息窗口本身从 `committed_offset + 1` 开始，因此不存在跳过更早未投递消息的路径。
- `offset > window_end`、`offset <= committed_offset` 或非当前持有者均被拒绝。
- 提交到窗口中部时，窗口收紧为 `[offset + 1, window_end]`；提交到窗口尾部时释放窗口。
- `committed_offset` 和 `confirmed_offset` 在同一事务中推进，并由 SQLite `CHECK(committed_offset <= confirmed_offset)` 兜底。

## 6. 失败、超时与毒点

失败或超时的公共路径：

1. 在分区锁内确认窗口持有者和状态。
2. 对窗口内所有未确认位点逐位点递增重投计数。
3. 关闭窗口，记录最早重投位点。
4. 若任意位点计数大于 `max_retries`，选择最早的超限位点，设置 `blocked=true` 并写入 `poison` 事件。
5. 将所有账本、计数和事件放在同一事务中提交，再替换内存状态。

`E_POISON` 响应携带 `partition_key`、`blocked_offset` 和 `blocked_retry_count`。阻塞期间拒绝拉取，但允许继续追加；人工 `unblock` 会清除未提交区间的重投计数并恢复有序投递。

## 7. 提交与超时竞争

提交、失败、拉取和后台超时扫描都先取得同一分区锁。提交在锁内比较当前单调时间与窗口期限：

- 时间早于期限：提交事件先写入，窗口释放或收紧，后续扫描看不到窗口。
- 时间达到期限：先走超时路径并写入 `timeout` 事件，提交返回 `E_FENCE`。

因此二者不可能同时成功。结构化日志记录 `decision`、`basis`、`now_mono_ns`、`deadline_mono_ns`、`event_id`；提交冲突日志还记录 `first_event=window_timeout`。

## 8. 强制终止恢复

消息追加、窗口发窗、提交、失败和超时都是 SQLite 事务。进程被强制终止时：

- 尚未提交的 SQLite 事务整体回滚，不会留下半条消息或半个账本更新。
- 已提交的消息、账本和事件在下次启动时可见。
- 启动时读取所有分区，校验消息位点连续性、`next_offset`、已提交/已确认位点、窗口边界、阻塞位点和重投计数。
- 若存在未确认窗口，将其标记为恢复窗口，设置 `pending_redelivery`，写入 `recovery_detected` 事件；下一次拉取从最早未确认位点重投。
- 启动阶段写恢复报告，列出分区数、窗口、待重投位点、重投计数、版本重建情况以及尾部事务处理策略。

进程不承诺在强制终止时生成退出报告；恢复报告只在重启时生成。

## 9. 版本识别与整体替换

- `meta.log_format_version` 和 `meta.ledger_format_version` 表示持久化格式版本。
- 每个 `partitions` 行同时保存 `ledger_version` 和正常状态递增的 `state_revision`。
- 任一格式版本较低时，不信任旧账本字段；验证器先读取 `messages` 和 `events`，为每个分区重建完整状态，然后整体覆盖 `partitions` 与 `retry_counts`。
- 全局日志版本变化时，同时从消息日志整体重建 `producer_sessions`。
- 版本高于当前支持版本时拒绝启动并返回 `E_LEDGER`。
- 正常版本下若字段约束损坏，启动校验返回 `E_LEDGER`，并携带 `partition_key` 和具体字段。

## 10. 独立验证

验证器只读打开 SQLite，不调用引擎内存状态。它按分区：

1. 校验消息位点连续、追加事件存在且生产者序列递增。
2. 重放 `window_issued`、`commit`、`fail`、`timeout`、`recovery_redelivery`、`poison`、`unblock`。
3. 校验每次提交只发生在当前窗口内且不越过最早未确认位点。
4. 重算逐位点重投计数、阻塞状态和最终窗口。
5. 与 `partitions` 和 `retry_counts` 的最终值比较。

首个差异写入 `first_violation`，总业务码为 `E_VERIFY`。报告最多保留 200 条差异，便于定位。

该实现通过 `orderqueue verify`、`orderqueue fault` 暴露；`orderverify check|fault` 只是同一内部实现的独立二进制入口。Windows 演示脚本只调用 `orderqueue.exe`，避免额外的可执行文件受应用控制策略单独拦截。

## 11. 可观测性

服务、验证器和故障工具都写 JSONL。公共字段包括 `event`、`ts`、`mono_ns`、`event_seq`、`pid`。关键事件包括追加提交/拒绝、窗口发放/围栏、提交成功/拒绝、重投、超时、毒点、解阻、版本重建和恢复完成。
