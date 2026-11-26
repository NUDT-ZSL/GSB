# 技术设计

## 1. 数据流

工作台只使用 Python 3.10 标准库。确定性种子脚本生成主题、事件流、订阅、v1 JSON、v2 分片 JSON、v2 SQLite 元数据库以及三个快照包。

主链路为：

1. `loader.py` 在写入状态前完成全部解析和交叉校验。
2. `semantics.py` 将 v1/v2 裁决为统一视图，并处理版本包、diff 与代数回退。
3. `watermark.py` 构建反向依赖图、失效闭包，并用定点计算分区水位。
4. `patches.py` 按主题游标和 `pull_batch_size` 生成分组补丁。
5. `scheduler.py` 按优先级、逻辑预算和分区并发上限生成确定性诊断计划。
6. `storage.py` 使用文件锁、SQLite 事务、幂等账本和 prepared/committed 检查点持久化。
7. `verifier.py` 在临时 SQLite 中重跑全链并进行字段级 diff。

## 2. 输入与校验

分区键规则支持：

- `suffix_mod:N`：取键末尾数字并对 `N` 取模。
- `fnv1a_mod:N`：稳定 FNV-1a 32 位哈希取模。
- `map:key=partition,...`：显式映射，未列出的键判定为未覆盖。

校验要点包括主题名/订阅者 ID 唯一、分区数为正、事件 `seq` 全局严格递增、逻辑时钟非负、回调必须是显式端口的 loopback URL、v1 相对秒数非负、v2 分片序号连续、分片代数非递减、`prev_sha256` 哈希链和文件 SHA-256 一致、SQLite 元数据与分片 JSON 一致。

校验发生在 `Storage.initialize()` 之前，因此非法样本不会创建或写入业务数据库。

## 3. 两代快照合并

同一 `message_key` 同时存在于 v1/v2 时：

1. 先比较候选条目的声明逻辑时钟 `logical_clock_ms`，较新者胜。
2. 声明时钟相同，按来源格式优先级裁决：`v2-package(30) > v2(20) > v1(10)`。
3. 语义代数和代差写入 `adjudication`；若胜出条目的代数低于另一来源，设置 `generation_rollback_against_other_format=true`，交给失效闭包处理。

v1 的非负相对秒数按注入合并时钟 `C` 换算：

```text
effective_at_ms = C + effective_rel_sec * 1000
scale_ms_per_second = 1000
rounding_mode = ceil
rounding_direction = toward_positive_infinity
```

整数秒没有余数，因此样本中的 `rounded=false`、`rounding_delta_ms=0`；舍入元数据仍逐条目保留。每个统一条目还保留来源、原始时间表示、候选时钟/代数和裁决原因。

## 4. 水位与失效闭包

规范消息键为 `topic/partition/offset`。水位 `watermark_offset` 是可连续投递的偏移数量，`delivered_through_offset=watermark_offset-1`。

从偏移 0 开始，遇到以下任一情况即在该偏移阻塞：

- 缺少语义条目；
- 生效时间晚于当前 `--clock-ms`；
- 墓碑；
- 语义代数/修订低于事件期望；
- 上游水位依赖未满足，默认要求 `upstream_offset + 1` 的连续水位。

跨分区依赖通过单调下降的定点迭代求解。反向图边为：

```text
upstream topic/partition/offset -> downstream message
```

补丁中的新墓碑或代数回退成为闭包根，BFS 得到精确受影响消息、边、路径和分区。增量模式只用旧水位作为未受影响分区的固定输入，仅重算闭包内分区；未受影响分区的 `content_json` 不执行 UPDATE，因此结论字节保持不变。

## 5. 补丁与游标

变更事件按主题分配连续 `topic_seq`，同时保留全局 `global_seq`。每个订阅者游标是该主题已处理的最大 `topic_seq`。

补丁按 `pull_batch_size` 分块，ID 形如：

```text
PATCH:<subscriber>:<topic>:<from>-<to>:<chunk>
```

每个分组包含 `trigger_semantic_version`、`upstream_offsets`、`impact_path`、变更原因、代数、修订和载荷摘要。游标提交写入 `cursor_commit_ledger(subscriber_id, from_seq, to_seq)`；重复或并发提交使用 `INSERT OR IGNORE`，只有账本首次插入成功时才递增 `cursor_revision`。

