# 技术说明

## 1. 目标与边界

组件解决的是“统计数据在流式执行中失真后，何时重优化、切换到哪个形态、切换是否值得”的问题。它不枚举编译期固定连接顺序，而是在每个已提交批次末尾，以实测前缀结果重新评估代价模型。

执行模型为：

- 表是列式批次的累积前缀。
- 查询是等值内连接图。
- 一个执行形态由连接顺序和每一步的连接算法组成。
- 观测点位于每个批次提交边界。
- 重优化只在批次完整提交后生效，不会中断进行中的批次。

## 2. 解析与校验

校验顺序为 `sample -> tables -> plan -> statistics -> reopt`。所有错误在创建 SQLite 文件前抛出。

主要不变量：

- 表名唯一。
- `row_count > 0`，每列 `1 <= ndv <= row_count`。
- `width_bytes` 为正整数。
- 连接谓词两端表和列存在，列类型相同，且两列均声明为连接键。
- 初始连接顺序包含每个表一次，且每个新表与已连接集合之间至少存在一个谓词。
- 批次到达号唯一且为正整数；每批行数为正整数。
- 每张表的批次行数之和等于表描述行数。
- 统计表集合与描述一致；统计 NDV 合法。
- 列选择和连接选择率均在开区间 `(0, 1)`。
- 配置阈值、冷却、时钟步进、预算、容差和误差预算非负或为正。

错误码：

| 错误码 | 场景 |
| --- | --- |
| `E_BAD_SAMPLE` | JSON、顶层结构或样本 ID 非法 |
| `E_BAD_TABLE` | 表/列/NDV/列宽/倾斜声明非法 |
| `E_BAD_PLAN` | 连接谓词、初始形态、批次顺序或行数非法 |
| `E_BAD_STATS` | 统计快照缺失、NDV 非法、选择率越界 |
| `E_BAD_CONFIG` | 重优化配置或 CLI 参数非法 |
| `E_OUT_OF_ORDER` | 尝试提交非下一个批次 |
| `E_DUPLICATE_BATCH` | 同到达号已以不同摘要提交 |
| `E_SWITCH_BUDGET` | 漂移触发且存在更优候选，但所有候选切换代价超预算 |
| `E_NO_BETTER_SHAPE` | 漂移触发但当前形态已是观测代价下最优 |
| `E_STATE_MISMATCH` | 数据库模式版本或计划指纹不一致 |
| `E_CORRUPT_STATE` | 水位、批次摘要或切换前后行摘要不一致 |
| `E_VERIFY_FAILED` | 重放或逐行真值校验失败 |

## 3. 确定性批次生成

### 3.1 种子

对每个表：

```text
seed_table = SHA256("stream-reopt-v1|global_seed|table_name")[:16]
```

每列和倾斜位置使用独立的 `SHA256(seed_table|purpose)` 派生种子，再构造 `PCG64`。这样新增/删除某一列不会影响其他列的随机流。

### 3.2 行切片

批次行偏移由计划中同一表此前批次的 `row_count` 前缀决定。表数据先生成完整列数组，再按 `[offset, offset + batch_rows)` 切片。因此批次摘要只取决于样本描述、全局种子和到达顺序。

### 3.3 倾斜

若表声明倾斜列：

```text
hot_rows = round(table_rows * hot_fraction)
```

使用无放回选择确定热点行位置，其余行从 NDV 值域排除热点键后均匀取值。因此整表热点比例精确等于声明值；主样本 `orders.customer_id` 中键 `7` 精确出现 315/900 行。

### 3.4 摘要

列摘要包含：

- 规范化魔数、dtype、维度和形状；
- 数值列按 C 顺序字节编码；
- 字符串/object 列逐元素写入 UTF-8 长度和字节，避免 object 指针进入摘要。

批次摘要再写入表名、到达号、列名、列 dtype 和列摘要。数据库只持久化批次摘要和行数；恢复时重新生成列并在参与观测前校验摘要。

## 4. 连接执行

### 4.1 形态

```text
Shape(join_order, algorithms)
```

`algorithms[i-1]` 表示把 `join_order[i]` 加入已连接集合时使用的算法。

