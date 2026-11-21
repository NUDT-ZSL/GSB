# chunkdiag

面向二维卷积与最近邻/双线性/双三次插值的本地确定性诊断组件。每次任务都按以下链路执行：

1. 参数校验
2. 共享块网格划分与资源自适应选路
3. 分块数值计算
4. 与独立 float64 逐元素参考实现比对、偏差谱归因和裁决

运行期只依赖 Python 3.10+ 与 NumPy，不需要容器、网络、GPU、外部数据或模型。

## 快速开始

在 Windows PowerShell 中：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m chunkdiag demo --out-dir .\artifacts\demo
```

也可以安装为本地可编辑组件：

```powershell
python -m pip install -e .
chunkdiag demo --out-dir .\artifacts\demo
```

演示命令会创建：

- `demo_tasks.jsonl`：9 个本地确定性任务，覆盖四类正常算子、四阶段注入和一次预算降级。
- `report.json`：聚合报告，包含策略记录、偏差谱、注入记录、降级链路、业务码和可复现哈希。

运行已有任务清单：

```powershell
python -m chunkdiag run --tasks .\artifacts\demo\demo_tasks.jsonl --out .\artifacts\demo\report.json
```

仅生成演示输入：

```powershell
python -m chunkdiag generate --out .\artifacts\demo
```

## 任务格式

任务清单是 JSONL，每行一个对象。示例：

```json
{
  "job_id": "conv-001",
  "op_kind": "conv2d",
  "input_shape": [1, 8, 9, 2],
  "kernel_shape": [3, 2, 3, 3],
  "stride": [1, 1],
  "padding": 1,
  "seed": 1101,
  "weight_seed": 2201,
  "dtype": "float64",
  "block_policy": {
    "block_size": [3, 4],
    "candidates": [[3, 4], [2, 2]],
    "selection": "fixed"
  },
  "tolerance": {"atol": 1e-12, "rtol": 1e-9},
  "resource_limits": {
    "max_resident_bytes": 268435456,
    "max_blocks": 1000000,
    "max_live_intermediates": 3,
    "allow_smaller_blocks": true,
    "allow_dtype_downgrade": true
  }
}
```

字段约定：

| 字段 | 说明 |
| --- | --- |
| `job_id` | 非空任务标识，所有错误、注入和降级记录都会携带 |
| `op_kind` | `conv2d`、`interpolate`，也接受 `nearest`、`bilinear`、`bicubic` 直接指定插值模式 |
| `input_shape` | 激活值形状 `[N, H, W, C]`，必须为四维正整数 |
| `kernel_shape` | 卷积核形状 `[O, C, KH, KW]`；`C` 必须等于 `input_shape[-1]` |
| `stride` | 正整数或 `[stride_h, stride_w]`；插值路径会解析并记录，但坐标映射只由 `scale` 决定 |
| `padding` | `valid`、`same`（仅卷积）、整数、`[pad_h, pad_w]` 或 `[top, bottom, left, right]` |
| `scale` | 插值缩放，单个正数或 `[scale_h, scale_w]` |
| `interp_mode` | `nearest`、`bilinear`、`bicubic` |
| `seed` | 输入特征图生成种子 |
| `weight_seed` | 卷积权重的独立生成种子；卷积默认 `seed + 1` |
| `dtype` | 执行精度，支持 `float64` 和 `float32` |
| `block_policy` | 块尺寸、候选列表和选择策略 |
| `tolerance` | `atol`、`rtol`，比对阶段要求为有限正数 |
| `resource_limits` | 驻留内存、块数、同时中间结果数与降级许可 |
| `injection` | 按阶段配置的故障注入，见下文 |
| `max_violation_records` | 报告中最多输出多少条超容差元素位置，默认 128 |
| `spectrum_detail_limit` | 每级偏差明细最多保存的值数量，默认 200000 |

卷积输出：

```text
out_h = floor((H + pad_top + pad_bottom - KH) / stride_h) + 1
out_w = floor((W + pad_left + pad_right - KW) / stride_w) + 1
output_shape = [N, O, out_h, out_w]
```

插值输出：

```text
out_h = floor((H + pad_top + pad_bottom) * scale_h)
out_w = floor((W + pad_left + pad_right) * scale_w)
output_shape = [N, out_h, out_w, C]
```

显式声明的 `padding` 会先在输入周围补零；卷积每个方向的单侧 padding 不得超过 `K - 1`，插值不得超过输入对应维度减 1。越界声明返回 `E_PARAM_INVALID`。

## 数值语义

- 输入和权重由 `numpy.random.default_rng(seed)` / `default_rng(weight_seed)` 确定性生成，初始类型为 `float64`。
- 四类参考实现分别使用独立 Python 循环逐元素计算，不调用分块执行路径：`reference_conv2d`、`reference_nearest`、`reference_bilinear`、`reference_bicubic`。
- 参考输出始终为 `float64`，并记录 C-contiguous 原始字节的 SHA-256。
- 卷积使用显式零填充；插值先把邻点索引夹紧到补零后的图像边界。
- 插值坐标采用 `align_corners=False`：`src = (dst + 0.5) / scale - 0.5`。
- 最近邻索引为 `floor(src + 0.5)` 后夹紧；双线性使用四个夹紧邻点；双三次使用 Keys 核 `a=-0.75` 的 4x4 夹紧邻点。
- 分块路径与参考路径使用不同的累加组织方式，因此诊断谱可以观察真实的低阶浮点差异。

## 分块与资源策略

卷积和三种插值共用 `make_blocks` 生成的输出空间块网格。块只覆盖输出范围，边缘块显式裁剪，不会隐式写出输出边界；边界处理由任务类型声明：

- 卷积：源图像外按零处理。
- 插值：补零后的邻点索引夹紧。

资源估计包含输入、权重、输出、float64 参考输出和按 `max_live_intermediates` 计算的块工作区。候选块从任务声明、完整输出块、逐级二分块和 `1x1` 块中生成。若 float64 候选均超限：

1. 在允许时切换到 `float32`。
2. 选择满足内存与块数限制的块尺寸。
3. 仍不可行时返回 `E_RESOURCE_LIMIT`。

float32 路径仍与 float64 参考比较，并使用已记录的精度下限：`atol >= 1e-5`、`rtol >= 1e-4`。这不会放宽“必须与参考同快照比对”的口径。

## 四阶段注入

通过任务的 `injection` 对象触发，不需要修改源码：

```json
{
  "injection": {
    "parameter_validation": "nonfinite_weight",
    "block_partition": "zero_block_size",
    "numeric_compute": "nonfinite_output",
    "deviation_compare": "nonpositive_tolerance"
  }
}
```

| 阶段 | 触发值 | 业务码 | 降级动作 |
| --- | --- | --- | --- |
| `parameter_validation` | `nonfinite_weight` | `E_PARAM_INVALID` | `REJECT_INVALID_INPUT`，下游标记 `skipped` |
| `parameter_validation` | `padding_overflow` | `E_PARAM_INVALID` | `REJECT_INVALID_INPUT` |
| `block_partition` | `zero_block_size` | `E_PARAM_INVALID` | `SINGLE_BLOCK_FALLBACK` |
| `numeric_compute` | `nonfinite_output` | `E_NUMERIC_NONFINITE` | `REFERENCE_FALLBACK` |
| `deviation_compare` | `nonpositive_tolerance` | `E_PARAM_INVALID` | `DEFAULT_TOLERANCE` |

非有限输入/权重、零块尺寸、非法容差等自然异常输入也会走到相同阶段并产生结构化记录。所有注入记录都包含 `job_id`、`stage`、`code`、`field`、`trigger` 和触发模式。

## 业务码与降级类别

业务码：

- `E_SHAPE_INVALID`：形状维度、通道匹配或输出尺寸非法。
- `E_PARAM_INVALID`：stride、padding、scale、tolerance、块策略等参数非法。
- `E_OP_UNSUPPORTED`：未知 `op_kind` 或 `interp_mode`。
- `E_DTYPE_UNSUPPORTED`：不支持的 `dtype`。
- `E_RESOURCE_LIMIT`：资源上限无法通过降级满足。
- `E_NUMERIC_NONFINITE`：数值阶段输出含非有限值。
- `E_NUMERIC_MISMATCH`：输出超过与 float64 参考比较的容差。
- `E_INTERNAL_UNATTRIBUTED`：未归因异常；原始类型、消息和 traceback 汇总在 `unattributed_exceptions`。

降级类别：

- `REJECT_INVALID_INPUT`
- `REJECT_RESOURCE_LIMIT`
- `REJECT_PARTITION_FAILURE`
- `SINGLE_BLOCK_FALLBACK`
- `REFERENCE_FALLBACK`
- `DEFAULT_TOLERANCE`
- `DTYPE_DOWNGRADE`
- `SMALLER_BLOCK`
- `LARGER_BLOCK`

每个 `degradation_chain` 项都包含 `category`、`upstream_code`、`upstream_field`、`stage` 和动作说明。

## 偏差谱与裁决

`deviation_spectrum.sources` 中每一级都包含：

- `signal_type`：实际观测区域或反事实干预探针。
- `quantiles`：`p50`、`p90`、`p95`、`p99`。
- `max_abs`、`mean_abs`、`rms_abs`、`sum_abs`。
- `error_amplification_ratio`：该级 `p95` 除以 chunk-vs-reference 的观测 `p95`。
- `details.values`：用于重算统计量的明细数组。

卷积归因级为 `block_boundary_oob`、`accumulation_order`、`compensated_sum`；插值归因级为 `weight_calculation`、`oob_neighbor_selection`、`accumulation_order`。

`adjudicate_deviations` 会从 `details.values` 重算分位数、求和并重新选出主导来源；结果写入 `report["adjudication"]`。若明细被截断或被篡改，`verified` 为 `false` 并列出不匹配项。

比对结果同时记录最大绝对偏差位置、最大相对偏差位置、超容差数量，以及带轴名的超容差元素位置。相对误差分母使用 `max(abs(reference), 1e-12)`。

## 公共 API

```python
from chunkdiag import (
    adjudicate_deviations,
    conv2d,
    inject_diagnose,
    interpolate,
)