## 6. 调度结局

候选任务为“订阅者 × 其主题分区”，稳定排序键是：

```text
(-priority, subscriber_id, partition, partition_key)
```

任务成本由 `pull_batch_size`、分区事件数和 `rate_limit_per_sec` 推导。调度器先检查每分区已启动槽位，再检查完整任务成本是否能放入剩余逻辑预算；任务一旦启动就扣满成本并运行到结束，不会被预算截断。

稳定结局分类：

- `started_succeeded` / `outcome_class=started_succeeded`
- `started_failed` / `outcome_class=failed_after_start` / `label_zh=启动后失败`
- `end_category=budget_unstarted` / `outcome_class=within_budget_unstarted` / `label_zh=预算内未启动`
- `end_category=voluntarily_abandoned` / `outcome_class=voluntarily_abandoned` / `label_zh=主动放弃`

## 7. SQLite、崩溃恢复与并发

主要表：

- `semantic_versions` / `unified_views`：语义指针和不可丢视图。
- `partition_watermarks` / `partition_commit_ledger`：当前水位和“分区 × 语义绑定”唯一提交账本。
- `subscription_cursors` / `cursor_commit_ledger`：游标和幂等推进账本。
- `change_events` / `patches`：变更序列和补丁 JSON。
- `invalidation_closures`：失效事件链。
- `diagnostic_runs` / `diagnostic_tasks`：确定性调度结论。
- `checkpoints`：`prepared` 或 `committed` 检查点及完整计划 JSON。

检查点先在一个短事务中写入 `status=prepared` 的完整计划，再在第二个 `BEGIN IMMEDIATE` 事务中原子写入语义指针、闭包、水位、游标、补丁、诊断运行和逻辑时钟水位，并把检查点改为 `committed`。

`WB_CRASH_POINT=checkpoint_after_prepare` 会在 prepared 事务后用 `SIGKILL` 自杀，模拟强制终止。恢复时：

- `prepared`：说明领域事务尚未提交，按计划幂等重放；
- `committed`：直接跳过；
- 提交中途被杀：SQLite 回滚第二个事务，检查点仍为 `prepared`，可安全重试。

`state/workbench.sqlite3.lock` 上的 `flock` 串行化写事务，SQLite `busy_timeout` 和幂等主键处理竞争。多个进程可并行处理不同订阅者；同一分区/语义绑定和同一游标区间在账本中只能插入一次。

## 8. 版本包裁决

当前语义指针包含 `snapshot_version`、`generation_min`、`manifest_hash` 和分片哈希。新包处理规则：

- 版本号更小：`WB_SNAPSHOT_VERSION_ROLLBACK`，CLI 退出码 `3`。
- 同版本且 manifest 哈希相同：幂等 no-op。
- 同版本但哈希不同：`WB_SNAPSHOT_VERSION_HASH_CONFLICT`。
- 代数下限回退：`WB_GENERATION_FLOOR_REGRESSION`。
- 新版本且 `mode=incremental` 或基础版本匹配：增量追加/补丁。
- `mode=full` 或缺少可兼容增量基线：完整替换。

## 9. 主要业务错误码

| 错误码 | 含义 |
|---|---|
| `WB_TOPIC_NAME_DUPLICATE` | 主题名重复 |
| `WB_SUBSCRIBER_ID_DUPLICATE` | 订阅者 ID 重复 |
| `WB_KEY_RULE_INVALID` / `WB_EVENT_KEY_UNCOVERED` | 分区键规则不可解析或未覆盖事件键 |
| `WB_EVENT_SEQUENCE_NOT_STRICT` | 事件序号未严格递增 |
| `WB_V1_RELATIVE_SECONDS_INVALID` | v1 相对秒数不是非负整数 |
| `WB_V2_SHARD_HASH_MISMATCH` | v2 分片哈希不一致 |
| `WB_V2_GENERATION_NOT_MONOTONIC` | v2 分片代数未单调 |
| `WB_SNAPSHOT_VERSION_ROLLBACK` | 语义版本回退 |
| `WB_VERIFY_MISMATCH` | 重跑全链后字段比对不一致 |
| `WB_LOCK_BUSY` | 文件锁等待超时 |

完整错误码定义见 `delivery_workbench/errors.py`。
