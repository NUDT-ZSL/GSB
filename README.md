# Fixed-point Shuffle Hash Join Engine

面向 Windows 本地环境的确定性 Shuffle 哈希连接与定点聚合引擎。输入为本地 JSON 两张逻辑表和一个 `INNER` 连接查询，输出包含：

- 按量化连接键进行的稳定 Shuffle 分区摘要；
- 每个分区的开放寻址桶容量、装载因子、探测次数、溢出区访问和连接统计；
- 浮点投影的参考值、定点值及逐行误差界；
- 分区级与全局的定点计数、求和、均值及其量化误差上界；
- Shuffle、桶探测、聚合三段误差传播路径和显式阈值下的主导段；
- `E_NULLMAP`、`E_SCALEF`、`E_PART` 三类可注入异常的结构化日志。

核心实现只使用 Python 标准库。`requirements.txt` 中的 NumPy 是可选环境检查项，不参与确定性业务计算。

## 环境准备

在 Windows PowerShell 或 cmd 中执行：

```text
cd <project-root>
python --version
python -c "import numpy; print(numpy.__version__)"
python tools\check_env.py
```

要求 Python 3.11 及以上。若需要安装 NumPy：

```text
python -m pip install -r requirements.txt
```

项目不会访问网络、真实账户或远程集群。所有输入、报告和日志均写入本地文件系统。

## 快速运行

PowerShell：

```powershell
python run.py run --tables data/sample_tables.json --query data/sample_query.json --partitions 4 --scale-factor 100 --output artifacts/run4
python run.py verify --reports artifacts/run4/report.json --output artifacts/verification.json
python scripts/run_demo.py
```

cmd：

```bat
python run.py run --tables data\sample_tables.json --query data\sample_query.json --partitions 4 --scale-factor 100 --output artifacts\run4
python run.py verify --reports artifacts\run4\report.json --output artifacts\verification.json
python scripts\run_demo.py
```

`run.py` 自带 `src` 路径注入，因此无需安装包即可运行。演示脚本会顺序执行三次实验和三次异常注入，并调用独立校验器。

## 演示结果

生成结果位于 `experiments/`：

- `exp1_single_partition`：单分区，`S=100`，13 条连接结果；
- `exp2_multi_partition`：4 分区，`S=100`，13 条连接结果；
- `exp3_scale_perturbation`：4 分区，`S=10000`，10 条连接结果；
- `injections/nullmap`、`injections/scalef`、`injections/part`：三类异常报告；
- `verification.json`：独立重算通过的校验结果；
- `demo_summary.md`、`demo_summary.json`：实验总览。

当前样本在 `S=100` 时产生 2 个“量化后相等但浮点参考值不等”的连接对，以及 1 次谓词比较边界不一致；这两类事件均显式写入报告，不会被静默忽略。`S=10000` 时该量化碰撞消失，体现缩放因子扰动的影响。

## 输入格式

### 表文件

`data/sample_tables.json` 顶层必须包含恰好两个表。每列声明 `type` 为 `int` 或 `float`；整数列必须声明 `min_value` 和 `max_value`；可空列必须提供与行数完全一致的 0/1 空值位图。

```json
{
  "tables": [
    {
      "name": "measurements",
      "columns": [
        {"name": "row_id", "type": "int", "nullable": false, "min_value": 1, "max_value": 100},
        {"name": "sensor_key", "type": "float", "nullable": true}
      ],
      "null_bitmap": {"sensor_key": [0, 0]},
      "rows": [
        {"row_id": 1, "sensor_key": 1.2},
        {"row_id": 2, "sensor_key": null}
      ]
    }
  ]
}
```

实际文件还包含第二张表。加载时会校验表名和列名唯一、列名标识符合法、行字段完整、空值位图长度和值一致、整数范围及浮点有限性。表结构错误使用 `E_TABLE`；空值位图错误使用 `E_NULLMAP`，并附带 `parent_code: E_TABLE`、表名、行号和列名。

### 查询文件

查询支持任意一侧或双侧谓词、投影列、构建侧选择，以及一个浮点或整数聚合源列。连接类型只允许 `INNER`。

```json
{
  "join_type": "INNER",
  "left_table": "measurements",
  "right_table": "calibrations",
  "join_key": {"left": "sensor_key", "right": "sensor_key"},
  "build_side": "right",
  "predicates": {
    "left": [{"column": "reading", "op": ">=", "value": 10.0}],
    "right": [{"column": "active", "op": "=", "value": 1}]
  },
  "projection": [
    {"name": "reading_left", "side": "left", "column": "reading"}
  ],
  "aggregate": {"side": "left", "column": "reading"}
}
```

比较算子支持 `=`, `!=`, `<`, `<=`, `>`, `>=`。查询结构或列引用错误返回 `E_QUERY`。

## 定点与误差规则

缩放因子必须是 `S = 10^e`，其中 `e` 为 0 到 9 的整数。浮点量化采用负无穷方向取整：

```text
q(x) = floor(x * S)
x_hat = q(x) / S
```

量化实现使用 IEEE 浮点值的精确整数比值 `numerator/denominator`，再计算 `(S * numerator) // denominator`，不使用浮点乘除。

