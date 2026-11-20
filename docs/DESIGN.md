# 技术设计说明

## 1. 时间模型

逻辑步编号 `k = 0..total_steps-1`，步边界毫秒为 `k * step_ms`。事件到达时刻 `a` 的入队步为：

```text
due_step = ceil(a / step_ms) = (a + step_ms - 1) // step_ms
```

这样半步到达的事件不会被错误地提前到步边界之前，但通过时延仍从原始 `arrival_ms` 计算。每步只执行一次固定顺序：补充令牌、接入/丢弃、层级调度、生成快照、提交。

## 2. 定点换算与可证明的余量

设有理令牌增量为：

```text
x = bps * step_ms * 1000 / 8000 = q + r / 8000
```

其中 `1000` 是固定点标度，`q = floor(x)`，`0 <= r < 8000`。每个桶状态包含累计余量 `saved`。每步：

```text
carry = saved + r
extra = floor(carry / 8000)
new_saved = carry mod 8000
added = q + extra
```

因此经过 `N` 步后，补充总额与 `N*x` 的差小于一个固定点单位，而不会随步数线性累计。承诺桶和峰值桶各有一份 `saved`，不能共用。

桶深按 `bytes * 1000` 精确转换。令牌、队列字节、事件字节、调度额度和等待毫秒均以整数或 `Fraction` 运算；运行时没有浮点状态。

## 3. 双桶状态机

每类状态：

```text
committed_tokens, peak_tokens
committed_remainder, peak_remainder
```

补充：

1. 承诺速率增量加入承诺桶，上限 `committed_burst_bytes * 1000`。
2. 承诺桶放不下的增量仅转给尚有空间的峰值桶。
3. 峰值速率增量加入峰值桶，上限 `peak_burst_bytes * 1000`。
4. 放不下的部分丢弃为“未保留的突发”，不产生负债。

消费：

```text
take_committed = min(request, committed_tokens)
take_peak = request - take_committed
```

只有 `committed_tokens + peak_tokens >= request` 才允许发送，因此两桶都不会为负。父类与所有祖先类在包通过时依次执行同样检查与扣减。

## 4. 接入、队列和 WRED

事件先按分类规则解析到叶子类并保持 `seq` 顺序。入队前根据当前队列占用裁决：

- Tail Drop：`queue_bytes + event_bytes > queue_depth_bytes` 时丢弃。
- WRED：队列占用在 `[min_bytes, max_bytes)` 间线性计算丢弃概率；超过 `max_bytes` 或队列溢出时强制候选。
- WRED 随机值使用 SplitMix64，输入由固定种子、类别稳定哈希、事件序号和步编号组成。

每个决策记录候选标志、概率 ppm、随机 ppm、依据、队列前后字节。非候选事件必然入队；候选但未被选中的事件也保留选择依据。

同类队列严格 FIFO。调度器只从队首取整包，不拆分事件。

## 5. 层级 WRR

对每个有子类的节点：

1. 本步预算 `B` 是该节点可用承诺令牌加峰值令牌，并受上层传入额度限制。
2. 已结转额度先按申请比例分配；只对当前可直接或间接受益的队列保留。
3. 剩余预算先按保证带宽分配，再按剩余预算和子类权重分配。
4. 所有除法使用整数商和稳定最大余数法；平局按配置顺序。
5. 子类实际发送不得超过父预算、自身配额和自身令牌。
6. 未使用配额结转，受 `max(committed_burst, queue_depth)` 上限约束。
7. 轮询起点每步前进一位，输出每子类本步/累计轮询次数和未发送原因。

保证带宽也保存逐子类余量，避免每步向下取整带来的误差积累。父类发送字节逐级从祖先令牌扣除，故谱系总发送量不超过任一整形节点的可用令牌。

## 6. 派生估计

流式路径维护每类窗口通过字节、等待值有序表及窗口内事件队列。每次步进：

- 移除 `pass_ms < window_end - window_ms` 的事件；
- 加入本步通过事件；
- 计算吞吐 `bytes * 8 * 1000 / window_ms` 和最近秩分位。

真值路径在该步前缀上重新扫描全部通过事件，再用相同窗口和聚合定义计算。逐类比较字段：

```text
passed_bytes, passed_events, throughput_bps,
p50_wait_ms, p95_wait_ms, max_wait_ms, mean_wait_ms
```

若不一致，按当前可用证据分类：

- 换算余量非零导致：`fixed_point_conversion`；
- 吞吐聚合差异：`token_accumulation_error`；
- 等待/顺序字段差异：`scheduling_order_difference`。

同时输出第一个分歧的步、类别、字段、窗口、两路中间值和本步事件序号。

## 7. 误差传播与敏感标记

换算台账为每个速率记录原始浮点文本、标度、分子、分母、商、余数、舍入方向。传播账本为每类给出：

- 令牌余额上界：两个桶各小于一个固定点单位；
- 队列占用上界：0，因为队列是整数；
- 吞吐上界：令牌上界按窗口换算后的保守值；
- 时延上界：0，因为等待由整数毫秒得到；
- 换算链和参与事件序号。

对叶子类两两比较观测吞吐与路径可达吞吐；对每个父类的兄弟类比较逐步调度额度。差值严格小于 manifest 容差时，输出 `precision_sensitive=true`、`depends_on_difference=true`、证据步、事件序号和换算来源。

## 8. SQLite 事务与并发

主表：

```sql
steps(
  step_index INTEGER,
  version INTEGER,
  logical_time_ms INTEGER,
  prev_digest TEXT,
  step_digest TEXT,
  payload_json TEXT,
  state_json TEXT,
  PRIMARY KEY(step_index, version)
)
```

摘要链：

```text
step_digest = SHA256(canonical({
  "prev_digest": previous_step_or_initial_digest,
  "payload": payload,
  "state": state
}))
```

提交算法：

1. 读最后完整快照；
2. 在纯内存中计算下一步；
3. `BEGIN IMMEDIATE`；
4. 若目标步已存在，直接返回既有记录；
5. 如果数据库水位不是目标步减一，返回 `E_STALE_COMMIT`；
6. 否则在同事务插入载荷、状态、前序摘要和步进摘要；
7. 提交后才对读者可见。

硬终止可能发生在内存 refill 后、调度后或事务前，均不会留下半步。恢复读取最后 `state_json`，其中包含令牌、余量、队列、事件游标、累加计数器和 WRR 结转/余量。因此只执行水位之后的步进。

多个进程竞争同一步时，`BEGIN IMMEDIATE` 串行化写者；主键保证 `(step_index, version)` 只存在一行。冲突进程重新加载已提交快照并继续下一步，不会重复发送。

## 9. 可复现性

- 输入文件 SHA-256 固化在 manifest；
- 分类器规则按固定顺序；
- WRED 使用无状态确定性哈希；
- 队列和子类使用固定配置顺序；
- 稳定最大余数分配；
- 状态 JSON 使用键排序和紧凑编码；
- 所有摘要使用 SHA-256；
- 验证器从初始状态完整重放并逐行比较载荷、状态、前一摘要和步进摘要。

