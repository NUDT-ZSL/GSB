# 技术说明

## 确定性边界

引擎所有时间来自场景 `time_ms`。没有墙钟读取、线程调度、浮点窗口运算或网络 I/O。RTT、PTO、拥塞窗口、流量控制额度和字节区间均为整数或整数区间。

- 拥塞窗口标度：`1_000_000` 包。
- 时间单位：毫秒。
- 包号：每个包号空间从 0 开始，单调递增。
- 字节区间：内部使用半开区间 `[start,end)`。
- ACK 区间：输入输出使用闭区间 `[smallest,largest]`。
- JSON 快照：创建时通过 canonical JSON 往返，消除共享可变引用。

## 事件状态迁移顺序

1. 设置注入逻辑时钟。
2. 若连接已失败，仅记录跳过原因；关闭事件仍可处理。
3. 应用事件：写流、发送、ACK、PTO、MTU 或流控更新。
4. ACK 处理中先确认新包并执行窗口增长，再按包号阈值检测丢失。
5. 事件末尾按当前 PTO 执行时间阈值丢失检测。
6. 汇总本事件所有新丢失，统一执行至多一次窗口缩减。
7. 检测持久拥塞。
8. 生成不可变、稳定排序的完整快照。

ACK 先于时间丢失处理，避免同一逻辑时刻已经到达的 ACK 被不必要地判为超时。

## 发送规划

每个流维护：

- `write_ranges`：应用写入的已合并区间。
- `sent_ranges`：已在线路上发送的区间。
- `retransmit_ranges`：丢失后需重传的区间。
- `highest_sent_offset`：流级流控使用的最高绝对偏移。

候选区间为 `(write_ranges - sent_ranges) ∪ retransmit_ranges`。新偏移受流级剩余额度与连接级剩余额度共同约束，并按 `stream_id`、偏移稳定分配。发送时按有效 MTU 扣除固定包头开销，单包可包含多个 STREAM 帧，并可在首包附带 ACK 帧摘要。

`sendable_packets` 同时受拥塞窗口包槽位与数据包数上界约束。`flow_controlled_new_bytes` 不会超出连接级和流级新数据余量；`sendable_bytes` 还包含位于已消费绝对偏移内的重传字节，并通过 `already_consumed_bytes` 单独标记。

## 版本化提交

一次 `commit_every` 批次对应一个新 `version`：

1. 校验旧版本水位和轨迹完整。
2. 计算增量路径与逐时刻全量重算路径。
3. 逐事件比较；分歧则回滚并单独写 `divergence_reports`。
4. 创建新 `runs` 行。
5. 复制当前版本旧轨迹到新版本（轨迹本身不覆盖）。
6. 写入新事件的两条路径快照、验证结论与事件提交账本。
7. 写入舍入账本。
8. 更新 `current_state` 指针。
9. 单事务提交。

因此任读取方要么看到旧完整版本，要么看到新完整版本。`trace_events` 从不原地更新。

## 恢复判定

恢复时检查以下不变量：

```text
COUNT(event_commits) == watermark
MAX(event_commits.event_seq) == watermark
COUNT(trace_events[incremental]) == watermark
COUNT(trace_events[full]) == watermark
hash(current_state.state_json) == current_state.state_hash
run.watermark_event == current_state.watermark_event
```

任一不满足即 `E_RECOVERY`。SQLite 未提交事务在进程退出时自动回滚，因此不会重放半提交事件；手工遗留的 `pending` 运行只被标记为 `aborted`，绝不能作为当前轨迹。

## 并发协议

每个写命令：

1. 获取 `<db>.lock` 的 `flock(LOCK_EX)`。
2. 打开 SQLite 并执行 `BEGIN IMMEDIATE`。
3. 在事务内重新读取水位，绝不使用锁外缓存状态。
4. 提交后释放锁。

这使“同一事件同版本提交一次”和状态推进具有串行等价性。`resume --max-events 1` 允许多个进程交错推进同一场景，每次成功提交一个批次。
