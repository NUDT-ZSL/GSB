# AlertLab 技术设计

## 1. 数据契约

### v1 快照

`samples/v1/rules_v1.json` 的顶层字段：

- `snapshot.snapshot_id`：来源标识。
- `snapshot.snapshot_version`：整个源快照版本，用于版本回退检测。
- `snapshot.logical_clock_ms`：裁决用快照逻辑时钟。
- `snapshot.format_priority`：同逻辑时钟下的来源优先级。
- `rules[]`：规则名、服务、实例、严重度、相对生效秒数、静默标签。
- `service_rules`：服务到规则名的扁平映射，必须与 `rules[]` 双向一致。

合法严重度为 `critical/error/high/warning/medium/low/info`。

### v2 索引、分片与元数据库

`samples/v2/index.json` 声明快照版本、逻辑时钟、格式优先级、SQLite 文件名，以及每个分片的 SHA-256。分片与 `metadata.sqlite3.v2_rules` 必须逐条一致；`v2_meta` 必须与索引一致。

`v2_rules` 按元数据插入顺序检查同一规范名的代数严格递增。墓碑在分片 JSON 中必须为布尔值，在 SQLite 中必须为 `0/1` 整数。

### 事件流

事件必须包含 `seq`、`instance_id`、`rule_name`、`severity`、`logical_clock_ms`、`metric_value`；可选 `labels`。`stream_id + seq` 决定稳定事件 UID。乱序时间戳合法；是否迟到由注入逻辑时钟水位决定。

## 2. 规则校验与跨版本裁决

所有来源在写入前完成解析与校验，校验函数不持有数据库连接。v1/v2 的规则名、服务、实例、严重度、依赖、静默、时间、墓碑分别使用稳定的 `E_*` 业务错误码。

裁决排序键：

```text
logical_clock_ms
generation
format_priority
source_version tie-break (v2 > v1)
source_ref
```

每条合并规则保存：

- `source_version`、`source_clock_ms`、`generation`、`format_priority`
- `original_time`
- `migration`
- `adjudication.chosen` 与 `rejected_candidates`

墓碑规则同样保存在规则视图中，但 `active_rules` 和聚合引擎会排除它。

## 3. 时间迁移

v1：

```text
effective_at_ms = injected_clock_ms + effective_after_seconds * 1000
scale_ms_per_unit = 1000
rounding_direction = exact
```

默认使用 v1 快照自身声明逻辑时钟作为注入基准；请求显式给出
`logical_clock_ms` 时使用该注入值。

v2：

```text
effective_at_ms = 源绝对毫秒值
scale_ms_per_unit = 1
rounding_direction = not_applicable
```

`migration_decisions` 的主键由来源、规则、代数、源时钟、注入时钟组成。重复决策若 JSON 不同则返回 `E_NONDETERMINISTIC`，否则按原记录复用。

## 4. 确定性降噪

事件先按 `(逻辑时钟毫秒, seq, UID)` 排序，再按 `(service, rule_name)` 分组。当前事件时间与上一事件时间之差大于 `window_ms` 时开新簇。

迟到判定使用流内已见最大事件时间和 `allowed_lateness_ms`：

```text
watermark_cutoff = max_seen_timestamp - allowed_lateness_ms
event.logical_clock_ms < watermark_cutoff -> late
```

已经提交过簇的实例/规则组合，新事件若落入已关闭簇的时间邻域，则标为 `late_closed_cluster`。迟到事件保留审计元数据，但不进入 `cluster_events()`。

代表事件固定排序：

1. 未静默优先。
2. 事件严重度降序。
3. 规则严重度降序。
4. 实例权重降序。
5. 事件时间升序。
6. `seq`、事件 UID 升序。

`suppressed_count = occurrence_count - 1`；其中静默导致的非代表事件单独记录为 `silence_suppressed_count`，其余为 `dedup_suppressed_count`。簇和候选均计算规范 JSON 哈希。

## 5. 根因候选

对簇所属服务计算上游闭包。v2 规则声明的直接上游覆盖拓扑直接上游，更深层继续使用拓扑；v1 使用拓扑。候选集合为闭包内每个服务的每个实例。

