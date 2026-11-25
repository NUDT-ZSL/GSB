# 技术设计与需求追踪

## 1. 模块结构

- `cache_ledger/models.py`：样本数据契约、严格类型/引用/摘要/哈希/代数校验。
- `cache_ledger/samplegen.py`：固定种子样本与 v2 SQLite 元数据库生成器。
- `cache_ledger/merge.py`：v1/v2 时间换算、版本裁决、统一视图和失效闭包。
- `cache_ledger/engine.py`：热度分、层级选择、容量匹配、受害者排序和阈值判定。
- `cache_ledger/store.py`：SQLite schema、事务决策、容量账本、pending op 与命令日志。
- `cache_ledger/lock.py`：`fcntl` 跨进程排他文件锁。
- `cache_ledger/runner.py`：`merge/admit/rebalance/evict/resume` 用例编排。
- `cache_ledger/verify.py`：守恒检查、全新库全局重放、首个分歧定位。
- `cache_ledger/cli.py` / `cache_ledger/__main__.py` / `main.py`：命令行入口。
- `tests/test_cache_ledger.py`：8 个标准库 `unittest` 用例。

## 2. 数据校验

所有输入先完整解析，再打开/修改运行账本。主要错误码包括：

- `E_DUPLICATE_TENANT`、`E_INVALID_QUOTA_RATIO`、`E_INVALID_QUOTA_BLOCKS`
- `E_ENTRY_SEQUENCE`、`E_DUPLICATE_ENTRY_KEY`、`E_DIGEST_MISMATCH`
- `E_INVALID_LAYER`、`E_UNDECLARED_TENANT`、`E_UNDECLARED_POLICY_KEY`
- `E_INVALID_REBALANCE_THRESHOLD`
- `E_V2_SHARD_HASH_MISMATCH`、`E_POLICY_GENERATION_NOT_MONOTONIC`
- `E_CLOCK_ROLLBACK`（独立 CLI 退出码 10）
- `E_POLICY_GENERATION_ROLLBACK`（退出码 11）
- `E_VERIFY_MISMATCH`（退出码 12）

条目摘要算法为：

```text
sha256("cache-ledger-block:v1:{seq}:{key}:{block_size}")
```

因此摘要同时绑定序号、键和块大小。v2 分片文件的 SHA-256 必须与元数据库
`shards.sha256` 完全一致；分片按 `shard_index` 排序后的 `generation` 必须单调不减。

## 3. 策略合并与定点时间

v1 的相对秒数按题义在注入逻辑时钟下换算：

```text
absolute_ms = injected_clock_ms + relative_effective_seconds * 1000
```

每条候选策略保留：

- 来源格式和来源版本；
- 原始相对秒或绝对毫秒表示；
- `seconds_to_ms=1000`、`fixed_point_scale_per_ms=1000`、`fixed_tick_ms=64`；
- 向下取整的 tick 序号和 `floor_for_tick_index;exact_for_milliseconds` 舍入说明；
- `absolute_ms % fixed_tick_ms` 的子定点单位余量；
- `[absolute_ms, remainder_ms, fixed_point_ticks]` 比较键，使余量参与同 tick 比较；
- 裁决依据文本和全部候选版本。

同一键的裁决顺序为 `(effective_at_ms, generation, source_priority, source)`。
即先比较声明逻辑时钟，同时钟同代数时 v2 来源优先级高于 v1；同时钟但代数不同则高代数获胜。

## 4. 增量失效闭包

闭包图显式遍历以下引用边：

```text
policy key -> cache entry -> tenant quota -> current layer ownership
```

实现会输出：

- `changed_policy_keys`
- `direct_entry_keys`
- `affected_tenants` / `affected_layers`
- `recompute_entry_keys` / `reused_entry_keys`
- 租户配额、层摘要和决策类型等需重估的账本域
- 每条图边 `graph_edges`

若策略层变化，旧/新目标层和当前层的全部归属条目进入闭包；若租户配额受影响，该租户
全部条目进入闭包。闭包外条目的策略签名、租户域和层归属均未变化，因此可安全复用。
测试 `test_incremental_closure_reuses_unaffected_entries` 对两个全局流程的最终
`(status, current_layer)` 做了逐键比较：闭包外条目必须完全一致。

初始 v1-only 到 v1+v2 的样本差异较大，闭包可能覆盖全部 36 条；局部补丁
`samples/policy_update_example.json` 会产生非空复用集合。

## 5. 热度与确定性准入

热度分用一个精确整数编码固定顺序：

```text
heat_score = last_access_ms * 2^64 + access_count
```

因为样本中的 `access_count < 2^64`，排序等价于先按最后访问逻辑时钟、再按访问计数。

准入不依赖多进程调度顺序。`build_admission_plan()` 始终从空占用开始，按条目序号做一次
全局确定性分配：

1. 墓碑策略 -> deny；
2. 失效标记忽略旧层映射，改由热度分层；
3. 普通策略使用策略层；无策略使用热度层；
4. 目标层不可行时依次尝试更低的 L1 -> L2 -> L3；
5. 租户硬配额、层块上限和总字节预算同时满足才准入，否则 defer。

每个 `--tenant` 进程得到同一份全局计划，只提交自己的租户分片。计划中的总分配满足全部
硬上限，因此任意进程交错都不会超扣，也不会改变决策载荷。

## 6. 重平衡、冷却和预算

重平衡只从 L1 -> L2、L2 -> L3 降级，候选顺序为：

```text
policy urgency (tombstone/invalid),
-eviction_priority_weight,
heat_score ascending,
seq ascending,
key
```

即更大的逐出优先级权重优先成为逐出/冷迁移候选，冷数据和更早序号进一步固定顺序。

