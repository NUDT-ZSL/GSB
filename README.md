# 接警查询复用内核

这是一个只依赖 Python 3 标准库的本地查询复用内核。它从固定种子生成三个年代的接警接入 schema，将其归一为统一事件模型，解析并规范化逻辑计划，提取公共子表达式（CSE），按确定的整数权重决定是否物化，执行查询并记录复用账目，再用版本向量完成追加与查询编辑后的增量重算。

持久化只使用标准库 `sqlite3`。默认数据库是当前工作目录下的 `dispatch.db`；首次运行自动建库，后续运行直接复用。不需要网络、外部数据库服务或第三方 Python 包。

## 快速开始

```bash
# 初始化固定种子问题
python3 main.py --reset init

# 查看候选、裁决理由、查询结果和账目
python3 main.py candidates
python3 main.py results
python3 main.py ledger

# 应用全部追加/查询编辑批次并验证
python3 main.py run

# 查看版本向量与一致性报告
python3 main.py vectors
python3 main.py verify

# 按接警编号或毫秒时间范围回查复用账目
python3 main.py lookup --incident-id INC00001
python3 main.py lookup --start-ms 1704067200000 --end-ms 1704070800000
```

全局参数放在子命令之前：

```bash
python3 main.py --db ./local.db --seed 42 --reset init
python3 main.py --db ./local.db --seed 42 run
```

不写子命令时默认执行 `demo`：初始化（如库不存在）、推进全部待处理批次、输出逐查询结果、复用账目、结果摘要和一致性结论。

## Python API

```python
from dispatch_kernel import IncidentReuseKernel

with IncidentReuseKernel("dispatch.db", seed=42, reset=True) as kernel:
    print(kernel.status())
    print(kernel.candidate_report())
    print(kernel.query_results())
    print(kernel.ledger())
    print(kernel.version_vectors())

    kernel.apply_updates(max_batches=2)
    print(kernel.verify())
```

重启后不要传 `reset=True`，内核会从 SQLite 中的批次状态和版本向量继续：

```python
with IncidentReuseKernel("dispatch.db", seed=42) as kernel:
    kernel.resume_updates()
    print(kernel.verify()["consistent"])
```

## 确定性数据

`dispatch_kernel.generator.generate_problem(seed)` 生成：

- 72 条初始接警事件，三个 schema 版本各 24 条；
- 16 辆救援车辆与 8 个市政水源；
- 12 条组合查询；
- 4 个更新批次：一版追加、`Q03` 编辑、三版追加、`Q05` 编辑。

事件编号为 `INC00001` 到 `INC00078`。初始数据和后续批次均来自同一个 `random.Random(seed)` 序列，固定种子下逐字段一致。

## 三版 schema 归一

统一字段为 `incident_id`、`occurred_at_ms`、`event_type`、`severity`、`district`、`address_path`、`persons`、`station_id`、`vehicle_id`。内部 SQLite 使用 `persons_json` 保存规范化人员数组，公共报告输出字段名为 `persons`。

| 统一字段 | v1 来源 | v2 来源 | v3 来源 |
|---|---|---|---|
| `incident_id` | `event_no` | `call_id` | `incident.id` |
| `occurred_at_ms` | `reported_epoch_s`，秒乘 1000 | `timestamp_ms`，原值 | `incident.occurred_at.epoch_ms` |
| `event_type` | 数字 `alarm_type_code` 映射 | 中文 `incident_kind` 映射 | `incident.classification.type` |
| `severity` | 数字 `level_code` | 中文 `priority_text` 映射 | `incident.classification.level` 映射 |
| `district` | 管道分隔地址第 3 段 | `address_json.district` | `location.address.district` |
| `address_path` | `address_line` 管道拆分 | 扁平 JSON 对象展开 | 嵌套树展开 |
| `persons` | `person_names` 分号拆分 | `persons_json` 名称数组 | `involved.persons` 对象数组 |
| `station_id` | `station_code` | `station_id` | `responders.station.id` |
| `vehicle_id` | `vehicle_code` | `vehicle_id` | `responders.vehicle.id` |

每个统一字段的来源版本、来源路径和变换名称同时写入：

- `incidents.provenance_json`；
- `field_provenance(incident_id, field_name, source_version, source_path, transform)`。

