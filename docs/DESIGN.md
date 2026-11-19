# 技术设计

## 1. 模块边界

| 文件 | 职责 |
| --- | --- |
| `alarm_dedup/parsing.py` | 拓扑、事件、补丁、窗口校验与规范化 |
| `alarm_dedup/engine.py` | 纯函数聚类、代表事件、上游闭包、候选排序 |
| `alarm_dedup/store.py` | SQLite schema、版本事务、增量失效、恢复、并发 |
| `alarm_dedup/canonical.py` | 去重键校验、规范 JSON、SHA-256 摘要 |
| `alarm_dedup/errors.py` | 稳定业务错误码与诊断字段 |
| `alarm_dedup/cli.py` | `load/ingest/patch/resume/status/verify` |

校验和计算层不读取系统时间，也不使用随机数。SQLite 自增 ID 只用于内部创建顺序，不进入确定性结论摘要。

## 2. 数据与版本

`topology_versions` 保存每个已提交拓扑的规范 JSON 和摘要。`meta` 中维护：

```text
schema_version
current_topology_version
current_result_version
event_watermark
cluster_window_ms
```

每一轮簇推进生成新的 `cluster_runs.result_version`。`clusters` 和 `candidates` 以 `(result_version, cluster_key)` 为主键，因此历史版本保留，当前读取只依赖 `current_result_version` 指针。

`event_suppressions` 是独立持久化的抑制账本。窗口撤销只改变 `windows.status`，不删除账本记录。

## 3. 输入校验顺序

1. 用 `object_pairs_hook` 拒绝重复 JSON 键，用 `parse_constant` 拒绝 `NaN/Infinity`。
2. 拓扑校验版本、服务唯一性、实例唯一性、依赖闭合、自环、实例归属、权重有限且为正。
3. 对依赖图做确定性 DFS，发现环时返回闭环路径 `cycle_path`。
4. 事件校验枚举、序号、时间戳、指标值、实例归属和事件内服务一致性。
5. 补丁校验版本、引用、窗口区间与最终拓扑环。
6. 所有解析/语义校验通过后才创建持久化 job。

因此非法输入不会写入拓扑、窗口、事件、簇或候选；已经存在的其他服务状态保持可读和可继续处理。

## 4. 聚类算法

输入事件集合按 `(service, rule)` 分组。对每组：

```text
events = sort(group_events, key=(logical_ts, event_seq))
cluster 边界: next.logical_ts - current.logical_ts > window_ms
```

`cluster_key = "C-" + sha256(service || rule || min(event_seq))[:24]`。事件序号严格递增，因此后续新事件不会改变已有锚点序号；若新事件使相邻簇合并，只重建对应 `(service, rule)` 组。

代表事件选择为字典序最小：

```text
(-severity_rank, -instance_weight, logical_ts, event_seq)
severity_rank: info=1, warning=2, critical=3
```

抑制事件仍保留在 `event_seqs`，但不参与代表选择。NumPy 的 `sum` 与 `dot` 只负责权重总量和加权严重度聚合。

## 5. 依赖闭包与候选排序

上游闭包使用按服务名排序的堆做最短距离 BFS。候选集包含目标服务和全部上游服务实例。每个候选的排序键：

```text
(
  has_own_unsuppressed_alert ? 0 : 1,
  earliest_alert_ts or MAX_INT,
  -instance_weight,
  causal_distance,
  instance_id
)
```

候选观测只使用抑制账本中未抑制的事件，按 `(logical_ts, event_seq)` 排序。`basis_text` 记录排名、最早观测时间、自身告警数、权重、因果距离和受影响实例数。顶层 `aggregation_order` 记录完整排序优先级。

## 6. 增量失效

### 补丁

在写事务中：

1. 解析并生成新拓扑，确定依赖变更服务、权重变更服务和窗口旧/新范围。
2. 更新窗口状态；用最终激活窗口扫描事件，仅把尚未抑制的事件写入抑制账本。
3. 对每个既有簇检查是否包含权重变更实例的事件，或旧/新窗口覆盖的事件。
4. 仅上述簇按持久化事件集合重建；其余簇原样复制。
5. 根因受影响服务闭包为所有变更服务的旧/新下游闭包；闭包内簇及被重算簇重建候选，其余候选原样复制。
6. 每个簇/候选写入一条失效决策日志。

### 事件增量

1. 重叠事件序号只有内容摘要一致才允许，否则 `EVENT_SEQUENCE_CONFLICT`。
2. 新事件服务对应的 `(service, rule)` 组重建；其他组复制。
3. 重建出的簇若摘要与旧簇逐位相同，继续复用旧簇并保留旧计算版本。
4. 新事件服务及下游闭包内的候选重建，其余复制。

## 7. 原子提交

每次结果推进：

```text
BEGIN IMMEDIATE
  upsert stage_clusters / stage_candidates
  INSERT INTO clusters SELECT ... FROM stage_clusters
  INSERT INTO candidates SELECT ... FROM stage_candidates
  INSERT invalidation_log
  INSERT cluster_runs
  UPDATE meta.current_result_version / topology_version / event_watermark
  UPDATE jobs.status = done
COMMIT
```

崩溃发生在 `COMMIT` 前时，SQLite 回滚所有正式表、临时表和 job 状态；崩溃发生在提交后时，状态完整且 `resume` 不再执行该 job。

## 8. 并发与幂等

- 写路径：进程级 `<db>.lock` 排他锁 + SQLite `BEGIN IMMEDIATE`。
- 事件批次：`event_batches.batch_id` 主键。批次 ID 为规范化事件的 SHA-256。
- 补丁：`patch_history.patch_id` 主键；同 ID 不同摘要报 `PATCH_ID_CONFLICT`。
- 任务：`jobs.job_id = kind + ":" + payload_digest`，同一载荷只对应一个任务。
- 结果：`(result_version, cluster_key)` 唯一，重复提交不会重复计入簇。

`status`/`verify` 使用读事务，不获取写文件锁；WAL 下读取方只看到完整提交的版本指针与结果集。

## 9. 恢复

`jobs` 在业务事务前单独提交为 `pending`。业务成功时 `done` 与结果同事务提交。进程被 `SIGKILL` 后：

- 尚未开始业务事务：job 仍为 pending，`resume` 执行；
- 业务事务中途：SQLite 回滚，job 仍为 pending，`resume` 执行；
- 业务事务已提交：job 为 done，`resume` 不重复执行。

事件去重与批次/补丁幂等作为第二道保护，保证恢复重放不会重复计入。

## 10. 验证方法

`verify` 不信任当前簇/候选表，而是：

1. 从 SQLite 读取当前拓扑、全部原始事件和抑制账本；
2. 在内存中全量聚类并计算所有候选；
3. 比较簇核心字段、簇摘要、候选 payload、候选摘要；
4. 检查事件水位、抑制账本完整性、结果指针、stage 表和 SQLite 完整性。

该路径与增量路径独立，因此能发现失效闭包漏算、错误复用或摘要不一致。
