# 技术说明

## 1. 结构

```text
elasticctl/
  model.py        领域对象、JSON 解析、跨对象校验
  fixed.py        Decimal/定点换算、核时单位
  aggregation.py  平均值/分位数聚合与双通路记录
  decision.py     目标副本数、规则优先级与方向裁决
  patch.py        增量补丁、依赖图、闭包和失效报告
  storage.py      SQLite schema、事务、对象读写
  snapshot.py     决策快照、指针、verify
  locks.py        跨平台阻塞式文件锁
  service.py      load/apply/decide/dispatch/resume/status/verify 编排
  cli.py          argparse 命令入口
```

动作执行器是本地确定性模拟器。它不访问网络；`action_outcomes.json` 只按 `(ruleId, logicalNowMs)` 注入成功/失败，未命中的动作默认成功。实际动作结果和退避参数会在预留时写入动作行，恢复时复用同一结果，不依赖系统时间或随机数。

## 2. 校验与业务错误

所有 `load` 文件在打开 SQLite 之前完成 JSON 解析和全量语义校验。`apply` 在 `BEGIN IMMEDIATE` 事务内基于当前配置计算新配置，完整复验后才写表；任何失败都回滚。

主要错误码：

- `E_DUPLICATE_ID`, `E_DUPLICATE_NAME`, `E_DUPLICATE_PRIORITY`
- `E_REPLICA_RANGE`, `E_INVALID_INTEGER`, `E_INVALID_NUMBER`
- `E_INVALID_AGGREGATION`, `E_INVALID_PERCENTILE`, `E_INVALID_PERCENT`
- `E_MISSING_WORKLOAD_REF`, `E_MISSING_METRIC_REF`
- `E_INVALID_COOLDOWN`, `E_INVALID_FREEZE_WINDOW`, `E_INVALID_BUDGET`
- `E_CLOCK_ROLLBACK`, `E_VERSION_CONFLICT`, `E_INFLIGHT_CONFLICT`
- `E_PATCH_OBJECT_NOT_FOUND`, `E_PATCH_DUPLICATE`, `E_PATCH_ALREADY_APPLIED`
- `E_NOT_LOADED`, `E_ACTION_NOT_FOUND`, `E_INVALID_STATE`

工作负载、指标源、规则名称分别在各自类型内唯一；ID 在工作负载/指标源/规则/预算之间全局唯一；规则优先级全局唯一。预算 `freezeWindows` 使用半开区间 `[startMs, endMs)`。

## 3. 依赖图与增量补丁

依赖边方向为：

```text
metric_source -> rule -> workload
resource_budget -> workload (x全部工作负载)
changed workload -> itself
```

补丁同时使用修改前和修改后的边计算闭包，因此新增、修改、删除引用都能得到从触发对象到工作负载的稳定对象链，例如：

```text
metric_source:ms-queue-depth -> rule:r-queue-fast-out -> workload:wl-queue
```

失效规则是精确的：

- 所有受影响工作负载的目标副本数缓存失效。
- 规则新增/删除，或 `workloadId/metricSourceId/direction/cooldownMs` 改变时，该规则冷却/退避状态重置。
- 仅修改 `step/percent/priority/name` 时保留冷却计时；测试验证规则 `r-queue-fast-out` 只改 `percent` 后其 `cooldownUntilMs` 不变。
- 预算变化使所有工作负载的后续预算预扣重算；每副本 CPU/内存、副本范围或会影响动作成本/步长的规则变化使对应工作负载重算。
- 未受影响工作负载的副本、规则截止时刻、决策缓存和副本账本不被写入；补丁报告列出 `unaffectedWorkloads`。

相同 `patchId` 和相同内容重放时直接返回首次报告，不改变配置版本或快照版本。

## 4. 确定性聚合

窗口定义为 `(logicalNowMs - windowMs, logicalNowMs]`，边界只来自注入的 `--at`；运行时不调用 `time.time()` 或系统墙钟。