`kernel.normalization_report(incident_id)` 和 CLI `normalize --incident-id ...` 可直接输出逐字段来源。

## 查询 DSL 与逻辑计划

每条查询先构造成 `Scan -> Filter -> Join -> Filter -> Aggregate -> Project` 形式的逻辑计划树。查询 DSL 的主要结构如下：

```json
{
  "query_id": "Q01",
  "event_filter": {
    "from_ms": 1704067200000,
    "to_ms": 1704076200000,
    "event_types": ["fire", "rescue"],
    "min_severity": 2,
    "source_versions": [1, 2, 3]
  },
  "joins": [
    {
      "table": "vehicles",
      "filter": {"status": ["available"], "min_capacity": 3}
    }
  ],
  "aggregate": {
    "group_by": ["e.station_id"],
    "metrics": [
      {"func": "count", "field": "*", "as": "incident_count"},
      {"func": "avg", "field": "e.severity", "as": "avg_severity"}
    ]
  },
  "project": [
    {"field": "e.station_id", "as": "station_id"},
    {"field": "incident_count", "as": "incident_count"}
  ]
}
```

语义约定：

- 时间窗是左闭右开：`occurred_at_ms >= from_ms` 且 `< to_ms`；
- 车辆按 `e.vehicle_id = v.vehicle_id` 连接；
- 水源按 `e.district = w.district` 连接；
- 所有过滤条件是 AND；
- 支持 `eq`、`ne`、`in`、`lt`、`le`、`gt`、`ge`、`contains`；
- 聚合支持 `count`、`sum`、`avg`、`min`、`max`。

规范化规则：

1. 相邻过滤节点合并，条件按规范化 JSON 字典序排序；
2. `in` 的值排序；
3. 内连接左右输入按规范化计划 JSON 排序，交换输入时同步交换连接键；
4. 连接表按表名排序；
5. 聚合分组字段排序，聚合指标按输出别名排序；
6. 投影列顺序保留，因为它是最终结果列顺序；
7. 计划指纹为规范化计划 JSON 的 SHA-256。

同一指纹的子树只有在至少两个不同查询中出现时才成为公共子表达式候选。候选输出包含 `refs`，每项形如：

```json
{"query_id": "Q01", "node_path": "$.project.child.aggregate.child"}
```

## 静态估计、权重与唯一裁决

所有估计均为整数。过滤选择率用万分比表示：

| 条件 | 选择率 |
|---|---:|
| `eq` | 1500 bp |
| `ne` | 8000 bp |
| `lt/le/gt/ge` | 4000 bp |
| `in` | `min(6000, 1500 * 值个数)` bp |
| `contains` | 3000 bp |

节点行数与代价：

- `Scan`: 行数等于表行数，代价等于表行数；
- `Filter`: 行数按条件逐个乘选择率并向上取整，代价为子代价加子行数；
- `Join`: 行数为 `ceil(left_rows * right_rows / max(distinct(left_key), distinct(right_key)))`，代价为左右代价之和再加 `left_rows * right_rows`；
- `Aggregate`: 分组基数取分组字段基数的乘积并截断到子行数，代价为子代价加 `2 * child_rows`；
- `Project`: 行数不变，代价为子代价加子行数。

候选权重：

```text
weight = (reference_count - 1) * cost - estimated_rows
```

其中 `reference_count` 是候选在全部查询计划中的引用位置数，`estimated_rows` 是静态结果行数，`cost` 是单次计算代价。

裁决规则是全局且确定的：

1. 仅当 `reference_count >= 2` 且 `weight >= 400` 时进入候选排序；
2. 排序键为 `(-weight, -cost, -reference_count, fingerprint)`；
3. 预算为 24 个物化候选，排名不超过预算者物化；
4. 权重相同先取单次代价更大的候选，再取引用更多的候选，最后用计划指纹字典序打破并列。

每条裁决保存完整整数算式和理由，例如：

```text
selected: weight=(4-1)*145-7=428 >= threshold=400; rank=3 <= 24
```

常量位于 `dispatch_kernel/config.py`，CLI `candidates` 输出候选、估计、权重、排名、是否物化及理由。

## 执行与复用账目

