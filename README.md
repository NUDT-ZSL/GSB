# GSB 本地列式分组聚合诊断工作台

本项目从零实现一个不依赖外部数据源的本地列式分组聚合引擎，重点处理倾斜热点键下的逻辑时延预算、逐位结果一致性、内存溢出回读、强制终止恢复和多进程幂等提交。

核心约束是：**加速通路不能改变精确通路的任何行序或浮点结果**。实现采用“批次内固定行序聚合 + 批次序号升序两阶段合并”，精确通路和热点分流通路共享完全相同的算术顺序；一致性检查使用浮点位模式比较，配置的 `float_tolerance` 只记录，不参与差异掩盖。

## 环境

- Python `3.11.15`
- numpy `1.26.4`
- sqlite3 `3.53.1`

本仓库使用本地虚拟环境验证：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
```

引擎本身只依赖 Python 标准库；numpy 作为任务环境依赖声明并已实际安装验证。

```bash
.venv/bin/python scripts/doctor.py
```

## 快速运行

固定倾斜样本已提交在 `samples/tilted`，也可以重新确定性地生成：

```bash
.venv/bin/python scripts/make_sample.py
```

清空旧状态并运行：

```bash
.venv/bin/python -m gsb run \
  --sample samples/tilted \
  --db .demo/state.sqlite \
  --work .demo/work \
  --out .demo/out
```

查看稳定排序的诊断 JSON：

```bash
.venv/bin/python -m gsb status \
  --db .demo/state.sqlite \
  --work .demo/work
```

校验持久化状态、双通路结果和输出文件：

```bash
.venv/bin/python -m gsb verify \
  --sample samples/tilted \
  --db .demo/state.sqlite \
  --work .demo/work \
  --out .demo/out
```

恢复使用 `resume`：

```bash
.venv/bin/python -m gsb resume \
  --sample samples/tilted \
  --db .demo/state.sqlite \
  --work .demo/work \
  --out .demo/out
