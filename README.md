# Local Columnar Pushdown Engine

这是一个不依赖网络或容器的本地列式计算引擎。它读取固定的列式 JSON 批次、逻辑计划和执行配置，在写入任何持久化状态之前完成输入校验与计划改写，然后使用 NumPy 块运算执行过滤、投影、分组聚合与跨分片滑动窗口。

## 环境

当前实际验收环境：

- WSL2 / Linux 6.18
- Python 3.11.15
- NumPy 2.4.6
- pytest 9.2.0

任务文字中的目标是 Windows。代码按 Python 3.11+ 编写，SQLite 使用标准库，文件锁在 POSIX 上使用 `fcntl`、在 Windows 上使用 `msvcrt`，因此可直接在 Windows 运行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m columnar_engine.cli init --db data\engine.db --work-dir var
```

Linux/WSL 下：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pytest
.venv/bin/python -m columnar_engine.cli init --db data/engine.db --work-dir var
```

## 快速运行

```bash
.venv/bin/python -m columnar_engine.cli verify-rewrite --sample samples/default
.venv/bin/python -m columnar_engine.cli run --sample samples/default --db data/engine.db --work-dir var
.venv/bin/python -m columnar_engine.cli run --sample samples/default --db data/engine.db --work-dir var
.venv/bin/python -m columnar_engine.cli resume --sample samples/default --db data/engine.db --work-dir var
```

`run` 与 `resume` 都按输入摘要生成稳定 `run_id`。同一输入重复调用是幂等的：已提交分片不会再次累计，`committed_this_call` 为空，最终结果摘要保持不变。

生成完整验收报告：

```bash
.venv/bin/python scripts/acceptance.py
```

输出写入 `reports/acceptance.txt` 与 `reports/acceptance.json`。

## 样本格式

固定样本位于：

- `samples/default/batches.json`
- `samples/default/plan.json`
- `samples/default/config.json`
- `samples/budget_abandon/config.json`

批次节点包含 `batch_id`、`rows` 和 `columns`。列可写为 `{"type":"int64","values":[...]}`，类型支持 `int64`、`float64`、`bool`。校验包括：

- JSON 对象不得有重复键，列名必须唯一。
- 每列长度必须等于批次 `rows`，跨批次列集合和类型必须一致。
- `batch_id` 必须严格递增。
- 计划必须有一个根、一个扫描节点、无环，且每个节点最多一个父节点。
- 节点引用的列和输入节点必须存在。
- 窗口 `length`、`step` 为正整数且 `step <= length`。
- 定点标度为 `0..18` 的整数。

非法输入返回 `E_BAD_BATCH`、`E_BAD_PLAN`、`E_PLAN_CYCLE`、`E_PLAN_REF` 或 `E_BAD_CONFIG`，且校验和改写发生在 SQLite 初始化之前，不会写入持久化状态。

## 改写

`rewrite_plan` 执行两类改写：

1. 从扫描列集中删除所有未被过滤、投影、聚合或窗口引用的列。
2. 将第一个有状态节点之前的、仅依赖扫描列的过滤谓词合并到扫描节点的 `pushed_predicates`，删除原过滤节点并重连计划。

报告包含扫描列改写前后差异、最终输出列差异、被删除列、被删除过滤节点以及 `before_scan_apply_predicate` 下推位置。

改写后会在内存参考执行器中与原计划逐分片执行，对最终结果和所有有状态节点结果按 dtype 与原始字节比较。任何差异返回 `E_REWRITE_MISMATCH`，并报告首个差异节点、列、行和值。

## 执行语义

- 分片按 `vector_width` 行切分，扫描任务并行执行。
- 过滤、投影、分组和窗口使用 NumPy 整块数组运算。
- 聚合按 `shard_id` 升序合并，每个分片内部保持稳定行序。浮点加法顺序固定，因此重复运行逐位一致。
- 滑动窗口以每个分区内的完整窗口计算；窗口起点为分区内 `0, step, 2*step, ...`，仅输出完整窗口。跨分片保留 `length-1` 行历史，并在窗口完成的那个分片中输出。
- 同一分片内按 `order_by` 稳定排序；跨已提交分片若分区 `order_by` 回退，返回 `E_WINDOW_ORDER`。
- NaN 规则为 `IGNORE_AND_COUNT`：`count` 不计 NaN，`sum/avg/sumsq` 忽略 NaN；全 NaN 的 `min/max/avg` 输出 NaN。
- 定点规则为 `ROUND_HALF_AWAY_FROM_ZERO`：先按二进制 float64 乘以 `10^scale`，再向远离零方向舍入；NaN 转为 0 并计数。定点结果超过 int64 返回 `E_FIXED_OVERFLOW`。

## 调度与预算

调度器按分片 ID 顺序领取任务，并在两个边界下裁决：

- 单个扫描任务估算内存不得超过 `scan_memory_budget_mib`。
- 所有活跃租约总内存不得超过 `global_memory_budget_mib`。
- 同时活跃任务数不得超过 `max_parallel_tasks`。
- 单逻辑轮允许启动数受 `round_time_budget_ms / logical_clock_step_ms` 限制。

分片结局明确区分：

- `COMMITTED`：启动并完整提交。
- `FAILED`：启动后得到明确异常。
- `ABANDONED`：启动前因预算主动放弃并记录原因。
- `NOT_STARTED`：因同批分片失败等调度决策未启动。
- `RESULT_READY`：计算完成但尚未满足状态提交水位。