每个候选逻辑成本：

```text
cost_ms = migration_base_cost_ms + block_size * migration_block_cost_ms
```

调度器每轮维护剩余逻辑预算，并严格输出三类结局：

- `not_started`：预算仍可讨论，但因冷却窗口、目标层容量或阈值恢复而未启动；
- `failed_after_start`：已创建 prepared op/逻辑启动，但提交失败或命中 `--fail-key`；
- `abandoned`：剩余不可分割候选成本无法纳入预算，或预算耗尽，主动放弃。

放弃记录包含候选键、受影响租户、源/目标层、原因、候选成本和剩余预算。

总字节预算压力被显式计算；由于跨层迁移不改变字节总量，字节压力下引擎会降低有效重平衡
阈值以先做冷层沉降，并由 `evict` 命令真正回收字节。该约束在报告中以
`byte_budget_pressure` 标明。

## 7. 容量账本和决策序号

SQLite 表包括：

- `entries`：原始条目、状态、当前层、策略代数、迁移时钟和决策指针；
- `tenant_usage` / `layer_usage`：物化容量摘要；
- `decisions`：从 1 开始的连续决策序号和唯一 `idempotency_key`；
- `policy_versions`：策略整体视图、闭包、代数和指纹；
- `pending_ops`：prepared 半提交操作；
- `command_log`：命令请求及 running/completed/failed 状态；
- `run_reports`：准入/重平衡/逐出报告。

每个决策在一个 `BEGIN IMMEDIATE` 事务内完成：

1. 幂等键检查；
2. 分配下一个连续 `seq`；
3. 条件式容量/状态变更；
4. 插入决策和策略指针；
5. 更新逻辑时钟水位；
6. 重算租户、层、字节和序号守恒断言；
7. 原子提交。

提交后任何时刻都满足：用量非负、租户用量等于 admitted 条目求和、层用量等于条目求和、
租户不超硬配额、层不超块上限、总字节不超预算、决策序号无缺口。

## 8. 并发与恢复

跨进程写入使用 `state/cache.sqlite3.lock` 上的 `fcntl.flock` 短临界区，并配合
SQLite `BEGIN IMMEDIATE`、`busy_timeout=30000`、`synchronous=FULL`。这保证：

- 决策序号主键和 `idempotency_key UNIQUE` 使同一决策只提交一次；
- 策略指纹主键和 `policy-merge:<fingerprint>` 幂等键使同一策略版本只整体替换一次；
- 容量检查与更新在同一序列化写事务内完成，不会并发超扣。

崩溃模型：

- `CACHE_LEDGER_CRASH_AFTER_PREPARE_KEY=<idem>`：pending op 提交后、决策提交前退出；
- `CACHE_LEDGER_CRASH_BEFORE_COMMIT_KEY=<idem>`：账本 SQL 已执行但 SQLite commit 前
  `os._exit(86)`，未提交事务由 SQLite 回滚；
- `CACHE_LEDGER_CRASH_AFTER_COMMITS=N`：第 N 个新决策提交后立即退出；
- `CACHE_LEDGER_HANG_AFTER_COMMITS=N` + marker：提交 N 个决策后挂起，可供外部
  `kill -9` 做真实强制终止演练。

`resume` 先逐个提交 `pending_ops.status='prepared'`，再按 `command_log.request_id`
重放所有 `running` 请求。已提交的幂等键直接返回原决策，因此不会重复扣账。

## 9. 全局重放验证

`verify` 将样本重新载入一个全新的临时 SQLite，然后按顺序重放所有已完成命令请求：

- 顺序 CLI 流程使用 `exact_decision_sequence` 模式，逐决策比较
  `seq/idempotency_key/kind/tenant/entry/clock/policy/payload`，并比较最终状态；
- 多进程租户流程允许全局序号交错，自动使用
  `semantic_idempotency_key_concurrent` 模式，按幂等键比较全部决策载荷和业务字段；
- 发现真实分歧时输出 `first_divergent_decision_seq`、`entry`、`fields`，并在错误详情中
  保留 expected/actual 或语义差异；
- 临时重放库在结束后删除。

## 10. 无墙钟原则

业务模块不调用 `time.time()`、`datetime.now()` 或系统随机源。所有裁决时间来自：

- 条目自带的 `last_access_ms`；
- 命令 `--clock-ms` 或样本中的确定性默认时钟；
- 已提交的逻辑时钟水位。

固定种子随机数只用于离线样本生成。挂起故障钩子中的等待仅用于测试 `SIGKILL`，不参与
任何时间比较。

## 11. 需求追踪矩阵

| 需求 | 实现位置 | 主要验证 |
| --- | --- | --- |
| 输入解析校验 | `models.py` | `test_invalid_digest_is_rejected_before_load` |
| 两代策略合并/时间换算 | `merge.py` | `test_sample_validation_and_merge_adjudication` |
| 增量失效闭包 | `merge.py::compute_closure` | `test_incremental_closure_reuses_unaffected_entries` |
| 注入时钟、热度、准入和回拨错误 | `engine.py`、`runner.py` | `test_clock_rollback_has_independent_code_and_no_new_decision` |
| 重平衡预算/冷却/失败分类 | `runner.py::run_rebalance` | `test_start_failure_is_distinguished_from_abandonment` |
| 容量账本和全局重算一致 | `store.py`、`verify.py` | `test_canonical_flow_is_deterministic_and_verifies` |
| 强制终止恢复 | `pending_ops`、`runner.resume` | `test_prepared_half_commit_recovers_and_verifies`、`VALIDATION.md` SIGKILL 演练 |
| CLI 和多进程隔离 | `cli.py`、`lock.py` | `test_parallel_tenant_admission_is_unique_and_consistent` |
