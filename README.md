# 多工序车间质量事件二级索引求解器

本项目在本地离线复演“质量事件写入内存表 → 冻结刷写为不可变段 → 分层晋升 → 失效比例/容量合并 → 删除旧段并回收空间”的全过程。仅使用 Python 3 标准库，不依赖第三方包、数据库或外部服务。

求解器按逻辑时刻推进，输出：

- 每条索引条目的内存表编号、逻辑偏移、所在段与段内偏移；
- 每次查询的合并命中顺序、段来源、失效过滤数量与扫描量；
- 每个 `(工序, 班次)` 分区的逐层段数、条目数和精确字节账目；
- 晋升、合并、旧段删除、回收前后占用与净回收字节；
- 与独立归并参考实现的逐查询核对结果及首个不一致键依据。

## 快速运行

```bash
cd /home/liang/feedback/uid42a8691e-default-471

# 一次完成生成、求解、全量核对和人工边界样本
python3 -m qindex selfcheck

# 或分步执行
python3 -m qindex generate --seed 20261128 --out artifacts/workload.json
python3 -m qindex solve artifacts/workload.json --out artifacts/report.json
python3 -m qindex reference artifacts/workload.json --out artifacts/reference.json
python3 -m qindex boundaries --out artifacts/boundaries.json

# 标准库单元测试
python3 -m unittest discover -s tests -v
```

`python3 main.py ...` 与 `python3 -m qindex ...` 等价。生成的 `workload.json`、`report.json`、`reference.json`、`boundaries.json` 默认位于 `artifacts/`。

## 工程结构

```text
qindex/
  model.py       复合键、条目、段、精确字节序列化与配置
  generator.py   固定种子事件流、查询规格与容量参数生成器
  loader.py      输入格式校验
  engine.py      内存表、不可变段、分层晋升、失效、合并、查询
  reference.py   不依赖段引擎的扁平归并查询参考实现
  solver.py      时序编排、逐查询/逐段核对、账目汇总
  boundaries.py  人工边界样本执行与断言
  cli.py         命令行入口
samples/         5 个人工构造的判定边界输入
tests/           unittest 测试
```

## 固定语义

### 分区与复合键

分区固定为 `(process, shift)`，每个分区拥有自己的活动内存表和层清单。索引键为：

```text
(process, shift, batch_prefix, logical_time, event_id)
```

比较顺序依次为工序、班次、批次前缀字典序、逻辑时刻升序，最后以 `event_id` 作为唯一性稳定决胜项。字符串采用 Python/Unicode 字典序，例如中文班次 `中班` 会排在 `早班` 前；业务分组关系不受影响。

查询按 `process + shift` 定位分区，再用 `batch_prefix` 做前缀匹配。跨段结果通过有序归并输出，因此同一批次前缀内保持逻辑时刻升序。

### 同刻原子刷写

每个逻辑时刻的写入先完整进入各分区内存表，然后才检查刷写阈值。达到 `flush_threshold` 的分区整体冻结并排序刷写，内存表随后清空。若同一时刻的突发写入超过阈值，本批仍进入同一个段，不会把同刻事件拆到刷写后的新段中。条目记录到达时的 `memtable_id` 与 `logical_offset`；内存表内部保持键升序，但不改写这两个定位字段。

### 段大小与占用

段逻辑字节大小定义为以下对象 UTF-8 规范 JSON 的长度：

```json
{"partition": ["工序", "班次"], "entries": ["全部条目记录"]}
```

条目记录包含键、业务事件标识、工位、内存表编号、逻辑偏移、测量项与结果。段 `id` 和所属层不计入该负载大小，因此整段晋升不会改变字节账目。分区视图中：

```text
segment_bytes == sum(level.bytes) == sum(segment.size_bytes)
```

### 分层晋升

每个非顶层按 `max_segments` 与 `max_bytes` 双重上限维护。任一项超限时，从最老段开始选择最少的候选整段晋升到下一层；晋升不重写条目、不改变字节大小。顶层超限进入容量合并。

### 合并优先级

维护优先级在 `IndexEngine.POLICY` 中固定，并在动作 `conditions` 中记录：

1. 非顶层段数或字节超限，优先整段晋升；
2. 段内失效条目比例 `invalid_count / entry_count >= merge_invalid_ratio` 时执行失效合并；
3. 分区字节超过 `partition_max_bytes` 或顶层容量超限时执行容量合并。