信号窗口：

```text
[cluster.first_event_ms - window_ms, cluster.last_event_ms + window_ms]
```

信号只使用未迟到、当前规则视图下有效且已到达注入时钟的事件。候选排序：

```text
has_signal desc
time_diff_ms asc
instance weight desc
dependency_distance asc
service asc
instance asc
```

每个候选保存参与实例、依赖路径、时间差、权重、最早信号、信号列表和固定排序说明。

## 6. 增量失效闭包

补丁只覆盖：

- `upstream_dependencies`
- `silence_labels`
- `effective_at_ms`

处理顺序：

1. 与当前值比较，剔除无实际变化的字段。
2. 仅依赖变化：簇摘要直接复用，候选重算。
3. 静默或生效时间变化：重新计算受影响规则的簇及候选。
4. 对所有未直接受影响簇也重算候选并与原值逐字段比较；若跨规则证据变化，只重算候选，簇摘要保持原文。
5. 以 `rule → instance → cluster → root_candidate` 路径写入 `invalidations`。
6. 未变化簇和候选复制原 JSON 与哈希，而不是重新序列化生成。

补丁结果在 `patch_before_commit` 前全部处于单事务中；崩溃时事务回滚，但 `patches.status = prepared` 保留，重启后可确定性重放。

## 7. 持久化、恢复与并发

核心表：

- `rule_views`、`view_pointer`：不可变规则版本与原子活动指针。
- `rules`、`migration_decisions`：统一规则视图和时间迁移审计。
- `view_results`：每个规则的结论修订号。
- `events`、`streams`、`ingest_runs`：原始事件、流配置和事件水位。
- `clusters`、`root_candidates`：带规范 JSON 和哈希的结论。
- `patches`、`invalidations`：补丁状态与失效路径。
- `commit_journal`：簇/补丁提交阶段。

簇按单簇事务提交。每簇先以独立事务写 `commit_journal(started)`，再在一个事务内写入簇、全部候选并标记 `committed`。强杀后：

- 已提交簇保留，恢复时比较摘要并跳过。
- `started` 但无簇记录的对象视为半提交，重算后补写。
- 同一 `(view_id, cluster_id)` 唯一约束阻止重复簇。
- 迟到和事件类型不会导致抑制计数二次累加。

快照替换先在校验通过的临时表中构造规则，再分配新 `view_id`、写不可变规则行，最后在同一事务内切换 `view_pointer`。低快照版本返回 `E_VIEW_ROLLBACK`；相同版本不同源内容返回 `E_SNAPSHOT_VERSION_CONFLICT`。

文件锁使用 `fcntl.flock`，并在同进程内提供可重入线程锁。SQLite 使用 WAL、`BEGIN IMMEDIATE`、15 秒 busy timeout 和唯一索引。公共 load/patch 操作在外层文件锁内串行化。

## 8. 稳定输出与校验

`status` 不包含系统墙钟。簇按 `(service, rule_name, first_event_ms, cluster_id)` 排序，候选按 `rank` 排序，原始 JSON 使用 `sort_keys=True`。

`verify` 检查：

- 活动视图和结果指针存在。
- 无墓碑簇。
- 每个簇和候选的规范 JSON 哈希。
- 排名从 1 连续。
- 用纯函数重放每个簇、根因候选和当前视图完整簇划分。
- 事件与逻辑时钟水位非负。
- 无未完成 ingest/patch。

## 9. 并发与幂等

- 相同快照内容只产生或复用唯一视图。
- 相同事件流在同一视图下产生固定 `run_id`、场景 UID 和簇 ID。
- 相同补丁 ID + 相同请求内容返回已存响应。
- 相同补丁 ID + 不同内容返回 `E_PATCH_CONFLICT`。
- 相同簇在同一视图只允许一行，候选与簇在同一事务写入。

多进程测试使用 4 个进程同时 load，再使用 4 个进程同时提交同一补丁，最终只有一个规则视图版本和一次补丁记录。
