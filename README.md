# stream-reopt

本地、确定性、可恢复的流式连接管线运行时自适应重优化组件。项目从零实现了一个可嵌入的领域库与 `run` / `resume` / `status` / `verify` 命令行工具，不访问网络，也不依赖外部数据源。

## 能力概览

- 严格校验表描述、连接谓词、流式批次和初始统计；错误输入返回稳定业务错误码，且在解析成功前不创建 SQLite/块目录状态。
- 按全局种子、批次和列名派生子种子，生成可逐位复现的列式数组；支持声明式热点键比例及随时间变化的倾斜比例。
- 以批次为单位执行 `hash`、`sort_merge`、`nested_loop` 三种内连接算法，并输出每阶段实测输入行数、输出行数与选择率。
- 只使用已提交批次窗口计算漂移偏差比；超过阈值后执行冷却检查、候选代价比较、切换预算裁决和可解释的拒绝记录。
- 每个观测点与每次偏差比都记录定点换算、舍入方向、量化误差上界和浮点累计上界；代价接近时使用 50 位 Decimal 通路复核。
- SQLite 事务原子提交批次水位、批次摘要、观测、漂移事件、决策和当前形态指针；候选形态先进入非权威 staging，崩溃后自动识别并清理。
- 使用跨进程文件锁和 SQLite 唯一约束保证同一批次同一形态只提交一次；重复提交相同摘要变为幂等操作。
- `verify` 在隔离目录中单进程串行重放，逐批比较输入/输出/执行摘要、形态版本、决策与块清单，并输出漂移证据。

## 快速开始

要求 Python 3.11+、`numpy`、`pytest`，以及 Python 自带的 `sqlite3`。

```bash
python3.11 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[test]"

python -m stream_reopt run --fresh --pretty
python -m stream_reopt status --pretty
python -m stream_reopt verify --pretty
```

默认使用相对路径：

- SQLite：`runtime/state.db`
- 块/分片：`runtime/blocks/`
- 锁文件：`runtime/state.db.lock`
- 串行真值：`runtime/verify_truth/`

所有路径均可通过 `--db`、`--blocks`、`--lock` 覆盖。默认值不写死机器相关绝对路径。

## 主样本结果

`samples/adaptive_join_sample.json` 包含三张表、12 个逻辑批次和随时间从 2% 升到 92% 的热点键比例。初始统计按均匀分布估计。

实际运行结果：

- 12 个批次全部提交，水位从 `-1` 推进到 `11`。
- `b004` 实测漂移比 `0.778443`，超过阈值 `0.25`，在批次边界从版本 1
  `["lineitem","orders","customers"]` 切换到版本 2
  `["customers","orders","lineitem"]`，两种连接算法均为 `hash`。
- 漂移触发点为 `b004`、`b007`、`b010`；冷却窗口内抑制了
  `b005`、`b006`、`b008`、`b009`、`b011`，原因均为 `COOLDOWN_ACTIVE`。
- 最终漂移累计约 `41.131304`，最大偏差比约 `12.445783`，`reopt_count=1`。
- 数据库中有 12 个批次、24 个连接阶段观测、2 个形态版本、3 个决策事件。
- `verify` 输出 `consistent=true`，比较 12 个批次、0 个不一致项。

常用验证命令：

```bash
# 查看切换裁决、被否决候选和漂移证据
python -m stream_reopt run --fresh --pretty

# 在 b004 已 staged 新形态但未提交时强制退出
python -m stream_reopt run --fresh \
  --crash-at-batch b004 --crash-point after-staging

# 半提交形态会保留在 status 中，resume 清理后只重放 b004-b011
python -m stream_reopt status --pretty
python -m stream_reopt resume --pretty
python -m stream_reopt verify --pretty
```

可用的故障点：

- `during-join`：当前批次第一个连接阶段完成后硬退出。
- `after-generation`：生成列式输入后硬退出。
- `after-execute`：完整批次执行后、任何持久化提交前硬退出。
- `after-staging`：候选形态写入 staging 后、批次事务提交前硬退出。
- `before-commit`：批次事务提交前硬退出。
- `after-commit`：批次事务提交后硬退出。

