# 技术说明

## 1. 模块边界

| 模块 | 职责 |
|---|---|
| `parser.py` | 词法分析、受限 SELECT 语法、逻辑谓词 AST、规范化文本 |
| `planner.py` | 表/列解析、稳定逻辑计划、拓扑序、真实计数 SQL |
| `stats.py` | 确定性采样、列基数/直方图/空值/高频值统计、选择性估计 |
| `engine.py` | SQLite 元数据、事务、版本、真实计数、热力、快照、异常注入 |
| `anomalies.py` | 异常调度、业务码断言、稳定结构化日志 |
| `cli.py` | 本地命令行接口 |
| `gui.py` | Tkinter 界面 |
| `experiment.py` | 建表、灌入、分析、口径变更、快照恢复、异常注入、报告 |

所有持久状态位于一个 SQLite 文件。用户表与内部 `_lab_*` 表同库；样本 SQL 禁止访问保留表。

## 2. SELECT 语法

支持范围：

```text
SELECT [DISTINCT] *|column[, ...]
FROM table [AS alias]
[JOIN table [AS alias] ON condition]
[WHERE condition]

condition := condition OR condition
           | condition AND condition
           | (condition)
           | column (=|!=|<>|<|<=|>|>=) (literal|column)
           | column IN (literal[, ...])
```

仅支持单次两表 INNER JOIN。子查询、聚合、函数、GROUP/HAVING/UNION、LEFT/RIGHT/CROSS JOIN 等返回 `E_PARSE`。

AST 规范化规则：

- 标识符转小写，空白折叠为单一空格；
- AND/OR 结合链扁平化，子条件按规范化文本排序；
- IN 去重并排序；
- 括号只保留表达语义所必需的分组；
- 规范化 SQL 由 AST 重新生成。

## 3. 逻辑计划

计划固定采用叶到根拓扑结构：

```text
SCAN orders AS o
SCAN customers AS c
JOIN o.customer_id = c.id
FILTER ...
```

节点 ID 为 `op_000`, `op_001`, ...。每个节点含 `children`，根节点为 `root`。`topological_order`、节点数组和树行均为稳定序列化。`plan_id` 是计划核心 JSON 的 SHA-256 摘要前缀。

真实计数对应关系：

- `scan`：`SELECT COUNT(*) FROM table`
- `join`：`SELECT COUNT(*) FROM ... JOIN ... ON ...`，不含 WHERE
- `filter`：完整 `JOIN ... WHERE ...` 的计数

因此每个算子都有独立真实行数，而非只比较最终结果。

## 4. 数据版本与灌入回滚

灌入步骤：

1. 解析 `@primary-key`、`@skew` 注释标记；
2. 逐条校验允许的 `CREATE TABLE/INDEX`、`INSERT/REPLACE`、`DROP`、`DELETE`、`UPDATE`；
3. `BEGIN IMMEDIATE`；
4. 删除当前用户表；
5. 逐条执行样本 SQL；
6. 读取表结构、主键、行数据；
7. 按表名和行内容排序，对类型标记值（int/float/text/blob/null 等）做 SHA-256；
8. 写入 `dv_*`、表元数据、会话初始状态；
9. 提交。

第 4 步之后任一步失败均执行 `ROLLBACK`，因此旧数据、旧 `current_data_version` 和旧会话状态一起恢复。重复灌入相同内容产生相同 `dv_*` 和内容摘要。

## 5. 统计快照

口径参数：

```json
{
  "histogram_buckets": 8,
  "sample_rate": 1.0,
  "null_fraction_overrides": {"orders.status": 0.12}
}
```

每列表统计包含：

- `ndv`：采样去重数按采样率放大后，受行数与非空比例约束；
- `null_fraction`：样本空值率或显式覆盖；
- `histogram.boundaries` 与 `histogram.counts`：数值等宽桶或文本顺序桶；
- `most_common`：最多 12 个高频值及 `share_ppm`；
- `sample_non_null_count`。

采样先把行按类型标记 JSON 排序，再按内容 SHA-256 排序选择固定数量行，因此与 SQLite 物理行序无关。统计快照 `sv_*` 由 `data_version + parameters + tables` 的内容摘要派生。

## 6. 行数估计

