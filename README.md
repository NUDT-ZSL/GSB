# 港口作业 B+Tree 索引实验内核

本项目只使用 Python 3 标准库，在本地生成确定性港口靠泊样本，构建可观测
B+Tree，逐批保存索引快照，并用全表扫描结果核对范围索引查询。它同时验证
增量插入、快照恢复和全量重建三条路径的查询结果与页结构。

## 快速运行

```bash
python3 run.py
```

等价于：

```bash
python3 run.py \
  --seed 20260701 \
  --batches 4 \
  --page-capacity 12 \
  --fill-factor 0.5 \
  --queries 10 \
  --output-dir output
```

运行结果写入 `output/`。进程退出码为 `0` 表示验收通过，`1` 表示验收失败，
`2` 表示参数或恢复错误。

测试：

```bash
python3 -m unittest discover -v
```

## 中断与恢复

先只写到第 2 批：

```bash
python3 run.py --seed 99 --batches 4 --page-capacity 8 \
  --output-dir output_demo --stop-after-batch 2
```

用相同样本与页参数，从每个索引最近一批快照继续：

```bash
python3 run.py --seed 99 --batches 4 --page-capacity 8 \
  --output-dir output_demo --resume
```

`--resume` 会校验快照种子、页容量、分裂填充因子、已完成批次、记录前缀和状态
指纹；参数不一致时拒绝恢复。恢复后继续写入的树会与连续全量写入路径逐页比较。

## 样本设计

`port_index_lab/generator.py` 按 `seed` 生成 1-based 批次与写入序号：

- 第一批记录数恰好等于叶页容量，分裂计数仍为 0，形成容量边界快照。
- 首批包含成组同靠泊时刻记录、零长占用区间，以及连续重复泊位编号；
  默认容量下该组为 4 条。
- 后续批次进入更晚的船期窗口，模拟船期不断追加。
- 每条记录包含 `write_seq`、`batch_id`、`ship_id`、`berth_id`、
  `berth_time`、`depart_time`、`tonnage`、`cargo_type`。

时刻使用可字典序比较的 ISO 字符串 `YYYY-MM-DDTHH:MM:SS`。

## B+Tree 语义

`port_index_lab/bptree.py` 中：

- `capacity` 是每页最大键数；内部页键数是分隔键数，子页数始终为键数加一。
- 插入在叶页内二分定位，保持键序；三个索引均追加唯一的 `write_seq`
  作为最终 tie-breaker。
- 页满后按 `fill_factor` 计算左侧键数。叶页把右页首键复制上移，内部页把
  实际分隔键上移，父页满时递归分裂；根页满时新建更高层根页。
- 每个分裂事件记录旧页号、新页号、父页号、层级、上移键、分裂前占用率、
  左右页分裂后键数与占用率，以及是否创建新根。
- 叶页维护 `prev_leaf`/`next_leaf` 双向链。结构校验会报告容量越界、排序
  错误、父指针错误、不可达叶页、重复叶链接、前向/后向链接不一致、叶链断裂
  和指向不存在页的具体页号。

每批结束都写一个完整快照，包含页表、键、记录号、记录内容、层数、各层页数、
每页占用、分裂历史、叶链状态和 SHA-256 状态指纹。

## 查询与基准

生成两类查询：

1. `berth_overlap`：指定泊位，查询区间 `[start, end]` 与占用区间
   `[berth_time, depart_time]` 闭区间相交。
2. `berth_time_closed`：全局靠泊时刻落在 `[start, end]` 内，包含端点。

索引扫描从根页逐层比较键定位起始叶页，再沿 `next_leaf` 扫描到上界或末叶，
输出起始页、终止页、逐页访问顺序、命中记录、命中键序和访问页数。叶链损坏会
在扫描停止原因和结构校验中同时体现。

全表基准完全绕过 B+Tree：先按写入顺序扫描所有记录并执行谓词，再按当前候选
索引的稳定键序排序。这样既能核对命中多重集合和重复项，也能逐位置核对顺序。
不一致比较会列出查询号、缺失记录、多余记录、重复项、首个分歧位置及索引访问
页号。