```

`run` 默认清理 SQLite、临时目录和输出目录；并发或续跑时使用 `--no-reset`。

## 输入格式

样本目录必须包含 `config.json`、查询描述以及 `batches/*.json`。支持 `queries.json`、`query.json`、`queries/*.json` 等查询文件布局。

### 执行配置

```json
{
  "time_budget_ms": 36.0,
  "max_batch_rows": 2500,
  "hot_key_min_rows": 2000,
  "skew_ratio": 0.15,
  "memory_limit_mib": 0.02,
  "fixed_scale": 1000,
  "float_tolerance": 1e-12,
  "logical_clock_step_ms": 1.5,
  "exact_cost_ms_per_1k_rows": 5.0,
  "accelerated_cost_ms_per_1k_rows": 0.8,
  "switch_overhead_ms": 1.0,
  "digest_algorithm": "sha256"
}
```

`time_budget_ms`、`max_batch_rows`、`hot_key_min_rows`、`skew_ratio`、`memory_limit_mib`、`fixed_scale`、`float_tolerance`、`logical_clock_step_ms` 必须存在且为正；`max_batch_rows` 和 `hot_key_min_rows` 必须为正整数，`skew_ratio <= 1`，加速每千行成本不得高于精确成本。常见别名字段也会归一化，例如 `latency_budget_ms`、`max_rows_per_batch`、`hot_threshold_rows`、`memory_limit_mb`、`decimal_scale`。

### 列式批次

```json
{
  "batch_index": 1,
  "row_count": 3,
  "columns": {
    "region": ["hot-A", "hot-B", "hot-A"],
    "amount": [1.25, 2.5, 3.75],
    "mark": [1, 0, 1]
  },
  "digest": "<sha256>"
}
```

默认摘要算法是：

```text
sha256(canonical_json({
  "batch_index": batch_index,
  "row_count": row_count,
  "columns": columns
}))
```

`canonical_json` 使用 UTF-8、键排序、紧凑分隔符并保留 `NaN`/`Infinity` 的标准 JSON 表示。校验器也兼容仅覆盖 `columns`、`columns + row_count` 或列对象数组的 SHA-256 摘要变体；声明摘要与内容不匹配时仍返回 `E_BAD_BATCH`。

### 聚合查询

```json
{
  "queries": [
    {
      "query_id": "tilted-main",
      "group_key": "region",
      "aggregates": [
        {"name": "amount_sum", "op": "sum", "column": "amount"},
        {"name": "amount_avg", "op": "avg", "column": "amount"},
        {"name": "amount_var", "op": "var", "column": "amount"},
        {"name": "amount_count", "op": "count", "column": "amount"},
        {"name": "rows", "op": "count", "column": "*"}
      ]
    },
    {
      "query_id": "tilted-window",
      "group_key": "region",
      "aggregates": ["sum(amount)", "count(*)"],
      "filter": {"column": "mark", "op": "eq", "value": 1},
      "window": {"length": 4000, "step": 3500}
    }
  ]
}
```

支持 `sum`、`avg`、`var`、`count`、`sumsq`，聚合表达式字符串和对象两种写法均可。窗口按全局行偏移滑动，只输出完整窗口；`length`、`step` 必须为正整数且 `step <= length`。

## 校验与错误码

所有输入在打开或重置 SQLite 之前完成校验；非法输入不会写入持久化状态，也不会清理已有数据库。

- `E_BAD_CONFIG`：配置缺失、类型错误、非正预算/标度/容差、非法倾斜比例等。
- `E_BAD_BATCH`：列名重复、数组长度不匹配、批次行数超限、序号不严格递增、摘要不一致、分组键为 `null`/NaN/无穷或含不可比较类型。
- `E_BAD_QUERY`：查询 id 重复、引用列不存在、数值聚合列类型不可比、窗口非法、聚合名称重复等。
- `E_STATE_CONFLICT`：现有 SQLite 状态对应的样本、查询或配置指纹不同。
- `E_BATCH_CONFLICT`：同一序号已提交不同内容摘要。
- `E_MEMORY_BUDGET`：并发内存租约已占满配置上限。
- `E_CORRUPT_STATE`：溢出文件摘要或 partial 账本摘要损坏。
- `E_VERIFY_FAILED`：精确通路和加速通路出现真实差异。

CLI 失败时向 stderr 输出稳定 JSON，并以非零状态码退出。

## 确定性聚合语义

每个批次先按行号升序生成局部聚合：

1. 过滤行。
2. 按分组键首次出现顺序建立局部组。
3. 对每个聚合按固定查询顺序、组内固定行序累计 `count`、`sum`、`sumsq`。
4. 批次之间严格按批次序号升序合并：`sum = sum_left + sum_right`，`sumsq` 同理。

因此结果只依赖输入、查询和配置，不依赖文件系统遍历顺序、进程调度、系统墙钟或 SQLite 事务时序。

输出按分组键稳定排序。数值键使用十进制规范化比较，字符串按 Unicode 顺序比较。`NaN` 和正负无穷数值保留在聚合中间值与输出中；分组键不允许 `NaN`/无穷，避免不可传递比较。

方差采用总体方差 `sumsq / n - mean^2`，仅将有限负零误差确定性钳制为 `0.0`。精确通路和加速通路使用完全相同的公式与位模式比较。

## 热点识别与加速通路

热点键在批次提交事务内更新，不进行全量预扫描。判定条件为：

```text
key_rows >= hot_key_min_rows AND key_rows / observed_rows >= skew_ratio
```

每次热点集合变化都记录版本、触发批次、加入/移除键、当时行数占比以及逐批观测。热点分流通道一旦启用则采用粘性集合，避免同一分组状态在普通/热点通道之间反复拆分。

加速模式 `hot_split_two_phase` 的实际路径：

1. 每批仍生成相同的局部 partial。
2. 热点键 partial 进入独立通道，普通键进入普通通道。
3. 通道切换只移动完整状态，不重新求和。
4. 最终按批次顺序两阶段合并通道。

`verify` 和运行结束报告会重放精确通路与加速通路，逐批次比较分组状态。任何差异会输出首个分歧分组键、触发批次、两侧 `count/sum/sumsq` 中间值，而不会套用浮点容差。

## 逻辑时钟与预算裁决

逻辑时钟只使用配置和批次行数，不读取系统墙钟：

```text
exact_batch_cost = logical_clock_step_ms + rows / 1000 * exact_cost_ms_per_1k_rows
accelerated_batch_cost = logical_clock_step_ms + rows / 1000 * accelerated_cost_ms_per_1k_rows
```

每个待提交批次开始前，基于全部已验证批次行数投影剩余精确时延。若精确总投影超出预算，则在明确触发批次切换到加速模式，并额外计入一次 `switch_overhead_ms`。模式事件记录投影值、切换前后时延、被否决模式和原因。相同逻辑时钟输入序列会得到相同的切换序号和逐位一致结果。

## 内存、溢出与回读

普通/热点通道的完整状态按规范化 JSON 字节数加分组开销估算。超过 `memory_limit_mib` 时：

1. 写入新的代际溢出文件 `state-<generation>.json`。
2. 使用规范化 payload 的 SHA-256 作为摘要。
3. 文件 fsync 后原子 rename。
4. 同一 SQLite 事务记录摘要、组数、字节数、触发批次并清空内存状态表。

下一次应用批次时重新读取溢出文件并校验摘要。损坏立即返回 `E_CORRUPT_STATE`，不会继续计算。报告累计记录溢出/回读次数、分组数和字节数。

## SQLite 状态与恢复协议

WAL + `synchronous=FULL` 的 SQLite 保存：

- 已提交批次序号、声明摘要、实际内容摘要和 partial 账本摘要。
- 批次 partial 与 `group_states`、分组状态版本账本。
- 查询水位、逻辑时钟水位、当前模式、热点状态与版本。
- 模式裁决、热点变化、溢出事件。
- 最终结果、报告、输出摘要与一致性检查。

处理顺序是：

```text
校验输入
  -> 批次 partial 原子暂存（prepared）
  -> 仅按水位 + 1 应用连续批次
  -> 分组状态/热点/时钟/模式/溢出元数据/批次 committed 同一事务提交
```

强制终止可能留下完整 `prepared` 批次，但不会留下已提交批次的部分分组状态。恢复时校验 prepared partial 摘要后继续应用；若检测到未提交批次的半提交版本账本，会先清理该账本再按序提交。计数和平方和不会因重放已提交批次而重复累加。

并发实例使用 SQLite `BEGIN IMMEDIATE` 串行化提交，并使用本地 `flock` 保护内存租约获取。租约按 PID 存活状态回收；同一查询活跃租约字节总和不得超过配置上限。批次号唯一约束、partial 唯一约束和分组版本账本共同保证同一批次同一分组只提交一次。

## 诊断输出

每个查询生成：

- `out/<query>.result.json`：稳定排序结果、实际模式、双通路一致性摘要。
- `out/<query>.report.json`：热点成因与观测、切换记录、时延、溢出、恢复、水位和状态摘要。

`status` 输出稳定排序 JSON，涵盖热点键、热点变化、执行模式、逻辑时延、溢出/回读、水位和资源租约。

## 实测结果

固定样本 `samples/tilted` 为 8 批 × 2000 行，共 16000 行、63 个最终分组。热点键为 `hot-A` 8800 行（55%）和 `hot-B` 2880 行（18%）。

```text
query=tilted-main
status=complete
mode=hot_split_two_phase (switch at batch 1)
projected exact=92.0ms, projected accelerated=25.8ms, budget=36.0ms
actual logical latency=25.8ms, wall_clock_used=false
hotspot changes: batch 2 -> [hot-A], batch 6 -> [hot-A, hot-B]
spills=8, spilled groups=504, spilled bytes=226834
readbacks=8, readback groups=504, readback bytes=226834
consistency=bitwise ok, groups_checked=63, values_checked=945
result_digest=e85702efafef63c53b4eebbbb6e39aadc73ca682febd2c7cea98231a01f36baa
result file sha256=3bd9e8549d6a6cea9b4695072e9b814535bf26484c4ec72f72af63e17b155591
```

窗口查询 `tilted-window` 同样完成预算切换，热点变化触发批次为 3 和 7，`verify` 通过。

中断与并发验证：

- `stage:3` 强杀后恢复：识别并续用 3 个 prepared 批次，结果文件 SHA-256 与不中断基线完全相同。
- `apply-before-commit:1` 在溢出后的状态提交前强杀：恢复后结果与基线完全相同，溢出统计保留。
- 两个进程分别暂存奇/偶批次：交错提交完成后 8 个 committed 批次、无重复分组版本，结果 SHA-256 与基线完全相同。
- 重复执行已完成查询：分组版本数保持 504，结果摘要不变。
- 半提交分组账本注入：恢复路径识别并清理 1 条半提交记录后正常完成。
- 内存租约占满时第二次获取返回 `E_MEMORY_BUDGET`。
- 强制溢出与提高内存上限后的无溢出运行：结果摘要和输出文件逐字节一致。

自动化测试：

```text
.venv/bin/python -m unittest discover -s tests -v
Ran 22 tests in 16.681s
OK
```

覆盖校验、NaN/无穷、热点版本、预算切换、双通路一致性、确定性重跑、溢出/无溢出一致性、半提交修复、内存租约、强制终止恢复、并发乱序提交和 `verify`。

## 已知边界

- 当前实现是单节点本地进程模型，不实现分布式共识。
- 窗口查询在最终化时按完整窗口重算；适用于任务给定的本地交互样本，不面向超大离线窗口。
- `fixed_scale` 被严格校验并纳入配置指纹，但当前聚合算术保留输入 IEEE-754 精度，不做隐式舍入；这避免“为了定点化”而改变真值。
- `float_tolerance` 同样不参与一致性判定；所有通路一致性均为位模式比较。
