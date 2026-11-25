# 技术设计

## 确定性边界

- 分片裁决只使用事件/注入中的 `logic_clock_ms`、`expected_commit_ms` 和由二者导出的阶段耗时。
- 核心引擎不读取 Python 系统墙钟；SQLite 中也不使用 `datetime('now')` 参与完成时刻。
- 所有候选按 `(node_id, shard_index, layer, phase_rank)` 稳定排序。
- 所有 JSON 输出使用 `sort_keys=True`，账本摘要使用规范化 JSON + SHA-256。

## 输入契约

种子生成器：

```bash
python samples/generate_samples.py
# 或
python main.py generate --out samples --seed 417
```

生成：

- `samples/training_plan.json`
- `samples/nodes.json`
- `samples/gradient_events.jsonl`
- `samples/injections.json`

解析器兼容常见别名字段。权威字段如下：

- 计划：`layers[].name`、`layers[].shards[].index|byte_size`、`parameter_bytes`、`sharding.method`
- 节点：`nodes[].node_id|address|compute_ratio|memory_limit_bytes`
- 事件：`event_seq|logic_clock_ms|node_id|layer|shard_index|spec_version|expected_commit_ms`
- 注入：`seq|type|logic_clock_ms|node_id|spec`

校验全部发生在打开/重建 SQLite 之前，因此非法输入不会写入新的持久化状态；若数据库已存在，也不会被覆盖。

## 两阶段 AllReduce 模型

每个唯一的 `(spec_version,node_id,layer,shard_index)` 上报产生一个幂等报告键：

1. 首次上报写入 `reports`，并记录 `source` 持有。
2. 调度 `reduce_scatter` 候选，完成后把持有形态迁移为 `reduced`。
3. 下一轮调度 `all_gather` 候选，完成后迁移为 `gathered`。
4. 当同一 `(spec,layer,shard)` 的所有报告节点都完成某阶段时，写入全局阶段完成集。
5. 层/阶段游标只推进连续从零开始的前缀；乱序完成会进入完成集合，但不会跳过缺口推进游标。

重复上报命中 `reports` 主键，返回 `duplicate=true`，不会重复增加持有或内存。

## 预算与结局码

- `SYNC_COMPLETED`：候选在本轮启动并按注入时钟完成。
- `BUDGET_EXHAUSTED`：剩余时间预算不足，本轮终止，后续候选全部放弃到下一轮。
- `DEFERRED_CONCURRENCY`：达到每轮并发上限，正常顺延。
- `MEMORY_LAG`：轮末审计发现节点内存超过上限，超额持有被标记并阻断该节点后续新候选。
- `NODE_FAULT_LAG`：节点故障时未完成持有回滚为待处理。
- `STALE_SPEC_REJECTED`：规格整体替换后收到旧规格上报，不写入完成集合。
- `SPEC_REPLACED`、`NODE_RECOVERED`：规格/成员状态变迁事件。

## 规格整体替换

规格版本存储在 `specs`，所有报告、阶段完成、候选、持有和账本均带 `spec_version`。

替换事务会：

1. 单调校验新版本；
2. 将旧规格标记为 `retired`；
3. 激活新规格并初始化新游标；
4. 将旧持有置为非当前活跃（行保留），旧完成集/游标保留历史；
5. 后续只从新规格的报告推导待处理候选；
6. 拒绝旧规格的新完成上报。

因此不同规格下同一节点/分片的状态由复合主键分账。

## SQLite 与恢复

数据库默认路径为 `state/gradient_engine.db`。关键表：

- `rounds`：轮次状态、预算、并发数、规范化报告和摘要。
- `round_candidates`：每个候选的稳定顺序、结局码、成本和完成时刻。
- `reports`：首次上报幂等键。
- `phase_completions` / `global_phase_completions` / `cursors`
- `holdings` / `memory_ledger`
- `lag_records` / `takeovers`
- `injections` / `specs` / `recovery_events`

每轮先提交 `status='prepared'` 的轮次标记，再在单个 SQLite 事务中提交候选、完成集、游标、持有、内存账本、逻辑时钟水位和 `committed` 状态。

崩溃演练 failpoint：

```bash
python main.py run --db state/crash.db --crash-before-commit-round 3
# 进程以 86 退出；prepared 第 3 轮保留
python main.py resume --db state/crash.db
```

恢复时把未提交轮次写入 `recovery_events` 后删除半提交轮次标记，再依据已提交的 `reports`、`phase_completions` 和 `holdings` 重放。已完成阶段不会再次执行，持有表的复合主键保证内存不重复累加。

## Verify

`verify` 从 SQLite 读取持久化输入（包括后续手动注入），在内存库中确定性重放一份参考轨迹，然后比较：

- 每轮完整规范化报告；
- 每节点每轮内存账本和摘要；
- 轮次数和账本条目数。

不一致时返回 `first_mismatch_round`、`first_mismatch_event_seq`、两侧轮次/账本摘要；命令退出码为 `1`。