每个加入步骤收集新表与已连接表之间的全部谓词。若存在多个谓词，算法先以第一个谓词建立候选，再用全部谓词过滤。

### 4.2 算法

- Hash：右侧输入建立键到行列表索引，左输入逐行探测。
- Nested loop：左右输入笛卡尔遍历并检查谓词。
- Sort merge：按第一个谓词键稳定排序，合并同键分组后检查全部谓词。

最终结果按 `(列名, 类型标签, 值)` 规范化排序。因此不同连接顺序或算法产生相同多重集时，行序列和摘要一致。重复行保留。

### 4.3 观测

在第 `n` 批提交边界：

```text
input_rows       = sum(committed rows of each table)
output_rows      = len(canonical join result of committed prefix)
observed_sel     = output_rows / product(committed rows of each table)
```

连接谓词 `T1.c1 = T2.c2` 的实测连接选择率为：

```text
pair_count = sum_k count_T1(c1=k) * count_T2(c2=k)
pair_sel   = pair_count / (rows_T1 * rows_T2)
```

观测只读取当前前缀的完整数据。数据库持久化水位之前的批次摘要，恢复时先校验摘要，再重建前缀。没有未来批次回填路径。

## 5. 漂移判定

每个参与连接键产生一个漂移来源：

```text
ndv_drift         = |observed_ndv - initial_ndv| / max(initial_ndv, 1)
join_sel_drift    = |observed_pair_sel - initial_pair_sel| / max(initial_pair_sel, 1)
skew_drift        = |observed_max_frequency - initial_skew_fraction| / max(initial_skew_fraction, epsilon)
drift_ratio       = max(ndv_drift, join_sel_drift, skew_drift)
```

`drift_ratio > drift_threshold` 的来源成为触发候选。每个来源记录：

- 表和连接键；
- 谓词两端；
- 偏差比和分量；
- 实测/估计 NDV、连接选择率、热点频率；
- 该表已提交批次区间；
- 本次偏差计算的误差链。

### 5.1 冷却

逻辑时钟：

```text
clock_ms = committed_batch_count * clock_step_ms
```

只有在以下条件满足时触发：

```text
clock_ms - last_reopt_clock_ms >= cooldown_ms
```

否则记录 `cooldown_suppressed` 事件。所有超阈值来源都更新累计器中的首次/末次批次、观测次数、最新偏差比和最大偏差比，但不调用切换。

冷却使用逻辑时钟而非墙钟，因此相同输入下事件序号和输出完全确定。

## 6. 代价模型与重优化

### 6.1 候选枚举

枚举所有满足前缀连通性的表排列，并对每个加入步骤枚举配置中的算法。每个候选形态使用当前已提交批次的实测表行数和实测边选择率估值。

单步输出估计：

```text
out = left_rows * right_rows * product(edge_selectivities)
```

单步代价：

```text
hash        = 0.8*left + 1.2*right + 0.4*out + 2
sort_merge  = 0.45*LlogL + 0.45*RlogR + 0.9*(left+right) + 0.5*out + 1
nested_loop = left*right + 0.25*out + 1
```

总代价使用 `math.fsum` 求和。每个候选保留逐步误差链。

### 6.2 切换代价

```text
switch_cost = 1.0
            + 0.002 * committed_input_rows
            + 0.25 * reorder_distance
            + 0.5  * algorithm_changes
```

只有 `switch_cost <= switch_cost_budget + float_tolerance` 的候选可进入可行集。

裁决：

1. 可行集中存在比当前形态估计代价更低的候选：切换最低代价候选。
2. 存在更优候选但全部超预算：保留原形态，结果事件和 CLI 返回 `E_SWITCH_BUDGET`。
3. 当前形态已是观测代价下最优：保留原形态，记录 `E_NO_BETTER_SHAPE`。

### 6.3 精度敏感

候选按 `(estimated_cost, canonical_shape)` 排序。当：

```text
|top_1_cost - top_2_cost| <= float_tolerance
```

且最终切换选择依赖该排序时，记录：

- `precision_sensitive=true`；
- 两个候选的估计代价和误差上界；
- 参与表、观测批次区间；
- 计算步骤链；
- 漂移触发来源。

主样本第 3 批的实际成本差为 `0.0`，因此呈现真正的候选并列场景。

