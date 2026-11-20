# Transformer Embedding Gather / Reduce 数值诊断组件

这是一个从零实现的本地可集成组件，面向 Transformer 输入侧的嵌入表查询、重复命中归约、padding 掩码、位置权重累加，以及以 float64 全精度路径为参照的逐级误差传播诊断。

项目只依赖 Python 标准库与 NumPy，不使用容器，不访问外部数据或网络目标。默认样例完全由固定种子在本地生成。

## 快速开始

要求 Python 3.10+、NumPy 1.24+。

Windows 默认终端：

```text
py -3 -m pip install "numpy>=1.24"
py -3 generate_samples.py
py -3 run_demo.py --output artifacts\report.json
py -3 -m unittest discover -s tests -v
```

Linux / macOS：

```text
python3 -m pip install "numpy>=1.24"
python3 generate_samples.py
python3 run_demo.py --output artifacts/report.json
python3 -m unittest discover -s tests -v
```

也可以直接使用模块入口：

```text
python -m embedding_ops --output artifacts/report.json
```

默认样例清单为 `samples/tasks.jsonl`，默认报告为 `artifacts/report.json`。如果清单不存在，CLI 会先确定性生成它。

## 项目结构

```text
embedding_ops/
  config.py       数值配置、舍入模式、次正规数策略、显式容差
  errors.py       结构化业务异常与业务码
  models.py       JSONL 任务模型
  indexing.py     索引描述解析、确定性生成、越界与 padding 掩码
  generator.py    固定种子嵌入表、索引、位置权重生成
  quantize.py     float/qint 表构造、反量化、独立重算校验
  numeric.py      float64 舍入、次正规数保护、加权累加
  pipeline.py     gather_embedding、reduce_and_weight、run_job
  diagnostics.py  逐级误差谱、主导来源归因、自检、错误注入
  snapshots.py    配置/量化参数/误差统计的版本化整体替换
  reporting.py    完整 JSON 报告与精度档切换
  cli.py          命令行入口
samples/tasks.jsonl
artifacts/report.json
tests/
TECHNICAL.md
run_demo.py
generate_samples.py
```

## 公开接口

```python
from embedding_ops import (
    JobSpec,
    NumericsConfig,
    run_job,
    gather_embedding,
    reduce_and_weight,
    build_error_spectrum,
    run_error_injection_suite,
    self_check_job,
)
```

`run_job` 接受 `JobSpec` 或清单字典，返回包含输出区间、权重明细、逐级误差谱和自检结果的 JSON 兼容字典。

最低层 Gather 与归约接口也可以单独集成：

```python
import numpy as np

from embedding_ops import NumericsConfig, gather_embedding, reduce_and_weight

table = np.arange(20, dtype=np.float64).reshape(5, 4)  # (vocab, embed)
indices = np.array([[1, 1], [3, 0]], dtype=np.int64)   # (tokens, hits)
pad_mask = np.array([[False, False], [False, False]])
position_weights = np.array(
    [[1.0, 0.5, -0.5, 2.0], [0.2, 0.3, 0.4, 0.5]],
    dtype=np.float64,
)

gathered = gather_embedding(table, indices, gather_axis=0, job_id="demo")
result = reduce_and_weight(
    gathered,
    indices,
    pad_mask,
    position_weights,
    reduction_mode="sum",
    pad_policy="exclude",
    config=NumericsConfig(),
    job_id="demo",
)
print(result.output)
```

`gather_axis=0` 使用 `(vocab_size, embed_dim)` 表。`gather_axis=1` 使用转置后的物理布局 `(embed_dim, vocab_size)`，但仍返回 `(tokens, hits, embed_dim)`，索引范围仍由 `vocab_size` 约束。

## 任务清单

清单是 UTF-8 JSONL，每行一个对象。字段如下：

| 字段 | 含义 |
| --- | --- |
| `job_id` | 唯一任务标识 |
| `vocab_size` | 可查询嵌入行数 |
| `embed_dim` | 每行向量维度 |
| `table_dtype` | `float64`、`float32`、`qint8`、`qint16` |
| `index_set` | 显式索引数组或确定性生成描述 |
| `gather_axis` | `0` 或 `1` |
| `reduction_mode` | `sum`、`mean`、`first`、`concat` |
| `pad_index` | 合法 padding 索引；`-1` 表示无 padding |
| `scale` | 位置权重尺度，必须非负 |
| `quant_scale` | 定点量化步长 |
| `quant_zero_point` | 零点 |
| `seed` | 固定样本种子 |
| `pad_policy` | 可选任务级覆盖：`exclude` 或 `zero_weight` |