## CLI

```text
python -m stream_reopt run     [--fresh] [--max-batches N] [--crash-at-batch ID --crash-point P]
python -m stream_reopt resume  [--max-batches N] [--crash-at-batch ID --crash-point P]
python -m stream_reopt status
python -m stream_reopt verify  [--truth-db PATH --truth-blocks PATH] [--keep-truth]
```

公共参数：

```text
--sample PATH      场景 JSON，默认 samples/adaptive_join_sample.json
--db PATH          SQLite 文件，默认 runtime/state.db
--blocks PATH      本地块目录，默认 runtime/blocks
--lock PATH        文件锁，默认 <db>.lock
--lock-timeout S   跨进程锁超时，默认 30 秒
--pretty           JSON 缩进输出
--allow-decision-errors
                   裁决返回 E_SWITCH_BUDGET 时仍以 0 退出
```

退出码：

- `0`：命令完成。
- `2`：输入或业务错误，JSON 中给出 `error.code`。
- `3`：流水线已完成，但至少一次切换裁决返回 `E_SWITCH_BUDGET`；原形态被保留。
- `4`：`verify` 发现一致性差异。
- `137`：测试用强制终止点触发。

## JSON 场景约定

顶层字段：

```json
{
  "tables": [],
  "plan": {},
  "initial_stats": {},
  "config": {}
}
```

### `tables`

每张表必须包含唯一 `name`、正整数 `rows` 和非空 `columns`。每列：

- `name`：表内唯一。
- `type`：支持整数、浮点和字符串类型族。
- `ndv`：`1 <= ndv <= table.rows`。
- `width`：正整数，表示用于代价估算的列宽字节。
- `key_semantics`：如 `primary_key`、`foreign_key`、`join_key`、`attribute`、`measure`。
- `skew`：可选；`ratio` 为热点样本比例，`hot_fraction` 为热点值域比例，`start_ratio` / `end_ratio` 让倾斜比例随批次进度变化，`hot_values` 可显式指定热点键。

### `plan`

- `plan_id`：运行标识，同一数据库内绑定一份确定的计划内容。
- `seed`：全局非负整数种子。
- `predicates`：连接谓词；两端表、列必须存在，类型必须可比。
- `initial_join_order`：必须覆盖所有表且逐步连通。
- `join_algorithms`：长度为 `表数 - 1`，支持 `hash`、`sort_merge`、`nested_loop`。
- `batches` 或 `batch_order + batch_rows`：批次 ID、到达序号和每表行数。到达序号必须唯一、从 0 连续递增，每表每批行数必须为正整数。

### `initial_stats`

`join_selectivities` 支持对象或数组，必须覆盖所有谓词，选择率严格位于 `(0, 1)`。`estimated_cardinality` 与 `table_cardinalities` 可选；表基数若给出必须等于表描述行数。

### `config`

- `drift_threshold`：偏差比触发阈值，必须大于 0。
- `cooldown_ms`：两次重优化裁决之间的逻辑冷却时间。
- `switch_cost_budget`：非负切换代价预算；负数为 `E_SWITCH_BUDGET_INVALID`。
- `fixed_scale`：定点标度，正整数。
- `float_tolerance`：精度敏感标记与净收益比较容差。
- `logical_clock_step_ms`：每个到达序号对应的逻辑毫秒步进。
- `observation_window`：漂移窗口包含的已提交批次数，默认 3。
- `switch_base_cost`：切换固定代价，默认 5。
- `max_candidates`：候选上限，默认 128。
- `enable_reopt`：默认 `true`。

## 领域库集成

