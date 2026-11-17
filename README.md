# ColPipe 本地列式处理管线

ColPipe 是一个面向本地 Windows 环境、无容器、无外部服务依赖的列式数据处理演示管线。它从声明式 JSON 加载两张列式表与一条 SQL 子集查询，完成：

- 强校验与原子加载（`E_DATA` / `E_PARSE`）
- 逻辑计划编译、过滤下推、有序列裁剪
- 整数定点选择度估算与确定性任务切分
- 受全局并行度和内存预算约束的扫描/过滤线程池调度
- 优化路径与未优化基线的聚合结果逐字节比较
- 会话快照保存、版本差异识别、整体替换恢复
- 独立快照/计划/轨迹/结果验证工具
- Tkinter 计划树、下推/裁剪、切分、时间线和业务码界面

核心只依赖 Python 3.11 标准库。检测到本地 NumPy 时会用于数值列统计的 min/max 加速；未安装 NumPy 时自动使用同语义的纯 Python 路径。

## 快速开始（Windows）

在仓库根目录打开 `cmd.exe`：

```bat
py -3.11 --version
py -3.11 -c "import tkinter; print('tkinter', tkinter.TkVersion)"
py -3.11 -c "import numpy; print('numpy', numpy.__version__)"

run_demo.bat
run_tests.bat
run_gui.bat
```

也可以直接使用模块入口：

```bat
python demo.py
python -m colpipe demo --workspace .
python -m unittest discover -s tests -v
python -m colpipe gui --workspace .
```

Windows 官方 Python 安装包通常包含 Tkinter。若 `tkinter` 缺失，请修复本地 Python 安装；核心管线和测试不要求 Tk 或 NumPy。GUI 模块采用延迟导入，因此在没有 Tk 的服务器上仍可导入、编译和测试核心。

## 查询语言

支持的单条查询形态如下，必须恰好包含两个表输入和一个内连接：

```sql
SELECT c.region AS region,
       o.category AS category,
       SUM(o.amount) AS total_amount,
       COUNT(*) AS order_count,
       MAX(o.quantity) AS max_quantity
FROM orders AS o
INNER JOIN customers AS c ON o.customer_id = c.id
WHERE (o.amount >= 25.0 AND o.amount < 90.0)
  AND o.category IN ['books', 'games']
  AND c.active = true
GROUP BY c.region, o.category
ORDER BY region, category;
```

支持：

- `SELECT` 中的 GROUP BY 列和 `SUM` / `COUNT` / `AVG` / `MIN` / `MAX` 聚合
- `COUNT(*)` 与 `COUNT(column)`
- `=`、`!=`、`<`、`<=`、`>`、`>=`
- `IN` / `NOT IN`，OR 不支持
- `BETWEEN` / `NOT BETWEEN`
- `IS NULL` / `IS NOT NULL`
- 仅由 `AND` 连接的条件；括号可任意包裹 AND 子树
- 两表等值内连接，可包含多个等值键并由 AND 组合
- `ORDER BY` 输出别名或 GROUP BY 列，可选 `ASC` / `DESC`

明确拒绝并以 `E_PARSE` 报错：OR、子查询、第三个关系输入、非等值连接、`DISTINCT` 聚合、未知函数等。语义引用错误（表、列、聚合参数、连接键类型）以 `E_DATA` 报错，并带出错字段路径。

空白差异和多余 AND 括号会被归一化。由于 WHERE 条件在逻辑上按 AND 排序，相同语义查询会得到相同的查询摘要、优化计划摘要和切分方案。

## 输入 JSON

演示脚本会生成 `artifacts/demo/pipeline_input.json`。结构如下（示例省略大部分样本值）：

```json
{
  "format": "colpipe.input.v1",
  "session_id": "colpipe-sales-demo",
  "data_version": "sales-2026-11-17.v1",
  "tables": [
    {
      "name": "orders",
      "schema_version": "orders-schema-v1",
      "row_count": 4096,
      "columns": [
        {
          "name": "amount",
          "type": "float64",
          "values": [12.5, null, 48.0],
          "null_bitmap": [false, true, false]
        }
      ]
    }
  ],
  "query": "SELECT ...",
  "settings": {
    "parallelism": 4,
    "memory_budget_bytes": 256000,
    "min_rows_per_task": 128
  }
}
```

约束：

- 表名全局唯一，列名匹配 `[A-Za-z_][A-Za-z0-9_]*`
- 类型只允许 `int64`、`float64`、`bool`、`string`
- 每列 `values` 和 `null_bitmap` 的长度必须都等于 `row_count`
- 空值位图为 `true` 的位置必须是 JSON `null`，反之亦然
- `float64` 必须有限；`int64` 必须在有符号 64 位范围内；布尔值不能冒充整数
- 加载是纯函数式整体校验；任何错误都会丢弃临时状态并返回 `E_DATA` 字段路径
- 连接键类型必须一致；聚合参数必须存在；SELECT 中非聚合表达式必须出现在 GROUP BY