`index_set` 支持：

```json
{
  "kind": "generated",
  "num_tokens": 6,
  "hits_per_token": 2,
  "low": 1,
  "high": 15,
  "duplicate_of": [
    {"at": [2, 1], "source": [0, 0]}
  ],
  "duplicate_positions": [[5, 1]],
  "pad_positions": [[1, 1], [4, 0]]
}
```

也支持显式二维数组：

```json
{"kind": "explicit", "values": [[1, 2], [3, 4]]}
```

或直接写二维数组。生成模式中：

- `duplicate_of.at` 复制 `duplicate_of.source` 的索引。
- `duplicate_positions` 的每个位置复制同一 token 的前一命中；若为每行首命中，则复制前一 token 的末命中。
- `pad_positions` 强制写入 `pad_index`。

## 确定性与数值语义

固定种子通过三个独立随机流生成数据：

- 嵌入表：`SeedSequence([seed, 0])`
- 索引数组：`SeedSequence([seed, 1])`
- 位置权重：`SeedSequence([seed, 2])`

全精度参考表为标准正态 float64 数组；位置权重为标准正态数组乘以 `scale`。

对 `qint8` / `qint16`：

```text
q = round_mode(truth / quant_scale + quant_zero_point)
q = clip(q, qmin, qmax)
dequantized = (q - quant_zero_point) * quant_scale
```

`quant_scale <= 0`、非有限值或零点越界返回 `E_QUANT_INVALID`。float 档不应用定点参数，但仍在报告中记录 `quant_applicable=false`。

归约系数 `alpha[token, hit]`：

- `sum`：非 padding 命中为 `1`。
- `mean` + `exclude`：分母为当前 token 的非 padding 命中数。
- `mean` + `zero_weight`：分母仍为 `hits_per_token`，padding 系数为 `0`，因此显式保留“零权重计入”的语义。
- `first`：第一个非 padding 命中为 `1`。
- `concat`：不做跨命中求和，按命中顺序展平为 `hits_per_token * embed_dim` 维向量。

非 concat 输出：

```text
output[t, d] = sum_k alpha[t, k] * position_weight[t, d] * table[index[t, k], d]
```

每个命中都会写入 token 权重明细：索引、是否 padding、归约权重、位置权重、组合权重和输出切片。`token_weight_details` 与反量化表可以独立重算最终输出，自检会实际执行该重算。

所有中间数学均使用 float64。舍入支持：

- `nearest_even`
- `nearest_away`
- `truncate`
- `floor`
- `ceil`

反量化可配置 `dequant_rounding_step`，加权累加可配置 `accum_rounding_step`。次正规数策略支持 `preserve`、`flush_to_zero`、`clamp_to_threshold`，阈值可配置。容差固定为：

```text
abs_diff <= tolerance_abs + tolerance_rel * max(1, abs(reference))
```

策略名为 `fixed_no_auto_widening`。低精度任务不会通过放大容差或跳过低精度档来“通过”。

## 误差谱与归因

每个任务比较四个阶段：

1. `dequantize`：反量化表 vs float64 真值表。
2. `gather`：实际查表结果 vs 真值表查表结果。
3. `reduce`：实际归约结果 vs 参考归约结果。
4. `accumulate`：实际逐 token 输出 vs 参考输出。

每级输出：

- 最大绝对偏差与最大相对偏差。
- 有符号误差区间 `[min(actual-reference), max(actual-reference)]`。
- 相对上一阶段的误差放大比与相对反量化阶段的累计放大比。
- 最大偏差元素位置。
- 观测数值分辨率及其方法。
- 相对于参考值的容差判定。

主导来源使用可隔离的最终输出贡献，而不是简单比较阶段峰值：

```text
dequantization =
  max(abs(actual_weights_without_accum_rounding - float64_reference))

weighted_accumulation =
  max(abs(final_output - actual_weights_without_accum_rounding))

reduction_weights =
  归约系数与参考路径的最大绝对差；当前参考路径复用同一 float64 掩码和系数
```

这避免把位置权重对既有反量化误差的放大误判为新的累加误差。报告中的 `dominance_trace`、`attribution_basis`、`reduction_weight_delta_max_abs` 和 `dequant_only_final_max_abs_deviation` 可供复核。

输出区间采用快照内参考真值包络加显式容差：

```text
lower = min(actual, reference) - tolerance
upper = max(actual, reference) + tolerance
```

区间精度标记为 `float64`，并记录 `contains_actual` 与 `contains_reference`。