执行器按逻辑计划进行哈希连接、分组聚合和投影，并逐行携带接警事件血缘。查询结果和物化结果先按列值规范化排序，再序列化为 UTF-8 紧凑 JSON BLOB，因此同一结果可逐字节比较。

对每个候选，账目记录：

- `reference_count`: 查询计划中的引用位置数；
- `reuse_count`: 物化后本可避免的重复执行次数，等于 `reference_count - 1`；
- `materialized_rows`: 实际物化行数；
- `cost`: 未复用时的单次静态代价；
- `saved_cost`: `reuse_count * cost`；
- `net_saved_cost`: `saved_cost - materialized_rows`；
- `actual_cache_hits`: 本次执行期间真实读取物化缓存次数；
- `decision_reason`: 裁决依据。

被拒绝物化的候选保留在账目中，`reuse_count`、`saved_cost` 和物化行数均为 0。

`ledger_incident_index` 将从物化结果血缘得到的接警编号和时间写入 SQLite，因此可按接警编号或时间范围回查为其提供复用贡献的候选。

## 版本向量与增量重算

每个物化候选维护：

```json
{
  "data": {
    "event.v1": 2,
    "event.v2": 1,
    "event.v3": 1,
    "aux": 1
  },
  "queries": {
    "Q01": 1,
    "Q02": 1,
    "Q09": 1
  }
}
```

- `data` 只包含候选实际依赖的事件块和辅表版本。查询中的 `source_versions` 过滤会收窄事件块依赖；未限定版本时依赖 `v1/v2/v3`。
- `queries` 只包含引用该候选的查询及其查询定义版本。
- 查询结果自身保存依赖数据版本和所属查询版本。

批次处理完全在一个 SQLite 事务中完成：追加原始事件、归一、递增数据版本、解析新查询、重新裁决候选、仅重算版本向量变化的物化项、重组受影响查询、更新账目、写入一致性报告、标记批次 `applied`，最后提交。

未受影响的物化行不会被 `UPDATE`，其 BLOB 在更新前后逐字节相同。查询编辑只递增该查询版本，并只影响引用它的候选；追加只递增对应事件块版本，依赖其他事件块的候选保持原样。

## 中断恢复

`update_batches` 只有 `pending` 和 `applied` 两种可见状态。若进程在事务中途退出，SQLite 回滚整批，重启后该批仍为 `pending`；若批次已提交但进程在返回结果前退出，该批为 `applied`，重启后从下一批继续。

可显式模拟“提交后中断”：

```bash
python3 main.py --reset init
python3 main.py apply --interrupt-after 2
# 退出码为 75；前两批已经提交
python3 main.py updates
python3 main.py apply
```

## 一致性验证

`verify` 对每个查询做两件事：

1. 读取增量维护后的持久化结果；
2. 使用完全禁用物化缓存的单查询执行器从当前统一事件模型全量重算。

随后逐查询比较规范化结果 BLOB 的 SHA-256。报告包含：

- 每条查询的增量/参考摘要、行数和引用到的 CSE；
- 是否全部一致；
- 若有差异，给出缺失/多余行数、样例和涉及候选。

报告写入 `consistency_reports`，`update_seq=0` 表示初始装载，之后每个已提交批次对应一个更新序号。

## SQLite 结构

主要表：

- `raw_v1`、`raw_v2`、`raw_v3`: 三版原始接入记录；
- `incidents`: 统一事件模型；
- `field_provenance`: 逐字段来源版本、路径和变换；
- `vehicles`、`water_sources`: 连接维表；
- `queries`: 查询 spec、版本、规范化计划及指纹；
- `candidates`: CSE、引用位置、估计、权重、排名、裁决理由；
- `materialized`: 物化 BLOB、行数、血缘、版本向量；
- `query_results`: 增量查询结果及依赖版本；
- `ledger`、`ledger_incident_index`: 复用账目与按事件回查索引；
- `update_batches`: 批次状态机；
- `consistency_reports`: 一致性报告历史。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖固定种子、三版归一、字段来源、逻辑计划稳定性、CSE 引用与整数权重、初始结果全量比对、查询编辑后未受影响 BLOB 保持不变、全部批次增量一致性、批边界重启恢复、事务失败回滚以及 CLI 冒烟测试。