- 平均值：定点整数求和后整除，余数达到半个单位时向上。
- 分位数：升序稳定排序，`rank=(n-1)*p`，取相邻两点线性插值，最终按 `ROUND_HALF_UP` 量化到 `0.000001`。
- 定点通路：样本先量化为 scale `1_000_000` 的整数。
- 浮点通路：样本按 `binary64` 由 NumPy/原生浮点计算，再用 `decimal(str(value))` 量化到相同 scale。

每条聚合结果记录 `fixedPoint.scale`、`fixedPoint.rounding`、`fixedPoint.roundingDirection`、浮点规范和 `pathsAgree`。决策只使用定点值。固定样本的所有窗口双通路逐位一致。

逻辑时钟回拨超过 `clockRollbackToleranceMs` 返回 `E_CLOCK_ROLLBACK`，事务在写任何聚合/决策状态前终止。允许的小幅回拨不会降低已提交逻辑时钟或采样水位。

## 5. 目标副本数

优先级数字越小优先级越高。命中条件：

```text
scale_out: aggregate >= alertThreshold AND aggregate > targetValue
scale_in:  aggregate <= alertThreshold AND aggregate < targetValue
```

步长百分比增量：

```text
stepDelta = max(step, ceil(currentReplicas * percent / 100))
```

扩容：

```text
ratioCandidate = ceil(currentReplicas * aggregate / targetValue)
rawCandidate   = max(currentReplicas + stepDelta, ratioCandidate)
target         = clamp(rawCandidate, minReplicas, maxReplicas)
```

缩容：

```text
ratioCandidate = floor(currentReplicas * aggregate / targetValue)
rawCandidate   = min(currentReplicas - stepDelta, ratioCandidate)
target         = clamp(rawCandidate, minReplicas, maxReplicas)
```

候选结果与当前副本相同则不产生动作，并记录 `max_replicas_reached`、`min_replicas_reached` 或夹取原因。

所有规则参与评估并输出 `ruleEvaluations`。按优先级和规则 ID 稳定排序后，跳过未命中、冷却、退避、冻结扩容和无变化规则；第一条可执行规则获胜。其他规则标记 `shadowed_by_higher_priority` 或 `direction_conflict_with_higher_priority`。冻结窗口只禁止 `scale_out`，不禁止 `scale_in`。

## 6. 预算与调度

内部核时账目使用整数 `millicore-millisecond`：

```text
1 core-hour = 1000 millicores * 3_600_000 ms = 3_600_000_000 units
actionDurationMs = actionBaseDurationMs + actionDurationPerReplicaMs * abs(delta)
coreCostUnits    = abs(delta) * cpuMillicoresPerReplica * actionDurationMs
memoryMiB        = abs(delta) * memoryMiBPerReplica
```

- 核时在规划时累计预扣，失败动作也不退还已消耗的核时。
- 内存按在途并发占用量预扣，动作成功/失败确认时释放一次。
- 每轮逻辑时间预算只限制本轮启动动作的 `actionDurationMs` 总和，不作为运行超时；已 `reserved` 的动作一定执行到成功/失败确定结局。
- 每工作负载在途 `reserved/started` 动作数必须小于 `maxConcurrentActionsPerWorkload`。

动作按规则优先级、工作负载 ID、规则 ID 稳定排序。一旦某个动作因核时、内存或轮时间预算不足而不能启动：

1. 当前动作记录 `budget_not_started`，原因是具体上限。
2. 后续低优先级动作记录 `abandoned`，原因 `dropped_after_higher_priority_budget_cutoff`。
3. 全部截断对象进入调度摘要的 `notStarted` / `activelyAbandoned`，不会扣减预算。

调度摘要明确区分 `startedSucceeded`、`failedAfterStart`、`notStarted`、`activelyAbandoned`、`unconfirmed`。

## 7. 冷却、冻结与退避