- 扫描：统计快照中的表 `row_count`；
- 连接：`left_est * right_est * join_selectivity`；
- 过滤：`input_est * where_selectivity`。

选择性规则：

- 等值：优先使用高频值样本占比；未知值在剩余 NDV 上均匀分摊；再乘非空比例；
- IN：各等值概率去重求和并截断；
- 范围：数值桶内线性插值计算 CDF；文本桶在桶内按 0.5 近似；
- 不等：非空比例减去等值概率；
- 列=列：`(nonnull_l * nonnull_r) / max(ndv_l, ndv_r)`；
- AND：独立概率乘积；
- OR：`1 - Π(1-p_i)`。

结果采用 decimal 半入舍入到整数。同一统计版本和数据版本重复估计得到逐字节相同的 JSON。

## 7. 偏差分数与热力

对算子 `e`（估计）和 `a`（真实）：

- `actual > 0`：`ratio = round(e/a, 6)`；`score = min(10000, round_half_up(|e-a|*10000/a))`；
- `actual = 0`：`ratio = "ZERO_ACTUAL"`、`ratio_ppm = null`；若 `e=0`，score 为 0，否则 score 为 10000。

热力档位：`low` (<100)、`medium` (<500)、`high` (<2000)、`critical`。归因链按叶到根拓扑序输出，最高分算子标记为 primary。

刷新热力时执行 `DELETE FROM _lab_heatmap`，再插入当前分析的全部算子。旧统计版本仍在 `_lab_stats`，但不会残留在热力表中。

## 8. 版本比较

`compare_stats_versions` 对同一规范化查询：

1. 校验两个统计版本都存在；
2. 校验二者与当前数据版本一致；
3. 分别估计每个算子；
4. 对同一真实行数计算旧/新偏差分数；
5. 仅输出估计或分数发生变化的算子，顺序遵循计划拓扑序。

## 9. 快照与恢复

快照载荷包含：

- 完整会话：数据版本、统计版本、计划历史、最近归因结果；
- 当前表结构、主键和标记元数据；
- 载荷摘要。

恢复步骤：

1. 读取快照并校验 Payload 摘要；
2. 校验快照数据版本等于当前 `current_data_version`；
3. 校验表结构元数据一致；
4. 校验当前统计版本与历史中的每个统计版本存在、未损坏且数据版本一致；
5. 对每条历史计划重建计划、比对 `plan_id`、重新估计并重算真实行数，要求每个算子偏差分数相同；
6. `BEGIN IMMEDIATE`；
7. 删除当前会话行，插入快照会话；删除热力表，重建快照热力；
8. 提交。

任何校验失败均不修改当前会话。统计版本缺失从 `E_STATS` 转换为 `E_SNAPSHOT`，以区分快照一致性问题。

## 10. 可控异常

| 异常 | 注入方式 | 事务行为 | 业务码 |
|---|---|---|---|
| 倾斜放大 | 把首个 `@skew` 列全部置为当前主导值并检查占比 | 抛错前 `ROLLBACK` | `E_SKEW` |
| 统计损坏 | 事务内删除统计 JSON 的 `tables`，触发校验 | 抛错前 `ROLLBACK` | `E_STATS` |
| 计划重放不一致 | 给内存计划注入错误 `plan_id` 后重放 | 不改持久状态 | `E_REPLAY` |

注入只改变数据副本、统计副本或调用参数，不修改源码。日志载荷由 `anomaly/business_code/message/details` 构成，不含时间戳和随机数；日志 ID 为载荷 SHA-256 前缀，使用 `INSERT OR IGNORE` 去重，因此重复注入得到相同 ID。`list_logs` 按 `(event, event_id, event_key)` 稳定排序。

## 11. 测试策略

`tests/test_lab.py` 覆盖：

- 空白/大小写/括号规范化与相同 `plan_id`；
- 子查询/聚合拒绝；
- 等值、范围、IN、AND/OR；
- 重复灌入相同版本与失败整体回滚；
- 估计确定性与 `E_STATS`；
- 真实行数、偏差归因、零值哨兵；
- 热力整替、旧统计保留、版本对比；
- 数据变更后的 `E_VERSION`；
- 快照重开恢复与统计缺失 `E_SNAPSHOT`；
- 三类异常业务码、重复性与稳定日志。