失效比例条件同时满足容量条件时，动作触发值为 `invalid_ratio+capacity`，优先决策固定为 `invalid_ratio_before_capacity`。失效合并的候选集为该触发层的全部段；容量合并优先选择超限层或字节最多的层，必要时跨层归并到顶层。合并只保留有效条目，合并前后有效事件标识及顺序保持一致。

### 失效与诊断

失效规格可给 `station`、`batch_prefix`，或同时给出两者（同时给出时按 AND 匹配当前已写入条目）。归档批次使用前缀匹配。动作报告包括：

- 匹配条目数、新增失效条目数及事件标识；
- 涉及段与内存表；
- 各受影响分区仍存续的首末键与条目数；
- 首个仍会命中该失效谓词的活动条目；正确执行时为 `null`。

### 查询

查询归并内存表与分区内所有层的段，过滤失效事件后按复合键升序返回。每个命中记录包含：

- `source.kind = segment|memtable`；
- `segment_id + segment_offset` 或 `memtable_id + logical_offset`；
- `live: true`；
- 完整键与业务事件标识。

被过滤的失效候选放在单独的 `filtered_invalid` 中，不计入命中。查询同时输出 `candidate_entries_scanned`、`invalid_filtered_entries` 与 `scan_amplification`，便于观察旧失效条目对扫描放大的影响。

### 空间回收账目

合并动作按“写新段 → 删除旧段 → 更新占用”记录，原始旧段字节与重写新段字节均保留。替换后的净账目为：

```text
partition_bytes_before = partition_bytes_after + reclaimed_bytes
deleted_segment_bytes  = rewritten_segment_bytes + reclaimed_bytes
```

其中 `reclaimed_bytes` 是替换净回收量，`deleted_segment_bytes` 是被删旧段原始字节。动作内含两个方程的逐项布尔校验；被删段会立即从存活段表和各层视图中移除。

## 输入格式

最小输入示例：

```json
{
  "format": "quality-index-workload/v1",
  "seed": 20261128,
  "config": {
    "flush_threshold": 4,
    "merge_invalid_ratio": 0.5,
    "partition_max_bytes": 4200,
    "levels": [
      {"name": "L0", "max_segments": 1, "max_bytes": 4000},
      {"name": "L1", "max_segments": 2, "max_bytes": 8000},
      {"name": "L2", "max_segments": 3, "max_bytes": 16000}
    ]
  },
  "timeline": [
    {
      "time": 1,
      "writes": [
        {
          "event_id": "EVT-000001",
          "process": "冲压",
          "shift": "早班",
          "batch_prefix": "LOT-CY-001",
          "logical_time": 1,
          "station": "ST-CY-01",
          "metric": "轮廓尺寸",
          "value": 1.25,
          "result": "合格"
        }
      ],
      "invalidations": [],
      "queries": [
        {
          "query_id": "QRY-000001",
          "process": "冲压",
          "shift": "早班",
          "batch_prefix": "LOT-CY-"
        }
      ]
    }
  ]
}
```

每个时刻的处理顺序固定为：写入整批追加并判定刷写/晋升 → 依次执行失效与维护 → 执行查询 → 输出分区视图。

## 独立参考与验收

`qindex/reference.py` 不导入段引擎，仅保存扁平写入记录与失效标记，每次查询重新过滤并按独立构造的键排序。求解器在每个时刻核对失效集合与存活条目，在每个查询核对完整 `(event_id, key)` 序列。`report.json` 的 `verification` 给出：

- 首个查询不一致的时刻、查询条件、双方事件与键；
- 首个段或内存表不一致的段号、偏移、双方键及原因；
- 首个失效集合不一致；
- 全部查询、段、失效标记与账目是否通过。

人工边界样本：

| 样本 | 验证点 |
| --- | --- |
| `samples/01_exact_flush.json` | 内存表恰好 3 条达到阈值，同刻条目只进入一个段 |
| `samples/02_invalid_ratio_exact.json` | 失效比例恰好 `0.5`，按 `>=` 触发合并并清理 2 条 |
| `samples/03_archived_query.json` | 比例 `1.0` 不触发合并，旧段仍存活但归档查询必须返回 0 条 |
| `samples/04_capacity_only.json` | 无失效时仅由容量阈值触发合并，净回收大于 0 |
| `samples/05_ratio_and_capacity.json` | 比例与容量同时触发，验证固定优先级与合并账目 |

默认种子 `20261128` 的结果为 24 个时刻、105 条事件、31 次查询；当前维护序列包含 24 次刷写、14 次晋升和 7 次失效合并，净回收 5192 字节。所有查询、段级状态、失效标记和占用账目均与独立参考一致。