## 7. 浮点误差上界

### 7.1 求和

对 `n` 项浮点输入，使用：

```text
result = fsum(values)
gamma  = n*eps / (1 - n*eps)
bound  = gamma * sum(|values|) + ulp(result)
```

### 7.2 乘积

逐因子传播：

```text
new_bound = |old_result|*ulp(factor)
          + |factor|*old_bound
          + ulp(new_result)
```

### 7.3 除法

对 `result = x / y`：

```text
bound = bound_x/|y|
      + |x|*bound_y/|y|^2
      + ulp(result)
```

每个观测、每个漂移偏差比、每个候选总代价均保留误差来源链。观测链还记录 `error_bound_budget` 与是否越界。

## 8. SQLite 持久化

核心表：

| 表 | 内容 |
| --- | --- |
| `meta` | 模式版本、样本/查询 ID、计划指纹、总批次数 |
| `committed_batches` | 到达号、序号、表、行数、批次摘要、列摘要 |
| `runtime_state` | 单行形态指针、水位、逻辑时钟、漂移累计、精度标记、结果摘要 |
| `observations` | 每个已提交批次的输入/输出、选择率、漂移来源、误差链 |
| `drift_events` | 触发/冷却拒绝、来源、被否决候选和时间 |
| `reopt_events` | 切换前后版本/形态、代价、预算、精度标记和错误码 |
| `shape_history` | 每个形态版本的创建批次和原因 |

数据库启用：

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=30000;
```

## 9. 原子提交与并发

写事务流程：

```text
acquire flock(db.lock)
  BEGIN IMMEDIATE
    reload and verify committed batch prefix
    build current batch
    execute join and observation
    update drift accumulator
    optionally decide and switch shape
    INSERT batch / observation / drift / reopt / shape_history
    UPDATE runtime_state pointer
    [optional SIGKILL hook]
  COMMIT
release flock
```

`committed_batches.arrival` 是主键，`ordinal` 唯一。两个执行实例同时提交时，文件锁和 `BEGIN IMMEDIATE` 串行化写者；后到实例重新读取状态，只能推进下一个未提交批次。

重复提交已存在到达号：

- 重新生成摘要并比较；
- 相同则返回 `committed=false, idempotent=true`；
- 不同则返回 `E_DUPLICATE_BATCH`。

读取方使用读事务，WAL 保证只看到完整提交快照。

## 10. 强杀恢复

`--crash-before-commit-arrival N` 在目标批次全部写入事务、更新状态指针之后、`COMMIT` 之前调用 `SIGKILL`。该事务没有 COMMIT，SQLite 在连接终止后回滚。

恢复流程：

1. 读取 `runtime_state.committed_count` 和对应批次记录。
2. 按计划顺序重新生成每个已提交批次。
3. 比较摘要和行数。
4. 重建已提交列数据与前缀连接结果。
5. 从第一个未提交批次继续。
6. 不重放已提交观测，不修改已提交形态。

主实验在提交第 5 批前强杀：恢复前水位 4、版本 2；`resume` 只推进第 5–9 批，最终状态与无崩溃运行完全一致。

## 11. 验证口径

`verify` 执行三层检查：

1. 每批数据库摘要与重新生成摘要一致。
2. 每个观测前缀按该观测记录的形态版本重放，比较输入行数、输出行数和结果摘要。
3. 用全部已提交数据分别按初始形态和当前最终形态做一次性全量连接，规范化排序后逐行比较，并比较持久化结果摘要和行数。

主样本验证结果：

```text
row_count = 33,498
truth_digest = current_digest
row_by_row_match = true
first_mismatch_index = null
batch_digest_checks = 9
observation_replays = 9
```

## 12. 可复现性说明

- Python `hash()` 不参与种子或摘要。
- 随机数使用固定种子 PCG64。
- 字符串列摘要不使用 object 指针或 pickle。
- 冷却采用逻辑时钟。
- 事件列表、候选列表和 JSON 键均稳定排序。
- SQLite 事务保证同一状态快照。
- `status` 输出不含墙钟时间。

因此，相同样本、相同起始状态和相同提交顺序下，批次摘要、观测序列、漂移候选、形态版本、误差链和状态 JSON 均可逐位复现。
