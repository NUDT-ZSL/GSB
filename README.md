# 城市公共数据列式批处理求值组件

仅使用 Python 标准库实现的独立验收组件。它把同一张人口、交通与环境统计逻辑表做成
三个字段命名、定点刻度表示和空值位图编码均不同的 schema 版本，归一后按批进行列向量
求值，并用另一套独立实现的三值逻辑参考求值器逐批、逐节点核对结果。

项目不读取外部数据文件、不访问网络，样本由固定种子确定性生成。

## 快速运行

要求 Python 3.10+（WSL Ubuntu 自带 `python3` 即可），不安装第三方包。

```bash
python3 main.py
```

默认执行 257 行、每批 32 行的完整验收，并生成：

- `artifacts/acceptance_report.json`：完整 JSON 验收报告，含逐批节点账目。
- `artifacts/generated_sample.json`：三 schema 原始样本与三份表达式规格。

常用参数：

```bash
python3 main.py --seed 20261128 --rows 257 --batch-rows 32 \
  --max-rows 64 --max-input-columns 12 --max-working-columns 64 --max-cells 4096

python3 main.py --compact  # 报告省略重复的逐批节点结果副本，但保留账目
```

退出码：

- `0`：验收状态为 `PASS`。
- `1`：存在验收失败项。
- `2`：批次或预算参数违反声明限制，已按 `reject` 策略拒绝。

## 项目结构

```text
main.py                      命令行入口
columnar_eval/
  generator.py               确定性样本与三 schema/表达式规格生成
  normalize.py               空值解码、字段映射、刻度对齐、版本差异清单
  typesys.py                 类型提升、向量类型转换
  evaluator.py               主列向量化求值、条件分支、逐节点账目
  reference.py               独立三值逻辑参考求值器
  verify.py                  逐批逐节点核对、版本一致性核对
  acceptance.py              验收编排与报告
  model.py                   类型、列向量、批次预算、拒绝异常
tests/test_acceptance.py     标准库 unittest 验收用例
```

## 三个 schema 版本

三个版本描述同一逻辑表 `municipal_metrics`，但保留各自原始格式：

| 版本 | 字段命名 | 定点刻度表示 | 空值位图 |
| --- | --- | --- | --- |
| `v1` | `snake_case` 业务别名 | 小数位数，物理刻度 1 | 逐列位串，MSB first |
| `v2` | 大写前缀历史名 | 10 的幂乘子，物理刻度 2 | 全局比特游标，列级 bit offset |
| `v3` | `camelCase` 目录名 | 负指数，物理刻度 3 | 全局十六进制字节游标，LSB first |

每列数据覆盖 `int64`、`decimal`、`string`、`bool`。空值不靠数据哨兵推断，而是从显式
位图解码；位图中 `1 = null`、`0 = 非 null`。

归一化会：

1. 按 `field_map` 把原始字段名映射到统一列名，并在模型中保留双向映射。
2. 把物理定点值精确重标到统一 `decimal(scale=3)`；只允许整数乘除，若无法整除则报错，
   不做静默舍入。
3. 把三种空值编码统一为每列显式布尔掩码，并保留 `msb_first` 的规范化位串。
4. 把各版本表达式里的原始字段名改写为统一列名，同时记录改写清单。
5. 生成版本差异清单。允许差异仅为字段命名、刻度表示、空值编码；归一后的逻辑列集合、
   数据类型、值与掩码必须一致。

## 类型提升规则

每个非叶子节点都在账目中记录操作数类型、目标类型、结果类型、转换项和判定依据。

- `int64 + decimal(scale=s)`：提升为 `decimal(scale=s)`，整型按 `raw * 10^s` 转换。
- `decimal(scale=s1) + decimal(scale=s2)`：提升到 `decimal(scale=max(s1,s2))` 后再算。
- `bool` 与 `int64` 比较：`bool` 先提升为 `int64`，`false -> 0`、`true -> 1`，结果为
  `bool`。
