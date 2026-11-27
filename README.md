# 供水管网水质监控 SQLite 谓词下推内核

这是一个仅使用 Python 标准库实现的本地执行内核。它将确定性生成的水质数据、查询请求、分区下推结果、空值判定解释、集中式基准、一致性报告、超标/复核清单和可续算快照统一持久化到 SQLite。

## 快速开始

环境要求：Python 3.10+，无需安装第三方包。

```bash
# 生成 data/water_quality.db，并运行全量、增量、快照、续算演示
python3 main.py demo

# 也可以分步执行
python3 -m water_quality init --db data/water_quality.db --seed 20261128
python3 -m water_quality run --db data/water_quality.db --run-id full-001
python3 -m water_quality snapshot --db data/water_quality.db \
  --run-id full-001 --snapshot-id snap-001
python3 -m water_quality verify-generator --db data/water_quality.db
python3 -m water_quality verify-snapshot --db data/water_quality.db \
  --snapshot-id snap-001
```

`demo` 会重置目标数据库，并在演示结束后恢复原始查询请求；历史运行和快照保持不变。

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

## 业务语义

### 三值逻辑

阈值比较统一采用 Kleene 三值逻辑：

- `NULL < 0.2`、`NULL > 1.0` 等比较结果为 `UNKNOWN`。
- `WHERE` 只保留 `TRUE`，`FALSE` 和 `UNKNOWN` 都不命中。
- `UNKNOWN` 不会被改写成 `FALSE` 后继续参与连接或阈值判断，也绝不会被当作 0。
- 生成的 SQL 对每个阈值比较显式加入 `IS NOT NULL` 保护，例如：

```sql
(r.residual_chlorine IS NOT NULL AND r.residual_chlorine < 0.2)
```

- `run_null_verdicts` / `snapshot_null_verdicts` 按监测点分区记录谓词、空值字段和 `UNKNOWN` 判定。

### 连接空值规则

连接键固定为 `monitoring_points.pipe_id = pipes.pipe_id`，`NULL = NULL` 永远不产生匹配。

| 连接类型 | 空连接键行为 | 结果标记 |
| --- | --- | --- |
| `inner` | 任一侧键为 `NULL` 时不产生行 | — |
| `left` | 保留监测点侧；无匹配管段时保留 | `point_key_null` |
| `right` | 保留管段侧；无匹配监测点/记录时保留 | `pipe_key_null` |
| `full` | 两侧未匹配行都保留，空键两侧各自成为独立未匹配行 | `point_key_null` / `pipe_key_null` |
| 任意外连接 | 非空键但找不到对应行 | `missing_pipe_reference` / `missing_point_reference` |

正常匹配行的标记为 `none`。连接执行计划同时写入 `query_plans.plan_json` 和 `query_plan_partitions` 的 `join_task` 行。

### 稳定顺序

集中式结果和分区下推结果统一按以下键排序后逐位置核对：

```text
(point_id, sampled_at, pipe_id, reading_id, pipe_row_id, join_null_source)
```

一致性报告比较命中集合、完整行内容、连接空值标记和记录顺序。不一致时记录首个分歧的分区、查询谓词或连接谓词。

### 业务清单

安全阈值为：

- 余氯 `< 0.2`：超标。
- 浊度 `> 1.0`：超标。
- pH `< 6.5` 或 `pH > 8.5`：超标。

等于阈值不算超标。指标为 `NULL` 时不进入超标清单，而进入人工复核清单。

人工复核区间按同一监测点的实际采样序列聚合连续缺失样本：

- `interval_start`：连续缺失区间的第一个采样时刻。
- `interval_end`：连续缺失区间的最后一个采样时刻。
- `missing_count`：区间内缺失样本数。

## 数据生成

默认生成参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `seed` | `20261128` | 固定随机种子 |
| `pipe_count` | `20` | 真实管段数，另加 1 条空键边界管段 |
| `point_count` | `32` | 监测点数 |
| `time_steps` | `18` | 每点采样时刻数 |
| `null_rate` | `0.12` | 单指标随机缺失概率 |
| `null_burst_rate` | `0.025` | 连续缺失突发概率 |
| `null_burst_max` | `4` | 最大连续缺失长度 |

生成器还会构造：

- 阈值等值样本：`0.2`、`1.0`、`6.5`、`8.5`。
- 刚越界样本：`0.199`、`1.001`、`6.499`、`8.501`。
- 管段侧空连接键和监测点侧空连接键。
- 指标缺失、连续缺失、未配置指标等情况。

相同种子和相同参数生成的 `data_fingerprint`、`truth_signature` 和 `truth_query_digest` 完全一致。`verify-generator` 会在临时数据库重放生成过程并比对这三个摘要。

生成真值表：

- `truth_violations`：超标清单真值。
- `truth_manual_reviews`：缺失时间区间真值。
- `truth_query_results`：逐查询集中式真值行和行签名。

## 查询请求

请求保存在 `query_requests.request_json`。示例：

```json
{
  "query_id": "Q001",
  "description": "北区余氯低值事件",
  "join_type": "inner",
  "pipe_predicates": [
    {"field": "region", "operator": "IN", "value": ["north"]}
  ],
  "point_predicates": [],
  "reading_predicate_groups": [
    [
      {"field": "residual_chlorine", "operator": "<", "value": 0.35}
    ]
  ]
}
```

`reading_predicate_groups` 是 DNF：组之间 `OR`，组内条件 `AND`。pH 区间外查询使用两个组。