## 计划与优化

优化计划稳定为：

```text
Project(final output order)
  Aggregate(group_by, aggregates)
    Project(join-side required columns)
      Join(inner equi-keys, pruned output columns)
        Scan(alias o, required columns, pushed filters)
        Scan(alias c, required columns, pushed filters)
```

基线计划保留全部扫描列，并在每个扫描之上保留独立 `Filter`，执行器不会把该过滤融合进扫描任务。

优化阶段：

1. 将 WHERE 的每个 AND 合取条件按其唯一基表引用下推到对应 Scan。
2. 从聚合/分组需求反向推导扫描所需列，并加上连接键和本地过滤条件列。
3. Join 只输出后续聚合和最终投影实际需要的列；join 键若不再向上使用不会进入中间结果。
4. Scan、Join 中间结果和聚合输入 Project 均按原 schema 顺序稳定裁剪。
5. 裁剪只裁剪列，不改变保留列的类型描述符、值或空值位图。

`logical_plans.json` 中会写入：

- `pushdown.pushed_conditions`：条件、扫描算子、表、推导理由
- `pruning.dropped_columns`：位置、列、类型、裁剪理由
- `pruning.kept_columns`：保留原因（过滤/连接键/分组/聚合/投影）
- `pruning.join_intermediate`：连接中间结果保留与丢弃列
- `pruning.derivation`：反向需求推导和空值语义说明

## 定点估算与切分

选择度使用 `1_000_000` 为 1.0 的整数定点值。统计快照包含行数、空值数、非空数、distinct 数、min/max、最多 256 个值频次。多个下推条件按确定性频次/范围策略估算并做定点乘法。

每个扫描输出：

- `selectivity_ppm`
- `estimated_output_rows`
- `quantization_error_rows_upper_bound`
- `fixed_point_scale`

切分策略固定为：

```text
task_count = min(parallelism, ceil(estimated_output_rows / min_rows_per_task))
rows_per_task = ceil(table_row_count / task_count)
task_i.input_range = [i * rows_per_task, min(row_count, (i+1) * rows_per_task))
```

任务数不会超过全局并行度预算；区间连续、覆盖完整、无重叠。相同输入必然得到相同 `split_sha256`。

## 并行执行与内存预算

每个扫描/过滤任务分配一个线程池工作项。调度器在提交任务前先预留该任务峰值内存：

```text
task_peak = 4096 + 2 * input_rows * (
    8                                     # row index buffer
  + Σ(column_buffer_width + 1)           # values + 1-byte null bitmap
)
```

`column_buffer_width` 对 bool 为 1、int64/float64 为 8、string 为 `4 + max(UTF-8 字节长度)`。输入缓冲区与过滤输出缓冲区都计入估算。

调度规则：

- 活跃任务数 `< parallelism`
- 活跃任务的峰值内存估算之和 `<= memory_budget_bytes`
- 队首任务内存不足时保持排队，绝不允许绕过预算
- 单任务本身超过预算直接返回 `E_BUDGET`
- 只有任务完成并释放预留后，队列才继续准入

轨迹按稳定任务顺序写入，每个任务包含 `start_ms`、`end_ms`、`duration_ms`、`budget_queue_wait_ms`、`input_range`、`input_rows`、`completed_rows`、`peak_memory_bytes` 等。时钟来自 `time.perf_counter_ns()`，为单调时钟毫秒值。

## 字节一致性

聚合结果按固定格式序列化为 `CPAGG1` 大端字节流，包含：

- 列数、列名、列类型、nullable 标志和列序
- 行数
- 每个值的 null 标记
- int64 / float64 / bool / UTF-8 string 的定长或长度前缀编码

优化路径和基线路径分别执行，最终比较原始字节，而不是只比较 JSON。若不同会抛出 `E_DIVERGE`，并给出首个差异分组键、字段、两边值和列序，同时校验两条路径的：

- join 左右输入行数
- join 输出行数
- aggregate 输入行数
- 结果行数和列序
- 收集统计与任务轨迹行数口径

## 快照与恢复

`artifacts/demo/session_snapshot.json` 自包含：

- snapshot format / format version / SHA-256 校验和
- session id、data version、表结构版本、schema fingerprint、data digest
- 完整表数据
- statistics 快照
- 可重建索引集合
- 计划历史
- 优化/基线计划、摘要、下推、裁剪、估算、切分
- 两条执行路径的聚合结果、规范字节、轨迹与收集统计

恢复规则：

- snapshot checksum、格式版本、嵌入数据 digest、schema fingerprint、统计和索引必须自洽
- 相同 `session_id` 的数据版本不一致是合法恢复：**整体替换**表、统计、索引和计划历史，不做增量合并
- 不同 `session_id`、未知 snapshot format/version、或嵌入状态无法对应时返回 `E_VERSION`，并列出可辨识差异
- 恢复报告明确写 `mode = replace_all_not_merge`

