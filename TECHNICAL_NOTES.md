# 技术说明与验收记录

## 1. 设计取舍

### 数据生成

`health_machine/seed.py` 使用固定种子号 `20261126`，但样本轨迹由显式数据构造，避免伪随机波动。生成内容包括：

- `registry.json`：1 个服务、4 个实例，每实例级别 `[1,2,3]`；
- `observations.json`：75 条观测，覆盖稳定、抖动、失联/隔离恢复和时钟跳变；
- `thresholds_v1.json`：单文件、相对秒、扁平失败数阈值和整数权重；
- `thresholds_v2/shard-00.json`、`shard-01.json` 与 `thresholds_v2/meta.sqlite`：分片哈希、生成代数和墓碑；
- `patches/*.json`：阈值修改、实例改名和墓碑。

### 阈值合并

每个实例+级别的候选阈值均写入 `threshold_candidates`，当前裁决写入 `threshold_view`。

裁决排序键为：

```text
(logical_clock_ms, format_priority, threshold_generation)
```

v2 的格式优先级为 `2`，v1 为 `1`。因此：

- 逻辑时钟不同：较新逻辑时钟获胜；
- 逻辑时钟相同：v2 来源格式获胜；
- 前两项仍相同：较高阈值代数获胜。

每条视图保留：

- `winner_format`、`winner_snapshot_id`、`generation`；
- `provenance_json` 中的候选来源、墓碑和完整裁决上下文；
- `decision_json` 中的裁决原因和失败候选；
- `raw_failure_counts_json`：来源原始失败数数组；
- `time_conversion_json`：原始时间值、单位、标度 `1000`、舍入 `floor`、时间基和绝对毫秒。

失败/落败候选也会在窗口结果的 `excluded_thresholds` 中输出代数和 `superseded_by_*` 原因；墓碑和未迁移阈值分别输出 `tombstoned`、`unmigrated_threshold`。

### 确定性得分

所有分数使用整数基点 `bp`，不使用墙钟或浮点打分：

```text
availability_bp = floor(10000 * successes / max(1, successes + failures))
latency_bp      = min(10000, floor(10000 * budget / avg_latency))
level_score_bp  = floor((70 * availability_bp + 30 * latency_bp) / 100)
window_score_bp = floor(sum(weight_i * score_i) / sum(weight_i))
```

平均延迟在聚合时用 `Decimal` 按探测总数加权，避免浮点末位差异。窗口归属为 `clock_ms // window_ms`。超时只比较相邻注入时钟差与注册的 `timeout_ms`。

### 状态机

联合裁决要求触发级别同时出现分数越界和原始失败数越界。主触发级别按失败比例、权重、级别号稳定排序选择。状态升级阈值为连续失败 `1/2/4`：

```text
healthy --1 failure--> degraded --2 failures--> lost --4 failures--> isolated
```

恢复弧从第一个合格窗口开始，记录窗口列表、起始/最后注入时钟、逻辑时长和所需阈值。状态保持期间恢复弧持续追加；再次失败会把当前弧标记为 `broken`；达到窗口数和时长后标记为 `completed` 并回到 `healthy`。

## 2. 崩溃恢复

`observation_inbox` 先持久化原始观测，状态为 `received`；窗口提交时在同一事务内完成：

1. 写 `window_summaries`；
2. 写 `window_level_scores`；
3. 写 `transition_log` 和哈希链；
4. 关闭/更新 `recovery_arcs`；
5. 把本窗口观测改为 `committed`；
6. 更新 `instance_state` 的计数、恢复弧、窗口/时钟水位、各级别序号、阈值指针和状态哈希。

因此不存在“窗口分数已提交但状态未提交”的中间版本。杀死在事务提交前只会留下持久化的 `received` 观测，即相对于窗口结论的半提交输入。`resume` 查询已有 `window_summaries`，用集合差选择缺失窗口，只重放后缀。

测试 `tests/test_recovery.py` 使用真实子进程和 `SIGKILL`（退出码 `-9`）验证：

- `before_window_commit`：首个 outage 窗口事务回滚，30 条 outage 观测仍为 `received`，0 个窗口；
- `after_inbox_commit`：load 阶段被杀，75 条观测已持久化；
- `before_merge_commit`：合并事务回滚，快照版本数为 0；
- `HSM_FAULT_MODE=sigterm`：信号处理器抛业务终止异常并回滚；
- 恢复后的状态哈希、级别分数、迁移哈希链与一次性执行一致。

## 3. 补丁闭包与并发

补丁按 `patch_id` 幂等。相同 ID 和相同内容返回首次结果；相同 ID 不同内容返回 `HSM-6002`。

- `upsert_threshold`：闭包包含该级别的 `level_score`、窗口分、状态迁移和恢复弧。
- `tombstone`：同样失效该级别派生结论。
- `rename_instance`：闭包包含注册表 ID 和所有实例引用表。

受影响实例在同一个 SQLite 写事务中删除派生结论并从原始收件箱重放；`recompute_audit` 只给实际重算的实例写审计行。未受影响实例的 `state_hash` 在测试中逐字节保持不变。

并发控制有两层：

1. `fcntl.flock`：按实例、注册表、观测、快照和补丁使用不同锁文件；
2. SQLite `BEGIN IMMEDIATE`、`busy_timeout=30000`、唯一主键和唯一补丁/快照版本。

测试启动 6 个进程同时处理同一 outage 实例，最终仍只有 10 个窗口和 4 条迁移；4 个进程分别处理不同实例也通过全量 verify。另有 4 进程重复 load 和 4 进程并发 merge 同一快照版本的测试。

## 4. verify 机制

`verify` 从持久化库复制 `meta`、注册信息、当前阈值视图、指针和原始观测到内存 SQLite，然后用生产引擎代码完整重放。它比较：

- `window_summaries`；
- `window_level_scores`；
- `transition_log`（排除自增 `id`）；
- `recovery_arcs`（排除自增 `id`）；
- `instance_state`；
- 迁移哈希链的 `prev_hash`。

不一致时输出 JSON Pointer 风格字段路径、期望值和实际值，例如：

```text
window_summaries[inst-steady/1].score_bp
instance_state[inst-flap].failure_streak
```

## 5. 实际验证

最近一次本地运行：

```bash
bash scripts/acceptance.sh
python3 -m pytest -q
```

结果摘要见 `artifacts/acceptance-output.txt` 与 `artifacts/pytest-output.txt`。验收脚本输出 `ACCEPTANCE_OK`，全部 pytest 用例通过，其中包含真实 SIGKILL、SIGTERM、多进程并发和字段级篡改检测。