- 成功：`cooldownUntilMs = actionLogicalNowMs + rule.cooldownMs`，连续失败数清零。
- 失败：连续失败数加一，清除冷却，设置 `backoffUntilMs`。
- 基础退避：`min(baseMs * 2^(failures-1), maxMs)`。
- 抖动：由 `sha256(ruleId|actionId|failures|logicalNowMs)` 决定 `[0, floor(capped*jitterRatio)]` 内的整数，最终总延迟再受 `maxMs` 限制。
- 冷却/退避内的规则不会进入本轮到执行队列。
- 冻结窗口内所有扩容规则被拒绝，缩容仍可参与优先级裁决。

## 8. 持久化、快照与恢复

SQLite 使用 WAL、`synchronous=FULL`、`BEGIN IMMEDIATE` 和 30 秒 busy timeout。写操作还使用数据库旁路文件锁，保证跨进程规划互斥；动作执行阶段允许不同实例并行。

事务内持久化：

- 逻辑时钟和已提交采样水位；
- 工作负载副本数、副本基准和成功动作副本账本；
- 核时累计用量、内存在途预留；
- 冷却/退避截止时刻和连续失败数；
- 轮次、决策缓存、动作、幂等动作效果；
- 完整决策快照和当前快照指针。

每次状态写入先以 compare-and-swap 方式推进 `state_version`。快照 body 使用 canonical JSON 和 SHA-256；`meta.current_snapshot_id` 在同一事务中整体切换，`verify` 会重建当前状态并逐字节比较快照。

动作 ID：

```text
sha256(roundId | workloadId | ruleId | ruleVersion | direction | from | to)
```

轮次 ID 包含逻辑时刻、配置版本和排序后的工作负载范围。动作表还有 `(roundId, workloadId, ruleId, ruleVersion)` 唯一约束，因此同一工作负载同一轮次同一规则版本只能预留一次。

动作状态：

```text
reserved -> started -> succeeded
                    -> failed
budget_not_started
not_started
abandoned
```

恢复时：

- `reserved`：预算已提交但外部动作尚未确认，可安全下发。
- `started`：可能已下发但未确认，以同一动作 ID 幂等重试。
- `succeeded/failed`：终态，不再重试、不再扣减、不再预占。
- `budget_not_started/not_started/abandoned`：从未启动，不进入恢复队列。

`resume` 输出 `recoveryClassification`，分别计数已确认终态、预留未下发、启动未确认和终态未启动动作，再只执行第二、三类。

结果事务同时更新副本、副本账本、冷却/退避、内存释放、幂等效果和快照；进程在事务中断开时 SQLite 回滚，恢复路径只会看到此前完整提交的状态。

## 9. 多进程与幂等

- 文件锁串行化一轮的决策和预算预留，SQLite `BEGIN IMMEDIATE` 串行化所有账目写事务。
- 不同范围的轮次可并行执行动作；核时和内存从同一 `budget_state` 原子预留。
- 两个实例同时处理同一工作负载同一轮次时，只有一个动作行；另一个实例复用并等待同一幂等效果。
- 重复 `dispatch` 返回相同决策/调度摘要，不重复扣减，也不推进快照版本。
- 重复 `apply` 相同补丁返回首次报告。

`verify` 检查：

- `PRAGMA integrity_check`；
- 快照指针、版本、SHA-256 和 live-state 逐字节一致性；
- 核时累计值等于所有已预扣动作成本之和；
- 内存在途值等于所有 `reserved/started` 动作内存之和；
- 副本基准 + 副本账本增量等于工作负载当前副本；
- 成功动作都有且只有一次幂等效果，非成功动作没有效果；
- 不存在重复动作业务键；
- 预算账目不超过池上限。

## 10. 说明与边界

- 这是单机控制面与本地动作模拟器，不实现云端真实扩缩容 API。
- 内存上限采用在途预留语义；完成后释放。核时上限采用累计消耗语义。
- 浮点通路用于审计和交叉检查；若极端输入导致双通路末位不同，`pathsAgree=false` 会被记录，业务决策仍使用定点通路。
- 修改工作负载 `currentReplicas` 是人工重同步：会重置该工作负载的副本基准与后续副本账本，但不会回滚历史动作和核时账目。