- `bool` 与 `decimal` 比较：`bool` 提升为对应刻度的 `decimal` 后比较。
- 乘法：整型/布尔先转 `int64`；小数结果刻度等于各操作数小数刻度之和。
- 除法：工作类型至少为 `decimal(scale=3)`，超过工作刻度的数字向零截断。
- `coalesce`、条件表达式分支：先求公共类型，再转换并合并。
- `abs` 保持整数或小数类型；`lower` 返回字符串；`length` 返回 `int64`。

## 三值逻辑

算术和比较节点：任一操作数为空，输出为空。

| AND | false | true | null |
| --- | --- | --- | --- |
| false | false | false | false |
| true | false | true | null |
| null | false | null | null |

| OR | false | true | null |
| --- | --- | --- | --- |
| false | false | true | null |
| true | true | true | true |
| null | null | true | null |

`NOT null = null`。主求值器和独立参考求值器都按上述真值表实现。

## 条件表达式与空值隔离

`conditional` 节点先整列求值条件，形成 true、false、null 三个选择向量。true/false
分支只在其选中行索引上分别求值，再按公共结果类型合并：

```text
结果为空 = 条件为空行 + 真正选中的 true 分支空值 + 真正选中的 false 分支空值
```

未选中分支上的空值不会进入合并结果。账目记录条件 true/false/null 行数、两个分支的
选择行数与空值数、合并空值数、选择行索引及 `unselected_branch_nulls_isolated` 标记。

## 批次预算与拒绝

`BatchBudget` 声明：

- `max_rows`：单批行数上限。
- `max_input_columns`：输入列宽上限。
- `max_working_columns`：输入列加表达式临时节点的估算工作列宽上限。
- `max_cells`：单批单元格上限。
- `rejection_policy`：默认 `reject`。

`evaluate_batch()` 在求值前检查输入行数、输入列宽和单元格数；每个表达式求值前检查工作
列宽。任一超限都会抛出 `BudgetRejected`，报告 `stage`、`metric`、`observed`、`limit`
与策略，不会截断数据。计划分批行数超过 `max_rows` 时也直接拒绝。

## 逐节点账目

每批每个节点记录：

- `evaluated_rows`、`row_start`、条件分支的选择行索引。
- `null_input_count`、`null_inputs_by_operand`、`null_output_count`。
- `output_type`、`promotion`（类型提升和刻度对齐依据）。
- 节点结果列、结果空值掩码与 `null_bitmap`。
- 表达式汇总中的节点聚合统计和各批次行数。

报告同时输出每个表达式的完整结果列与空值掩码，三版本结果按归一后的裸整数和掩码比较，
避免浮点表示造成口径漂移。

## 独立参考核验

`reference_evaluate_model()` 使用独立代码路径复算类型规则、三值逻辑、函数和条件分支，
不调用主求值器的算子实现。`verify_against_reference()` 按批次、表达式、节点顺序核对：

1. 结果类型、长度、空值掩码逐位比较。
2. 非空值逐位置比较。
3. 条件分支节点还比较选择行索引。

出现不一致时抛出 `ReferenceMismatch`，其中 `first_mismatch` 包含：

- `batch_index`、`expression_id`、`node_id`、`position`、`null_mask_position`、
  `column_position`、`result_column_index`。
- 主结果与参考结果值。
- 完整 `main_bitstring`、`reference_bitstring`、`difference_bitstring` 和差异位置。

## 编程接口

```python
from columnar_eval import (
    BatchBudget, generate_sample, normalize_all, evaluate_model,
    reference_evaluate_model, verify_against_reference, run_acceptance,
)

sample = generate_sample(seed=20261128, row_count=257)
models = normalize_all(sample)
budget = BatchBudget(
    max_rows=64,
    max_input_columns=12,
    max_working_columns=64,
    max_cells=4096,
)

main = evaluate_model(models["v1"], batch_rows=32, budget=budget)
reference = reference_evaluate_model(models["v1"], batch_rows=32, budget=budget)
check = verify_against_reference(main, reference)

report = run_acceptance(
    seed=20261128,
    row_count=257,
    batch_rows=32,
    budget=budget,
    include_run_details=True,
)
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖确定性生成、三 schema 编码差异、归一化恢复、版本差异清单、完整的
AND/OR/NOT 三值真值表、条件分支空值隔离、行/列预算拒绝、类型提升记录、首个差异定位
以及完整验收流程。