主要误差界均以整数格为单位：

- 单值：`x_hat - x` 属于 `[-1/S, 0)`，上界为 1 格；
- 比较：落入同一格的两个参考值之差小于 `1/S`，边界判定误差上界为 1 格；
- 求和：`n` 个浮点输入的和误差小于 `n/S`，保守上界为 `n` 格；
- 均值：求和误差加一次朝零截断误差，上界为 `n + 1` 格；
- 整数输入量化误差为 0。

均值定义为 `trunc_toward_zero(sum_fixed / count_non_null)`。空值不参与求和与均值，并单独计入 `count_null`。

## Shuffle、连接与桶

- 整数连接键使用域 `i64`；浮点连接键先量化，再使用域 `f64q`；跨整数/浮点连接时整数侧乘以 `S`；
- 哈希输入为 ASCII `fixed-join-v1|<domain>|<signed_lattice_value>` 的 SHA-256；
- 取摘要前 8 字节作为大端无符号整数，对分区数取模；
- 分区摘要在分区内按规范 JSON 行内容的 SHA-256 排序后计算，因此与执行顺序和并发调度无关；
- 每个分区为构建侧建立开放寻址多值哈希表，容量为 `next_power_of_two(max(4, 2 * build_rows))`；
- 线性探测上限为 8，超过上限的键进入显式溢出区；探测和溢出区访问逐分区统计；
- 空连接键不参与 INNER 匹配；
- 定点键相等而浮点参考值不等的连接对进入 `quantized_reference_collisions`，并在输出行标记 `reference_collision: true`。

## 误差归因

输出行在全局规范顺序下计算归因，顺序仅由 `(fixed_join_key, left_row_index, right_row_index)` 决定，因此不依赖分区数或分区执行顺序。

- 量化界 = 两侧浮点连接键界 + 非空浮点投影列数；
- 聚合前缀界 = 当前行之前及当前非空浮点聚合输入的累计数量；
- 两者相等为 `both`，量化界更大为 `quantization`，聚合前缀界更大为 `aggregation`。

三段报告分别计算：

- Shuffle：输出行数乘以浮点连接键侧数；
- 桶探测：量化参考碰撞数加谓词比较边界不一致数；
- 聚合：非空浮点聚合输入数，等于求和误差界。

若最大段的误差界占三段总界的比例至少为 `50%`，该段为主导段；否则主导段为 `mixed`。判定规则写入报告。

## 异常注入

```text
python run.py run --inject nullmap --output artifacts/inject_nullmap
python run.py run --inject scalef --output artifacts/inject_scalef
python run.py run --inject part --output artifacts/inject_part
```

预期业务码与退出码：

| 注入 | 业务码 | 退出码 | 变更位置 |
| --- | --- | ---: | --- |
| `nullmap` | `E_NULLMAP` | 2 | 在输出目录生成缩短空值位图的输入副本 |
| `scalef` | `E_SCALEF` | 2 | 会话参数 `S=25`，非 10 的整数幂 |
| `part` | `E_PART` | 2 | 会话参数分区数为上限 64 加 1 |

每次失败都会写 `error.json` 和 `events.jsonl`。`nullmap` 注入不会修改原始样本文件。

## 校验器

校验器独立读取报告和源输入，重算：

- 每个分区的行数、空键数、左右摘要和组合摘要；
- 定点连接结果集及量化碰撞集合；
- 桶容量推导、投影定点值和聚合输入的量化；
- 分区级与全局计数、求和、均值、误差界；
- 逐行误差归因和跨分区归因一致性；
- 三段误差贡献、占比、主导段和 50% 阈值规则；
- 注入报告是否返回预期业务码。

首个偏差会写入 `verification.json` 或指定的 `--output`。普通重算偏差使用 `E_VERIFY`；归因不一致使用 `E_ATTR`。

## 验证命令

```text
python -m unittest discover -s tests -v
python scripts/run_demo.py
python -m compileall -q src run.py tools scripts tests
```

当前环境实际执行结果为 11 个单元/集成测试全部通过，3 份成功报告和 3 类注入报告全部通过独立校验。详细实验数据见 `experiments/demo_summary.md`。

## 项目结构

```text
run.py                         主命令行入口
src/fixed_join/                校验、量化、Shuffle、哈希表、连接、聚合、归因、校验实现
data/sample_tables.json        确定性双表样本
data/sample_query.json         INNER 查询定义
scripts/run_demo.py            三次实验与三次注入的可重复演示
tools/verify_reports.py        独立校验入口
tools/check_env.py             Python/NumPy 环境检查
tests/                         标准库 unittest 测试
experiments/                   已生成的报告、日志、汇总和校验结果
docs/design.md                 公式、状态机和契约说明
```

## 边界说明

这是单机、确定性、本地文件驱动的执行核心，不实现真实网络调度器或分布式容错。分区统计模拟分区执行边界，哈希与摘要规则可直接迁移到并发或分布式执行器。核心计算不依赖 NumPy；NumPy 仅作为可选环境项，用于满足本地分析环境的常规准备。