已启动线程不会被取消，也不会用事后超时标注替代资源隔离。预算在领取前裁决，租约写入 SQLite，防止并发实例超卖。

## 持久化、提交与恢复

SQLite 表包括：

- `runs`：计划、配置、改写报告、逻辑时钟、最终结果摘要。
- `shards`：分片范围、估算内存、状态、原因、内容摘要、payload 路径。
- `node_states`：窗口/聚合累计状态、状态版本、分片水位、状态摘要。
- `commits`：`(run_id, node_id, shard_id)` 唯一提交记录。
- `node_outputs`：窗口与聚合输出及逐结果误差上界。
- `leases`：并发内存租约。
- `errors`：结构化业务错误链。

每个分片的窗口/聚合状态、水位、摘要与输出在同一 SQLite `BEGIN IMMEDIATE` 事务中提交。分片 payload 先原子写入临时文件再 `os.replace`，同时写出 `.sha256` 摘要文件；提交前同时校验 payload 和摘要文件。摘要不一致返回 `E_DIGEST_MISMATCH` 且事务回滚。

恢复路径会：

- 删除死亡进程留下的租约，把 stale `RUNNING` 分片重置为 `PENDING`。
- 跳过 `COMMITTED` 分片。
- 优先提交已有 `RESULT_READY` 分片。
- 只重新执行缺失分片。
- 从持久化状态继续累计，不重复加入已提交的窗口历史或聚合计数。

强制终止验收使用测试选项 `--crash-after-commits N` 或 `--crash-after-results N`，在真实事务提交后调用 `os._exit(137)`，不做清理。

## 并发一致性

多实例通过两层机制协作：

- 文件锁：每个 `run_id` 一条锁，串行化领取、结果登记、状态提交。
- SQLite 事务：`BEGIN IMMEDIATE`、唯一主键、`WAL`、`synchronous=FULL`、busy timeout。

同一分片同一算子版本只允许一个 `commits` 主键。重复提交相同摘要返回幂等结果，不同摘要返回 `E_COMMIT_CONFLICT` 或 `E_DIGEST_MISMATCH`。读取快照只返回已提交分片对应的节点状态。

## 误差诊断

每个聚合和窗口数值结果在 `node_outputs` 中保存：

- 结果值。
- 误差上界。
- 误差来源链。
- 定点标度、舍入方向、NaN 规则和聚合顺序。

误差谱会比较同列的两个分组或两个窗口结果。当差值小于 `float_tolerance` 时标记 `precision_sensitive` 和 `decision_ambiguous`，并输出双方来源链。定点/浮点双通路会按 `10^scale` 归一化定点值，与浮点 `sum` 比较差值。

## 业务错误码

| 错误码 | 含义 |
|---|---|
| `E_BAD_BATCH` | 列式批次非法 |
| `E_BAD_PLAN` / `E_PLAN_REF` / `E_PLAN_CYCLE` | 计划结构、引用或环非法 |
| `E_BAD_CONFIG` | 配置或标度非法 |
| `E_REWRITE_MISMATCH` | 改写后语义或位级结果不一致 |
| `E_DIGEST_MISMATCH` | 分片 payload 或算子状态摘要不匹配 |
| `E_FIXED_OVERFLOW` | 定点换算或累计溢出 int64 |
| `E_BUFFER_BUDGET` | 窗口/聚合持久化缓冲超预算 |
| `E_CLOCK_REGRESSION` | 逻辑时钟回拨 |
| `E_COMMIT_CONFLICT` | 同一分片算子版本提交了不同内容 |
| `E_SHARD_FAILURE` | 已启动分片执行异常 |
| `E_WINDOW_ORDER` | 跨分片窗口排序键回退 |

## 测试与验收

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/acceptance.py
```

测试覆盖非法输入零写入、列裁剪与下推等价、并行预算裁决、三类分片结局、摘要篡改、定点溢出、缓冲超预算、逻辑时钟回拨、硬终止续算、并发实例提交与误差诊断。实际重复运行输出保存在 `reports/acceptance.txt` 和 `reports/acceptance.json`。

最近一次实际结果（Python 3.11.15 / Linux）：

```text
Sample run: COMPLETE, shards={'COMMITTED': 6}, digest=948dc2f76153c5fa3efb75cded2d054754e2e808a62741d6fd7062cb9cc2d859
Rewrite: removed=['qty'], pushed=['filter_active'], equivalent=True
Repeat run: COMPLETE, newly_committed=[], same_digest=True
Budget: ABANDONED_PARTIAL, abandoned=[{'shard_id': 0, 'reason': 'SHARD_EXCEEDS_SCAN_MEMORY_BUDGET'}]
Recovery: crash_exit=137, resume=COMPLETE, same_digest=True
Failure isolation: {'COMMITTED': 1, 'FAILED': 1, 'NOT_STARTED': 3, 'RESULT_READY': 1}, watermarks={'agg1': 0, 'window1': 0}
Concurrency: committed_sets=[[0, 1, 5], [2, 3, 4]], disjoint=True
Diagnostics: precision_sensitive=29, rounding=ROUND_HALF_AWAY_FROM_ZERO
```

`pytest -q` 当前为 `24 passed`。