支持的运算符：`<`、`<=`、`>`、`>=`、`=`、`!=`、`IN`、`NOT IN`、`IS NULL`、`IS NOT NULL`。

支持的分区策略：

- 管段：`region` 或 `hash`。
- 监测点：`location_zone` 或 `hash`。

规则版本、规则 JSON 和每个实体归属保存在 `partition_rules`、`partition_assignments`，运行开始时会复制到 `run_partition_assignments`。

## 执行过程

1. `planner.py` 为每个管段分区和监测点分区生成下推 SQL，同时建立连接任务清单。
2. 管段侧只执行管段分区谓词；监测点侧只执行监测点/监测记录分区谓词。
3. 每次过滤都会执行 SQL，并用相同的 Kleene 逻辑在 Python 中对候选行求值，二者不一致会抛出 `ExecutionInvariantError`。
4. 分区连接器显式处理空键规则，并按连接任务保存结果。
5. `reference.py` 使用一个全量 SQLite CTE 查询作为集中式基准。
6. 合并分区结果后与集中式基准比较，生成 `consistency_reports`。
7. 从原始监测记录生成超标清单和人工复核清单。

关键输出表：

| 表 | 内容 |
| --- | --- |
| `query_plans` | 查询表达式、连接规则、剩余处理步骤、完整计划 JSON |
| `query_plan_partitions` | `pipe_filter` / `point_filter` / `join_task` 的 SQL 与结果数 |
| `run_pipe_hits` | 管段分区命中行 |
| `run_point_hits` | 监测点分区命中行 |
| `run_null_verdicts` | 空值比较判定解释，固定为 `UNKNOWN` |
| `run_join_results` | 各连接任务的分区连接结果 |
| `run_query_results` | 最终有序查询结果 |
| `consistency_reports` | 逐查询一致性与分歧定位 |
| `run_violations` | 超标清单 |
| `run_manual_reviews` | 人工复核清单 |

## 增量重算

`incremental` 以上一运行作为基线：

- 谓词变更：扫描旧/新谓词在实际行上的命中状态、空值解释变化，得到受影响监测点/管段分区。
- 管段谓词变更：额外定位引用这些管段的监测点分区，以重算连接外侧状态。
- 分区规则变更：逐实体比较旧运行归属与新归属，只重算旧/新分区。
- 连接类型变更或原始数据变化：强制重算连接任务，但仍尽量复用未变的过滤结果。
- 未受影响的分区命中、空值解释和连接任务直接从快照/上一运行复制。
- 增量运行结束后，额外执行一次全量下推核验，并写入 `run_incremental_checks`。

示例谓词变更文件 `changes.json`：

```json
{
  "Q001": {
    "query_id": "Q001",
    "description": "北区余氯低值事件（阈值调整）",
    "join_type": "inner",
    "pipe_predicates": [
      {"field": "region", "operator": "IN", "value": ["north"]}
    ],
    "reading_predicate_groups": [
      [{"field": "residual_chlorine", "operator": "<", "value": 0.5}]
    ]
  }
}
```

```bash
python3 -m water_quality incremental --db data/water_quality.db \
  --previous-run-id full-001 --query-change-file changes.json
```

分区规则变更文件：

```json
{
  "version": "partition-v2",
  "pipe_strategy": "hash",
  "point_strategy": "hash",
  "pipe_hash_buckets": 2,
  "point_hash_buckets": 2
}
```

```bash
python3 -m water_quality incremental --db data/water_quality.db \
  --previous-run-id full-001 --new-rules-file rules-v2.json
```

## 快照与续算

快照表包括：

- `snapshot_query_plans`
- `snapshot_plan_partitions`
- `snapshot_partition_assignments`
- `snapshot_pipe_hits`
- `snapshot_point_hits`
- `snapshot_null_verdicts`
- `snapshot_join_results`
- `snapshot_query_results`
- `snapshot_consistency`
- `snapshot_violations`
- `snapshot_manual_reviews`

```bash
python3 -m water_quality resume --db data/water_quality.db \
  --snapshot-id snap-001 --query-change-file changes.json
```

续算前会比较快照的分区规则版本/规则 JSON 与当前活动规则。若不一致，命令返回退出码 `3`，不会创建运行，并输出：

```text
拒绝续算：快照分区规则版本 ... 与当前活动版本 ... 不一致。
```

`verify-snapshot` 会把快照恢复到临时运行，重新计算逻辑载荷摘要，再清理临时运行。

## 项目结构

```text
water_quality/
  schema.py       SQLite 模式
  db.py           连接、事务、指纹
  generator.py    确定性数据、查询与真值生成
  predicates.py   三值谓词、SQL/Kleene 求值
  rules.py        分区规则与归属
  planner.py      分区谓词与连接计划
  joins.py        NULL 感知的内/外连接
  reference.py    集中式 SQLite 基准
  engine.py       全量/增量执行、一致性、清单
  snapshots.py    快照、恢复、版本拒绝
  audit.py        超标与缺失区间清单
  cli.py          命令行入口
tests/
  test_predicates.py
  test_generator.py
  test_engine.py
  test_manual_boundary.py
  test_incremental_snapshot.py
main.py
```

## 退出码

- `0`：成功且一致性/真值核对通过。
- `2`：执行完成但存在不一致或真值不匹配。
- `3`：快照分区规则版本不一致，拒绝续算。

