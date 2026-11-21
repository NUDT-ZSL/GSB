# LockFair 技术说明

## 1. 模块边界

| 模块 | 职责 |
| --- | --- |
| `lockfair.models` | JSON 解析、闭合枚举与业务约束校验；失败时不触碰 SQLite |
| `lockfair.engine` | 队列状态机、轮内时间轴、三重约束选择、饥饿让渡、租约代数与恢复 |
| `lockfair.store` | SQLite schema、原子快照、版本指针、规范化镜像、文件锁与只读快照 |
| `lockfair.web` | 本地 HTTP API 与静态资源服务 |
| `lockfair.cli` | `validate`、`init`、`step`、`run`、`recover`、`serve` 等入口 |

业务时间只来自 JSON 配置、申请逻辑时钟、轮次逻辑时钟和租约到期字段。代码中没有 `time.time()`、`datetime.now()` 或系统墙钟参与裁决。

## 2. 轮次模型

### 2.1 逻辑时钟

- 初始水位：`config.initial_clock_ms`。
- 默认下一轮时钟：上一已提交水位 + `clock_step_ms`。
- CLI/API 可显式传入 `clock_ms`；若低于水位且超过 `clock_rollback_tolerance_ms`，返回 `CLOCK_ROLLBACK_EXCEEDED`。
- 容忍范围内的轻微回拨不会让水位倒退，提交时使用 `max(watermark, proposed_clock)`。

### 2.2 预算与轮内时间轴

一个候选的授予成本：

```text
cost = min(application.desired_hold_ms, resource.lease_duration_ms)
```

每轮预算约束：

```text
sum(admitted grant cost) <= config.round_budget_ms
```

调度器在 `[clock_ms, clock_ms + round_budget_ms]` 内推进内部时间轴。某锁键的租约到期后，同一轮可以再次授予，但每次授予必须：

1. 先在同一状态副本中完成回收，持有者占用减一；
2. 持久化租约代数加一；
3. 用一个严格更大的代数写入新授予。

轮次提交时仍活跃的租约保留在快照中；后续轮次在其 `expires_at_ms <= round_clock` 时回收。单个锁键同一时刻最多一个活跃租约。

## 3. 队列状态机

每个锁键维护 `queues[lock_key]`：

- `groups[label].seqs`：待授予申请，按 `(arrival_ms, seq)` 稳定排序。
- `positions`：跨标签的队列位置，标签按 `config.fairness_labels` 枚举顺序排列。
- `queued_count` / `scheduled_count`：已到期待授予数量与未来到达数量。
- `active_seq`、`active_holder`、`active_expires_ms`：当前租约摘要。

申请状态：

```text
scheduled -> queued -> granted -> expired
                    -> cancelled
                    -> abandoned
                    -> failed (stale generation)
```

事件：

- `enqueued` / `arrived`：记录位置与在队轮数。
- `granted`：记录授予时位置、等待轮数、代数、起止逻辑时钟与成本。
- `cancelled` / `abandoned`：记录操作原因与当时队列位置。
- `failed`：记录落后代数诊断。
- `expired`：记录回收逻辑时钟。

`wait_rounds` 只在轮次模拟末尾统一加一，并且只对已到期、仍为 `queued` 的申请推进。该模拟结果只有在整轮事务提交后才可见；因此崩溃回滚或幂等重放不会重复累计。

## 4. 候选选择

### 4.1 预处理

每轮开始时：

1. 回收 `expires_at_ms <= round_clock` 的租约，占用计数同步减一。
2. 将 `arrival_ms <= round_clock` 的 `scheduled` 申请转为 `queued`。
3. 对显式提供 `expected_lease_generation` 且落后于当前代数的申请标记 `failed`，写 `STALE_LEASE_GENERATION`，其他申请继续。

### 4.2 候选集

对每个无活跃租约的锁键：

- 每个公平标签提供一个队首候选；
- 若标签内存在达到饥饿阈值且未耗尽让渡额度的申请，则强制候选先于该标签普通队首；
- 候选必须满足当前持有者占用 `< max_concurrent_grants`；
- 候选成本必须不超过当前剩余预算。

跨锁键候选排序：

```text
forced                      降序（强制让渡优先）
label_round_grant_count     升序（本轮标签公平）
resource.priority           降序
application.priority        降序
wait_rounds                 降序
arrival_ms                  升序
seq                         升序
```

公平组轮转先于应用优先级填充，避免高优先级新流量持续占满预算。预算耗尽后未授予的到期申请写入 `deferred`，包含：

- `queue_position`
- `wait_rounds`
- `deferred_rounds`
- `first_trigger`：`lease_active`、`holder_cap`、`budget` 或 `fairness_quota`
- 对应约束细节

### 4.3 公平配额

`fair_quota_min_grants` 定义为“当轮存在足够候选时，应达到的最少授予数”。验证阶段会检查：

```text
fair_quota_min_grants * min_grant_cost <= round_budget_ms
```

运行阶段若实际授予数少于 `min(config.fair_quota_min_grants, candidate_count)`，提交隔离的 `FAIR_QUOTA_UNSATISFIABLE` 诊断，不中止其他锁键。

## 5. 饥饿让渡

判定条件：

```text
wait_rounds >= starvation_rounds
and forced_yields < max_forced_yields_per_application
```

申请在本轮授予成功后：

- `forced_yields += 1`；
- 输出 `threshold` 与触发时的 `wait_rounds`；
- 比较同锁键中优先级更高且未强制的申请；
- 输出 `compared_application_seq`、`compared_priority`；
- `estimated_avoided_delay_rounds = 1 + 更高优先级非强制竞争候选数`。