```python
from stream_reopt import RuntimeEngine, load_scenario, verify_against_serial

scenario = load_scenario("samples/adaptive_join_sample.json")
engine = RuntimeEngine(
    scenario,
    db_path="runtime/state.db",
    blocks_path="runtime/blocks",
)
try:
    result = engine.run(resume=False)
    print(result.final_watermark, result.final_shape)
finally:
    engine.close()

verification = verify_against_serial(
    scenario,
    db_path="runtime/state.db",
    blocks_path="runtime/blocks",
    truth_db_path="runtime/verify_truth/state.db",
    truth_blocks_path="runtime/verify_truth/blocks",
)
assert verification.consistent
```

直接使用底层操作：

- `parse_scenario(dict)` / `load_scenario(path)`：纯校验，不写状态。
- `generate_batch(scenario, batch)`：生成确定性列式批次。
- `execute_batch(scenario, batch, table_batches, shape)`：执行指定形态。
- `assess_drift(...)` / `choose_shape(...)`：独立漂移与切换裁决。
- `StateStore`：事务提交、形态版本、staging、恢复、状态查询。
- `BlockStore`：本地 `.npz` 块与完整清单。

## 关键不变量

1. 相同计划内容、全局种子、批次与列名产生相同的列摘要和批次输入摘要。
2. 一个批次的观测只能来自该批次完整执行结果；漂移窗口只能引用 `arrival_seq` 更小的已提交批次。
3. 当前批次总是使用提交前读取的形态；形态指针只在该批次提交事务内整体替换。
4. `state.current_version` / `current_shape_digest` 与 `shapes` 表中的不可变内容摘要必须一致。
5. 批次表、观测表、事件表、决策表和状态水位在同一 SQLite 事务中推进。
6. staging 表不是权威状态；恢复时先记录再删除所有半提交候选。
7. 相同 `(run_id, batch_id)` 只能存在一个提交摘要；相同摘要重复提交返回 duplicate，不同摘要返回 `E_BATCH_DIGEST_MISMATCH`。

## 漂移、代价与精度

阶段选择率定义为：

```text
actual_selectivity = output_rows / (left_rows * right_rows)
deviation_ratio = abs(actual_selectivity - initial_selectivity) / initial_selectivity
```

漂移使用最近 `observation_window` 个已提交阶段观测的加权聚合。触发记录包含：

- 表与连接键；
- 实际选择率、初始估计、偏差比；
- 观测批次 ID 区间与到达序号区间；
- 阈值、阈值余量和触发依据；
- 定点换算、舍入方向、量化误差上界、浮点累计上界和合计上界。

候选枚举覆盖所有连通连接顺序与算法组合。切换代价：

```text
switch_cost = switch_base_cost + 0.02 * current_stage_estimated_rows
```

净收益必须覆盖切换代价，且 `switch_cost <= switch_cost_budget`。否则保留原形态并记录 `NO_NET_BENEFIT`、`OVER_BUDGET` 或 `NOT_BETTER` 等拒绝原因；存在可获益候选但全部超预算时返回 `E_SWITCH_BUDGET`。

最佳与次佳候选总代价差小于 `float_tolerance` 时标记 `precision_sensitive=true`，并用 `Decimal` 50 位精度重建代价、复核胜者是否稳定，输出参与表、观测区间、换算标度和舍入方向。

## 持久化与恢复

SQLite 使用 WAL、`synchronous=FULL`、外键和 `BEGIN IMMEDIATE`。批次执行和提交均处于跨平台文件锁内，因此多个实例可以竞争推进，但同一时刻只有一个执行器进入批次临界区。

持久化表：

- `runs`：计划内容摘要、种子、表/统计/配置快照。
- `state`：批次水位、当前形态指针、冷却时间、漂移累计和切换次数。
- `shapes`：不可变形态版本与 `FULL_REPLACE` / `INCREMENTAL_APPEND` 动作。
- `staging_shapes`：非权威候选形态，仅用于两阶段切换与半提交识别。
- `batches`：输入摘要、输出摘要、执行摘要、提交摘要和块路径。
- `observations`：每批每连接阶段的实测记录与误差账本。
- `drift_events`：漂移评估、触发和冷却抑制事件。
- `decisions`：完整重优化裁决、被否决候选和错误码。