## 业务错误与注入

清单级和流水线级错误均包含 `code`、`stage`、`job_id`、`message`、`detail`：

| 业务码 | 典型阶段 | 触发条件 |
| --- | --- | --- |
| `E_INDEX_OUT_OF_RANGE` | `index_validation` / `gather` | 首个索引越界，含第一个越界位置 |
| `E_INDEX_EMPTY` | `index_validation` | 索引数组为空 |
| `E_INDEX_INVALID` | `index_validation` | 非整数或维度不合法 |
| `E_PAD_INDEX_INVALID` | `index_validation` | `pad_index` 越界 |
| `E_QUANT_INVALID` | `table_build` | 量化步长非正或零点越界 |
| `E_SHAPE_MISMATCH` | `table_build` / `weighted_accumulation` | 表维度、权重维度不符 |
| `E_SCALE_NEGATIVE` | `table_build` | `scale < 0` |
| `E_DTYPE_UNKNOWN` | `table_build` | 未知精度档 |
| `E_DUP_JOB` | `manifest` | 重复 `job_id` |
| `E_SNAPSHOT_ROLLBACK` | `snapshot_load` | 旧版本覆盖新版本 |
| `E_SNAPSHOT_CORRUPT` | `snapshot_load` | 校验和、结构或同版本内容不符 |

内置注入套件覆盖形状不符、空索引、负 `scale`、未知精度档、越界索引、非法量化参数、非法 padding 索引和重复 `job_id`。预期异常均记录阶段和任务归因；非预期异常进入报告顶层 `unattributed_exceptions`，不会静默吞掉。

## 版本化快照

`config`、`quant_params`、`spectrum_stats` 三类快照都带有：

- 单调递增版本号。
- 基于 `kind + version + content` 规范 JSON 的 SHA-256 校验和。
- 完整内容对象。

加载规则：

- 首次加载：整体写入。
- 更高版本且校验通过：整体替换，不做字段合并。
- 同版本且校验和相同：`unchanged`。
- 同版本但校验和或内容不同：`E_SNAPSHOT_CORRUPT`。
- 更低版本：`E_SNAPSHOT_ROLLBACK`。
- 校验和不符或结构损坏：`E_SNAPSHOT_CORRUPT`。

示例报告会验证替换后旧字段已消失，且回退/损坏尝试没有改变当前版本。

## CLI 选项

```text
python run_demo.py --help
```

常用选项：

```text
--jobs PATH
--output PATH
--regenerate-samples
--rounding-mode {nearest_even,nearest_away,truncate,floor,ceil}
--dequant-rounding-step FLOAT
--accum-rounding-step FLOAT
--subnormal-policy {preserve,flush_to_zero,clamp_to_threshold}
--subnormal-threshold FLOAT
--pad-policy {exclude,zero_weight}
--tolerance-abs FLOAT
--tolerance-rel FLOAT
--compact
```

## 实际验证结果

本轮验证环境为 Python 3.10.12、NumPy 1.26.4。

```text
python -m unittest discover -s tests -v
Ran 30 tests
OK
```

正式报告 `artifacts/report.json` 的结果：

- 主任务：8/8 成功，7 个降级标记。
- 自检：8/8 通过。
- 注入：8/8 按预期抛出结构化业务码。
- 快照：2 次拒绝，分别触发 `E_SNAPSHOT_ROLLBACK` 与 `E_SNAPSHOT_CORRUPT`。
- 未归因异常：0。
- 业务码共 10 类，均进入 `summary.business_codes`。
- 报告字节 SHA-256：`95ac7f4625447f258f74c44eb16af9aa283be366a84a793e370ebb44dd741147`。

同一任务的精度档切换（最大最终绝对偏差）：

| 精度档 | 最大最终绝对偏差 | 主导来源 |
| --- | ---: | --- |
| `float64` | `0.0` | `none` |
| `float32` | `1.284322177497188e-07` | `dequantization` |
| `qint16` | `1.164795876654523e-03` | `dequantization` |
| `qint8` | `3.0544295341591976e-02` | `dequantization` |

其他样例：

- `mean + exclude`：`1.8749161494149735e-02`
- `mean + zero_weight`：`1.5272147670795988e-02`
- `gather_axis=1`：`1.164795876654523e-03`
- `concat`：`1.9573551681219215e-02`

`tests/test_snapshots_manifest_report.py` 会连续运行 CLI 两次并比较报告字节，本轮通过。

更完整的公式、状态机与测试覆盖见 `TECHNICAL.md`。