强制候选即使优先级低于新到申请，也在跨锁键排序中位于所有非强制候选之前。申请一旦授予即离开待授予队列，并且 `forced_yields` 永不超过配置上限。

## 6. 事务与快照协议

### 6.1 写入口

所有写操作：

1. 获取跨平台文件锁；
2. 打开 SQLite，设置 `WAL`、`busy_timeout` 与 `synchronous=FULL`；
3. 使用 `BEGIN IMMEDIATE` 获取写事务；
4. 在事务内读取 `current_snapshot` 指针与状态 JSON；
5. 检查期望版本；
6. 写入新状态；
7. 同一事务移动 `current_snapshot` 指针并提交。

### 6.2 两阶段轮次意图

`execute_round` 的第一事务只做：

```text
INSERT round_jobs(status='prepared', base_version, base_snapshot_id, payload)
```

第二事务：

```text
simulate_round(base state)
INSERT snapshots(version, state_hash, state_json)
INSERT rounds(...)
INSERT grants / actions / queue_events / round_errors
REPLACE current_snapshot(snapshot_id, version)
UPDATE round_jobs(status='completed', result_version)
COMMIT
```

因此：

- `prepared` 已提交、轮次状态未提交时强杀：恢复路径重放该意图。
- 第二事务中途强杀：SQLite 回滚全部行，基础快照仍当前，恢复后重算一次。
- 第二事务完成后强杀：作业和轮次都已提交，读取方看到完整快照。

测试注入点：

| 注入点 | 位置 |
| --- | --- |
| `after_prepare` | 意图事务提交后、状态事务前 |
| `after_base_read` | 状态事务读取基础快照后 |
| `mid_apply` | 轮次模拟后、写入快照前 |
| `before_commit` | 快照与指针写入后、COMMIT 前 |
| `after_commit` | COMMIT 后 |

### 6.3 幂等键

- `rounds.round_no`：一轮最多一个正式轮次快照。
- `grants(round_no, seq)`：同一申请同一轮只授予一次。
- `grants(lock_key, lease_generation)`：同一锁键同一代数只授予一次。
- `actions.idempotency_key`：`wait_advance`、`grant`、`expire`、`forced_yield` 等动作唯一。
- `round_jobs.request_id`：重复请求返回原结果并要求 `idempotent_replay=true`。

## 7. SQLite 结构

| 表 | 作用 |
| --- | --- |
| `snapshots` | 不可变完整状态 JSON、版本、轮次、哈希 |
| `current_snapshot` | 单行快照指针，读取方只读此入口 |
| `resources` / `holders` / `applications` | 当前提交状态的规范化镜像 |
| `grants` | 授予历史、代数、起止时钟、回收状态 |
| `round_jobs` | 两阶段意图与幂等结果 |
| `rounds` | 已提交轮次摘要 |
| `actions` | 动作级幂等键 |
| `queue_events` | 入队、到达、授予、过期、撤销、放弃、失败事件 |
| `round_errors` | 业务错误链与诊断字段 |
| `recovery_log` | 恢复动作审计 |

每次提交删除并重建 `resources`、`holders`、`applications` 镜像，与快照 JSON 完全一致；授予记录追加并更新历史状态。

## 8. 多进程一致性

- 文件锁覆盖本地所有编排实例的写临界区。
- SQLite `BEGIN IMMEDIATE` 与唯一索引提供第二层一致性。
- 同一请求并发执行时，第一个事务创建 `prepared` 作业，其余实例发现后重放同一作业或读取同一完成结果。
- 不同锁键/标签的并发 `submit` 使用当前指针做读改写，不丢更新。
- 读接口只读当前完整快照，不读取 `prepared` 作业或中间规范化行。

当前实现选择“全局轮次快照 + 串行写临界区”，而不是为每个锁键维护独立数据库。这样公平配额、持有者并发计数和逻辑时钟水位可以在单轮内一致裁决；不同实例仍可并发提交、竞争和读取，写入由文件锁与 SQLite 安全串行化。

## 9. 错误隔离

- 校验错误在创建 SQLite/锁文件前抛出。
- 落后租约代数在候选预处理中标记单申请失败，其他申请继续。
- 预算与公平配额问题写入本轮 `round_errors`，其他锁键的合法授予仍可提交。
- `_grant` 内部仍有占用上限与重复活跃租约不变量检查；若出现程序性不变量破坏，整轮回滚而不是写入半状态。
- HTTP/CLI 统一返回业务错误对象。

## 10. 测试映射

| 验收项 | 测试 |
| --- | --- |
| 输入校验与零写入 | `tests/test_validation.py` |
| 三重约束 | `test_budget_is_hard_cap`、`test_holder_cap_serializes_same_holder`、`test_fairness_quota_and_label_rotation` |
| 饥饿强制让渡 | `test_starvation_forced_yield_compares_high_priority_newcomer` |
| 租约代数与回收 | `test_lease_generation_and_atomic_release` |
| 错误码隔离 | `test_per_key_error_does_not_block_other_key`、`test_budget_cannot_satisfy_any_candidate`、`test_clock_rollback_code_and_no_commit` |
| 崩溃恢复 | `test_crash_after_prepare_replays_once`、`test_crash_mid_transaction_leaves_no_half_snapshot` |
| 多进程幂等 | `test_multiprocess_same_round_is_idempotent`、`test_multiprocess_submissions_on_distinct_keys_are_not_lost` |
| 快照完整性/确定性 | `test_committed_snapshot_hashes_are_immutable`、`test_deterministic_replay_across_databases` |
| 交互应用 | `test_http_dashboard_and_step_api`、`test_cli_compact_validation_and_error_json` |