形态更新分类：

- 当前摘要相同：`NOOP`，不产生新版本。
- `StateStore.append_shadow_shape()`：记录不激活的 `INCREMENTAL_APPEND`，当前指针不变。
- 批次边界切换：新版本写入后以 `FULL_REPLACE` 整体替换当前指针。
- 版本小于等于当前版本且内容不同：`E_FORM_VERSION_REGRESSION`。

崩溃恢复顺序：

1. 获取文件锁。
2. 查询 `state.watermark_seq` 和当前形态。
3. 检查 `batches` 最大序号与水位一致、当前形态摘要一致。
4. 记录并清除 `staging_shapes` 中的半提交形态。
5. 从 `watermark_seq + 1` 开始重新生成并执行，只提交缺失批次。
6. 观测以 `(run_id, batch_id, join_id)` 为主键，不重复累计。

## 错误码

| 错误码 | 含义 |
| --- | --- |
| `E_BAD_PLAN` | 表/计划/批次结构非法 |
| `E_BAD_STATS` | 统计快照非法或与谓词不匹配 |
| `E_TYPE_MISMATCH` | 连接键类型不可比 |
| `E_BATCH_DIGEST_MISMATCH` | 同一批次摘要冲突 |
| `E_EMPTY_OBSERVATION` | 漂移观测样本数为零 |
| `E_SWITCH_BUDGET_INVALID` | 切换预算配置非法 |
| `E_SWITCH_BUDGET` | 有可获益候选但切换预算不足，保留原形态 |
| `E_FORM_VERSION_REGRESSION` | 试图写入或激活旧形态版本 |
| `E_WATERMARK_MISMATCH` | 批次不能连续推进水位 |
| `E_STATE_CORRUPT` | 持久化状态自洽检查失败 |
| `E_CONCURRENCY_CONFLICT` | 文件锁等待超时或并发冲突 |
| `E_NOT_FOUND` | 缺少样本、运行或真值数据库 |
| `E_VERIFY_MISMATCH` | 验证一致性失败 |

## 测试与验收

```bash
.venv/bin/python -m pytest -q
```

本次实际验证环境：

- Python `3.11.18`
- NumPy `2.4.6`
- pytest `9.2.0`
- SQLite `3.53.1`

测试覆盖 27 项：

- 非法计划/统计/类型/预算及“不写状态”；
- 生成、连接摘要逐位复现；
- 三种算法结果多重集一致；
- 漂移触发、窗口、冷却抑制与误差账本；
- 正式切换、预算拒绝、精度敏感 Decimal 复核；
- staging 后与连接中途强制终止恢复；
- 三进程并发推进、批次和观测唯一性；
- 版本回退拒绝、重复提交幂等、摘要冲突；
- 单进程串行 `verify` 对账；
- 篡改输出摘要后 `verify` 返回 `E_VERIFY_MISMATCH`；
- `--fresh` 拒绝删除工作区等危险路径。

最终结果：`27 passed`。

额外交叉检查：Python `3.10.12` + NumPy `1.26.4` 与 Python `3.11.18` + NumPy `2.4.6` 分别运行主样本，12 个批次的输入摘要、输出摘要、提交摘要、最终形态和漂移触发数量完全一致。

## 项目结构

```text
stream_reopt/
  models.py       严格 JSON 解析与不可变领域模型
  data.py         确定性列式生成与摘要
  join.py         三种分批连接算法与阶段观测
  precision.py    定点误差账本与精度证据
  reopt.py        漂移、候选代价、预算与重优化裁决
  storage.py      SQLite 事务、形态版本、staging、文件锁
  blocks.py       本地块/分片与清单
  engine.py       运行时编排、崩溃恢复、并发临界区
  verify.py       串行真值重放与对账
  cli.py          命令行入口
samples/
  adaptive_join_sample.json
examples/
  embed_runtime.py
tests/
runtime/          运行后生成，已加入 .gitignore
```