result = conv2d(task)
print(result.status)
print(result.report["comparison"]["max_abs_error"])
print(result.output)            # NumPy 输出数组
print(result.reference_output)  # 独立 float64 参考数组

adjudication = adjudicate_deviations(result)

interp_result = interpolate(interp_task, mode="bicubic")
generic_result = inject_diagnose(task)
```

`result.report` 可直接写入 JSON；`result.output` 和 `result.reference_output` 供本地集成使用。`ExecutionResult` 还提供 `job_id`、`status`、`primary_code` 和 `to_dict()`。

## 报告结构

关键顶层字段：

- `effective_config`：规范化参数与最终生效精度。
- `inputs`：种子生成数组的形状、类型和 SHA-256。
- `policy_record`：共享块网格、边界规则、候选与选择依据。
- `resource`：驻留内存、块数、工作区估计。
- `stage_records`：四个阶段的 `ok`、`degraded`、`error`、`skipped` 或未归因状态。
- `business_codes`：结构化业务码与恢复状态。
- `injection_records`：阶段、触发值和业务码。
- `degradation_chain`：降级类别、上游业务码和动作。
- `reference` / `execution`：参考与执行输出形状、类型、SHA-256 和输出来源。
- `comparison`：容差、最大偏差、超容差位置。
- `deviation_spectrum` / `adjudication`：偏差谱和独立重算裁决。
- `unattributed_exceptions`：任何未归因异常，不会静默吞掉。
- `reproducibility`：任务指纹、输入、参考输出和最终输出哈希。

报告不写入时间戳、随机 ID 或机器路径；同一任务在同一 NumPy 环境下重复运行，参考字节、执行字节和规范 JSON 内容均一致。

## 实际验证

运行：

```powershell
python -m unittest discover -s tests -v
python -m chunkdiag demo --out-dir .\artifacts\demo
```

本工作区实测结果：

- `23` 个 `unittest` 全部通过。
- 演示 `9` 个任务：`4 ok`、`4 degraded`、`1 error`。
- 四类正常算子均与 float64 参考在容差内一致；示例最大绝对偏差约为：卷积 `3.55e-15`、最近邻 `0`、双线性 `4.44e-16`、双三次 `8.88e-16`。
- 四阶段注入记录齐全，降级类别包含 `SINGLE_BLOCK_FALLBACK`、`REFERENCE_FALLBACK` 和 `DEFAULT_TOLERANCE`。
- 预算演示同时触发 `DTYPE_DOWNGRADE` 与 `SMALLER_BLOCK`，最终 float32 输出仍与 float64 参考在声明容差内一致。
- CLI 连续两次在独立目录生成的 `report.json` 字节一致。

验证生成的输入与报告位于 `artifacts/demo/`。
