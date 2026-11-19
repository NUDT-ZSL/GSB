# Local Shuffle + Join Engine

一个不依赖数据库服务、容器或外部数据的本地查询执行引擎，用确定性生成样本复现
Shuffle、广播哈希连接、分片哈希连接、排序归并连接、SQLite 状态恢复与真值校验。

## 环境

- Python 3.10+
- NumPy 1.24+
- 标准库 `sqlite3`、`multiprocessing`、`hashlib`
- Linux 本地文件锁 `fcntl`

当前工作区已经建立 `.venv`。从零复现可使用：

```bash
uv venv --python /usr/bin/python3.10 .venv
uv pip install --python .venv/bin/python pip
.venv/bin/python -m pip install -r requirements.txt
```

也可在支持 `venv/pip` 的环境中使用：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

运行入口为：

```bash
.venv/bin/python -m join_engine <command> --sample samples/query_sample.json
```

## 快速开始

```bash
# 只观测与裁决，不写持久化状态
.venv/bin/python -m join_engine plan \
  --sample samples/query_sample.json --state-dir .runtime/demo --scale medium

# 执行并自动与嵌套循环真值比较
.venv/bin/python -m join_engine run \
  --sample samples/query_sample.json --state-dir .runtime/demo --scale medium

# 稳定排序 JSON 状态
.venv/bin/python -m join_engine status \
  --sample samples/query_sample.json --state-dir .runtime/demo --scale medium

# 校验分区文件、提交账本、结果指针与真值
.venv/bin/python -m join_engine verify \
  --sample samples/query_sample.json --state-dir .runtime/demo --scale medium

# 三档规模 x 三种算法的可比较基准
.venv/bin/python -m join_engine benchmark \
  --sample samples/query_sample.json --state-dir .runtime/benchmark \
  --output artifacts/benchmark_report.json
```

`run` 和 `resume` 支持并行分区工作进程：

```bash
.venv/bin/python -m join_engine run ... --partitions 0,2,4
.venv/bin/python -m join_engine run ... --partitions 1,3,5
```

同一查询、版本、分区由 SQLite 事务和 `fcntl` 文件锁串行化提交；重复提交相同摘要
是幂等的。所有工作分区完成后，任一副本会以事务方式整体替换当前结果指针。

## 样本格式

`samples/query_sample.json` 是自包含样本，包含：

- `seed`：全局确定性种子。
- `tables`：表名、行数、连接键、每列类型/NDV/宽度/空值率、可选倾斜键与比例。
- `query`：左右表、内/左外/右外连接、等值连接谓词、残差过滤选择率。
- `execution`：分区数、单分区内存上限、分块行数、倾斜阈值、容差、逻辑时钟步进、
  广播/哈希预算比例和三档显式规模采样点。

样本必须通过模型校验：表名唯一、行数为正整数、连接键存在、`1 <= NDV <= rows`、
列宽合法、选择率位于 `(0, 1)`、分区数与分块行数为正等。非法输入返回稳定业务错误码
（例如 `E_BAD_ROW_COUNT`、`E_BAD_NDV`、`E_BAD_SELECTIVITY`），且校验发生在状态目录
创建之前。

## 数据与联接语义

生成器使用 seed、表名、行号和列名派生的 BLAKE2b 值，不依赖进程随机状态。倾斜键先按
声明比例注入；空键由列级 `null_fraction` 确定。逐行规范 JSON 的行摘要和排序后的键摘要
用作后续观测与校验来源。

联接规则：

- 空键永远不参与等值匹配；外连接仍会保留对应侧空键行并补 `NULL`。
- 重复键产生全部过滤通过的笛卡尔组合。
- 过滤选择率是 `ON` 条件的一部分，在匹配对产生后、外连接补空之前应用。
- 每条结果包含 `left_row_id`、`right_row_id`、原始列值及
  `source_partitions.left/right`，可追溯到来源分区。
- 真值由左表行号为主序、右表行号为次序的全量嵌套循环产生；最终结果和真值均按固定
  规范顺序比较，首个差异同时给出双方来源分区。

## Shuffle 与分区文件

- 分区号由连接键规范编码的 BLAKE2b 值对分区数取模；同一键在左右两侧必落同一分区。
- 每行写为 `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=True)`
  加换行，因此相同输入重复 Shuffle 得到逐字节相同的分区文件。