## 命令

```bat
:: 生成样本
python -m colpipe generate --workspace .

:: 运行优化路径
python -m colpipe run --input artifacts/demo/pipeline_input.json --mode optimized --output-dir artifacts/optimized-run

:: 运行基线路径
python -m colpipe run --input artifacts/demo/pipeline_input.json --mode baseline --output-dir artifacts/baseline-run

:: 一次执行两路并比较、写快照
python -m colpipe compare --input artifacts/demo/pipeline_input.json --output-dir artifacts/compare

:: 独立验证快照与产出文件
python -m colpipe verify --snapshot artifacts/demo/session_snapshot.json --output artifacts/demo/verification.json --artifacts artifacts/demo

:: 从快照整体替换恢复并重放优化路径
python -m colpipe restore --snapshot artifacts/demo/session_snapshot.json --current artifacts/demo/pipeline_input_v2.json --output-dir artifacts/restore

:: 三场景演示
python -m colpipe demo --workspace .

:: Tkinter 界面
python -m colpipe gui --workspace .
```

退出码：正常 `0`；一般业务拒绝 `2`；`compare` 发现差异 `3`；验签失败 `4`。

## 业务码

| 业务码 | 含义 |
|---|---|
| `E_DATA` | JSON 结构、类型、表/列、空值位图或查询引用不合法 |
| `E_PARSE` | 查询语法或不受支持的语法 |
| `E_PLAN` | 计划/执行元数据错误 |
| `E_BUDGET` | 单任务或队列无法在内存预算内满足 |
| `E_DIVERGE` | 优化与基线结果字节不一致 |
| `E_VERSION` | 快照版本、校验和、嵌入状态或版本线无法对应 |
| `E_INTERNAL` | 未预期内部错误 |

## 界面

`run_gui.bat` 或 `python -m colpipe gui --workspace .` 启动后默认加载/生成样本。界面包含：

- 左侧稳定顺序的逻辑计划树
- “计划与推导”：查询/计划摘要和剪枝依据
- “下推与裁剪”：下推条件集合、裁剪列集合
- “切分与估算”：定点选择度、任务数、行区间
- “聚合结果”：列序、结果和字节摘要
- “任务轨迹”：优化/基线任务起止时间、完成行数、峰值内存
- “资源时间线”：按优化/基线分行的任务占用条
- 底部业务码与预算状态

渲染顺序由计划和轨迹的稳定 ID 顺序决定，不使用集合迭代顺序。

## 本次实际验证结果

运行环境：Linux 侧验收容器，Python `3.11.0rc1`；该容器未安装 `tkinter` 与 `numpy`，因此核心使用标准库回退路径。GUI 源码已通过 `py_compile` 与无 Tk 导入测试；Windows 上需按快速开始命令确认本地 Tk。

测试：

```text
python -m unittest discover -s tests -v
Ran 13 tests
OK
```

三场景演示：

```text
python -m colpipe demo --workspace .
status: PASS
full: PASS
optimized: PASS
restore_replay: PASS
```

样本规模：customers 128 行 × 6 列，orders 4096 行 × 8 列；并行度 4，内存预算 256000 bytes。

实测关键数据：

- 下推条件：4 个
- 裁剪列：10 个
- 优化扫描任务：5 个（orders 4 + customers 1）
- 优化扫描输入：4224 行，扫描输出：1207 行
- 基线扫描输出：4224 行
- 聚合结果：10 组 × 5 列
- 优化单任务峰值估算：98304 bytes
- 优化调度并发预留峰值：196608 bytes <= 256000 bytes
- 基线单任务峰值估算：186368 bytes
- 基线调度并发预留峰值：208384 bytes <= 256000 bytes
- 优化/基线规范字节 SHA-256：`ebb9290fb8b83e1844b8c8b0d7b87908973fe89a3e7f33404b1db91e09c76d8d`
- 字节一致：`true`
- 独立验证：`PASS`，47 项检查，0 失败

快照恢复场景中，当前状态被改为 `sales-2026-11-17.v2`，随后从 v1 快照整体替换恢复为 `sales-2026-11-17.v1`；恢复后重放结果与原优化结果字节一致，统计快照可重建。差异报告明确标识了 `orders` 表数据摘要变化。

## 设计边界

- 当前只支持两个表、一个内连接和最终分组聚合，符合任务声明范围。
- 扫描/过滤阶段进入资源预算调度；哈希连接与聚合在扫描收集完成后串行执行，其额外哈希表内存不纳入“每任务列缓冲区”预算。该边界在快照 `artifact_contract` 中明确记录。
- 由于使用 Python 线程，GIL 下 CPU 并行收益有限；实现重点是可验证的并行调度、预算准入和确定性结果，而非吞吐基准。
- 样本数据、样本查询和所有分叉状态均由本地确定性 LCG 生成，不访问网络。