## 三个候选索引

| 索引 | 键 | 泊位重叠查询访问 | 全局靠泊时刻闭区间 |
|---|---|---|---|
| `berth_time_composite` | `(berth_id, berth_time, write_seq)` | 用泊位前缀裁剪，再过滤离泊时刻 | 前导列不是时刻，需全叶链扫描 |
| `berth_time_depart` | `(berth_time, depart_time, write_seq)` | 从最左叶扫到查询结束时刻，再过滤泊位 | 直接按靠泊时刻定位 |
| `berth_time_only` | `(berth_time, write_seq)` | 从最左叶扫到查询结束时刻，再过滤泊位和离泊 | 直接按靠泊时刻定位 |

默认参数本次运行的核心指标：

| 索引 | 分裂次数 | 总页数 | 深度 | 叶平均填充率 | 泊位重叠平均访问页 | 时刻闭区间平均访问页 |
|---|---:|---:|---:|---:|---:|---:|
| `berth_time_composite` | 20 | 23 | 3 | 0.650000 | 1.666667 | 20.0 |
| `berth_time_depart` | 17 | 20 | 3 | 0.764706 | 8.166667 | 1.0 |
| `berth_time_only` | 17 | 20 | 3 | 0.764706 | 8.166667 | 1.0 |

结论：

- 面向“指定泊位 + 占用区间重叠”的主查询，选择
  `(berth_id, berth_time, write_seq)`。它的前导列直接匹配泊位等值条件，
  默认样本下平均只访问约 1.67 个叶页，显著少于另外两个候选索引。
- `berth_time_depart` 的离泊时刻只在前导靠泊时刻相同或靠泊范围确定后有用，
  无法替代缺失的泊位前导列，因此不能解决泊位区间裁剪问题。
- 如果全局靠泊时刻闭区间查询占比高，应补充单一 `(berth_time, write_seq)`
  二级索引。它与 `(berth_time, depart_time, write_seq)` 扫描页数相同，但
  字段更少、存储和写入维护成本更低。
- 选择基于访问代价，不以“索引返回看起来更少”为依据。所有候选索引的查询
  结果都必须先通过全表基准和落点校验。

## JSON 产物

```text
output/
  snapshots/
    berth_time_composite/batch_0001.json ... batch_0004.json
    berth_time_depart/batch_0001.json ... batch_0004.json
    berth_time_only/batch_0001.json ... batch_0004.json
  reports/
    samples.json
    queries.json
    final_report.json
    acceptance_report.json
    partial_report.json       # 仅 --stop-after-batch 时
```

`final_report.json` 包含：

- 配置、边界样本证据、完整查询定义。
- 每个索引的指标、最终页表、分裂历史、逐查询访问路径、命中记录和基准比较。
- `index_comparison.choice`：按实测平均扫描页数生成的索引选择与取舍依据。
- `index_comparison.cross_index_set_consistency`：同一查询在三个候选索引上的
  命中多重集合比较；键序可因索引定义不同，但业务命中集合必须一致。
- `path_consistency`：增量对全量重建、快照恢复对连续写入的逐查询结果和逐页
  结构比较；不一致时包含首个查询分歧与首个差异页。
- `acceptance_checks`：叶链、全表基准、落点、容量边界和三条写入路径的验收项。

## 项目结构

```text
run.py                         命令行入口
port_index_lab/generator.py    确定性样本与边界形态
port_index_lab/bptree.py       B+Tree、分裂日志、叶链、扫描、快照
port_index_lab/indexes.py      三种索引定义与访问计划
port_index_lab/queries.py      查询生成与全表基准
port_index_lab/compare.py      结果顺序/多重集合比较与页差异定位
port_index_lab/runner.py       多索引、路径一致性、报告与验收
tests/test_port_index_lab.py   标准库单元与端到端测试
```

这是单进程、内存页模型配合 JSON 持久化的可观测实验内核，不模拟并发事务、
缓冲池淘汰或磁盘页 I/O 延迟。