- 每个分区记录行数、字节数、文件 SHA-256、键摘要和输入行摘要。
- 最终文件先写唯一临时文件并 `fsync`，再原子重命名；SQLite 只在文件完整落盘后提交。
- 读取方只从 SQLite 已提交记录发现分区，`.tmp-<pid>-part-...` 不会被当作可读分区。

## 算法裁决

裁决只使用确定性观测和注入的逻辑时钟，不使用墙钟：

1. 小侧总序列化字节 `<= memory_limit_mib * broadcast_fraction`：广播哈希连接。
2. 否则最大分区估算字节 `<= memory_limit_mib * hash_fraction`：分片哈希连接。
3. 否则排序归并连接，分区内按 `chunk_rows` 生成排序 run，再归并。

每个决策包含：

- 首个触发条件 `first_trigger`；
- 所有条件、观测值、阈值和表达式；
- 被否决算法及原因；
- 显式降级策略。

预算不足时不会静默超限：广播构建侧超预算会降级为分区局部哈希；分片哈希的单分区
构建侧超预算会降级为该分区的有界外部排序归并。实际算法、降级分区和原因写入指标和
结果分区提交记录。

## 持久化与恢复

SQLite 状态库保存：

- 查询当前版本和整轮结果指针；
- 每轮 `run/resume` 的 PID、状态和逻辑时钟水位；
- 数据观测摘要、算法决策；
- 左右 Shuffle 分区提交、结果分区提交；
- 结果账本及最终文件摘要。

版本指纹包含 seed、表描述、查询、执行配置与规模采样点。指纹变化时在同一事务中创建
新版本、清空当前结果指针并切换当前版本。最终提交会再次校验版本，旧版本进程不能覆盖
新指针。

强制终止测试可使用：

```bash
JOIN_ENGINE_CRASH_POINT=shuffle_left_after:2 ...
JOIN_ENGINE_CRASH_POINT=shuffle_left_during:100 ...
JOIN_ENGINE_CRASH_POINT=join_after:2 ...
```

`resume` 会：

- 将已死亡进程留下的 `running` 轮次标为 `interrupted`；
- 校验已提交分区摘要，跳过完整分区；
- 只重建缺失或半写入分区；
- 清理死亡进程的临时文件；
- 在全部结果分区提交后重做全局规范排序、真值校验和事务化结果指针提交。

## 指标口径

所有耗时均为 `logical_clock_step` 注入的逻辑 tick：

- `join_duration_logical_ticks`：联接算法本身；
- `duration_logical_ticks`：本轮包含真值验证的总逻辑 tick；
- `peak_partition_rows`：实际左右分区和联接构建侧的最大行数；
- `overflow_spill_bytes`：外部排序 run 落盘字节；
- `result_rows`：最终结果行数。

指标中明确标注观测来源为“确定性生成器全量扫描”、真值来源为“固定顺序嵌套循环”，
并标记 `wall_clock_used_for_decisions: false`。

## 测试与验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试覆盖非法输入零状态写入、生成/Shuffle 字节确定性、无键跨分区、三档算法选择、
内/左外/右外三种联接语义、两次强制中断恢复、并发同分区幂等提交、版本替换和稳定
`status` 输出。

实际验收结果见 [docs/VALIDATION.md](docs/VALIDATION.md)，机器可读摘要见
`artifacts/validation_summary.json`，完整基准见 `artifacts/benchmark_report.json`。

## 目录

```text
join_engine/
  models.py            解析、业务校验、版本指纹
  generator.py         确定性数据与观测统计
  partitioning.py      键到分区映射
  shuffle.py           分区落盘、原子提交、恢复辅助
  selection.py         三算法裁决与拒绝原因
  join_algorithms.py   哈希、广播、排序归并与外部排序
  truth.py             固定顺序嵌套循环真值
  storage.py           SQLite 事务状态与账本
  engine.py            plan/run/resume/verify/benchmark 编排
  cli.py               命令行接口
samples/query_sample.json
tests/test_engine.py
docs/VALIDATION.md
```

## 边界

这是面向确定性本地样本的可复现实验引擎，不实现分布式调度、网络传输、SQL 解析器或
任意复杂谓词。真值校验刻意使用全量嵌套循环，复杂度为 `O(left_rows * right_rows)`，
适合当前自包含样本而非超大数据集。

