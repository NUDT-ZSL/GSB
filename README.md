# 分块 Softmax / LayerNorm 确定性组件

面向 Transformer 前向计算的本地可集成实现。项目只依赖 Python 3.10+ 标准库与
NumPy，提供确定性样本生成、两类算子的统一分块执行入口、严格偏差谱、四阶段异常
注入与降级、资源上限、版本化快照、跨模块自检和 Windows 可运行示例。

## 快速开始

在项目根目录执行：

```bash
python scripts/generate_samples.py --output-dir samples
python -m unittest discover -s tests -v
python -m blockops demo --data-dir samples --output-dir build/demo
```

Windows 命令行可直接运行：

```bat
generate_samples.bat
run_tests.bat
run_demo.bat
```

演示退出码规则：结构和自检全部通过时返回 `0`；报告另行如实记录故意注入导致的
`failed`/`degraded` 任务。示例报告位于：

- `build/demo/demo_report.json`：完整 JSON 报告，含分块统计、偏差谱、注入记录、
  降级链、业务码、自检和重跑哈希。
- `build/demo/snapshots.json`：最终版本化快照。
- `build/demo/outputs/*.npy`：正常、降级和注入任务的实际输出。
- `build/demo/demo_report.sha256`：报告文件的 SHA-256。

查询快照：

```bash
python -m blockops snapshot-query --file build/demo/snapshots.json --category tolerance_config
python -m blockops snapshot-query --file build/demo/snapshots.json --category "job_statistics:softmax_2d_f64"
```

## 目录

```text
blockops/
  operators.py   统一入口、Softmax/LayerNorm 分块执行、写回、容差裁决
  planner.py     字段校验、块计划、资源预检
  tolerance.py   float64 偏差谱
  selfcheck.py   跨模块一致性自检
  snapshot.py    校验和快照、整体替换、回退/损坏检测
  demo.py        固定样本演示与报告
  cli.py         `python -m blockops` 入口
scripts/generate_samples.py
samples/tasks.jsonl
samples/tensors/*.npy
tests/
```

## 样本与任务格式

`scripts/generate_samples.py` 使用任务中的 `seed` 创建
`numpy.random.default_rng`，生成标准正态特征，固定保留三位小数后转为指定 dtype。
Softmax 样本会显式构造跨块的同值最大值，用于验证“同值并列取最小元素索引”。
`.npy` 和 JSONL 均按固定字节格式写入。

`samples/tasks.jsonl` 每行字段严格为：

```json
{
  "job_id": "softmax_2d_f64",
  "op_kind": "softmax",
  "input_shape": [3, 10],
  "block_size": 4,
  "axis": -1,
  "eps": 1e-09,
  "dtype": "float64",
  "seed": 1101,
  "norm_mode": "stable"
}
```

当前样本覆盖二维/三维、首轴/中间轴/末轴、`float32`/`float64`、population 和
unbiased LayerNorm。

## 组件接口

```python
import numpy as np
from blockops import (
    ExecutionOptions, FaultParameters, ResourceLimits, ToleranceConfig,
    OperatorTask, execute_job, softmax, layernorm,
    injection_diagnostics, query_snapshot,
)

result = softmax(values, axis=-1, block_size=4, job_id="s1")
result = layernorm(values, axis=-1, block_size=4, eps=1e-5, job_id="l1")

task = OperatorTask.from_mapping(task_dict)
result = execute_job(task, values, ExecutionOptions())

print(result.output)
print(result.business_codes())
print(result.plan)
print(result.tolerance_report)
print(result.degradations)
print(result.self_check)
print(injection_diagnostics(result))
```

`dtype` 用于校验磁盘/输入张量类型。正常路径会把 `float32` 输入提升到
`float64` 计算并输出 `float64`，因此默认 `1e-12` 绝对/相对容差不会被存储精度
掩盖。只有显式资源或异常降级时才进入 `float32` 路径，并在结果中标记
`compute_dtype`；此路径若超过严格容差，会如实产生 `E_TOLERANCE_EXCEEDED`。

## 分块与数值路径

块边界是沿 `axis` 的半开元素区间 `[start, end)`。每个块记录完整的
`element_indices`、逐行元素索引、统计量和输出值，避免隐式切片或平台相关边界。

### Softmax

1. 沿 `axis` 逐块扫描最大值，块内用首个最大值索引裁决并列。
2. 跨块用更小元素索引裁决同值最大值。
3. 每块计算 `exp(x - new_max)`，并以
   `denom = old_denom * exp(old_max - new_max) + block_sum` 在线更新分母。
4. 扫描结束后用最终最大值和最终分母归一化，再按显式索引写回。

### LayerNorm

1. 每块计算计数、均值、M2（平方偏差和）。
2. 用 Chan/Welford 合并公式按块序号升序跨块归约，避免直接累加平方产生明显抵消。
3. population 使用 `M2/n`，unbiased 使用 `M2/(n-1)`。
4. 输出 `(x - mean) / sqrt(variance + eps)`。

一次性参考实现始终在 `float64` 下整段计算。偏差谱逐元素记录：

- 最大绝对偏差及其扁平索引；
- 最大相对偏差及其扁平索引，零参考值使用可配置的 `relative_floor`；
- 所有越界元素的扁平索引、多维索引、分块值、参考值、绝对/相对偏差；
- 偏差谱内容校验和，供分块明细重算比对。

默认 `abs_tol=1e-12`、`rel_tol=1e-12`。容差通过公开配置提供，不会为了通过比较
而动态放宽。

## 四阶段注入与降级

注入完全由输入任务或公开参数触发，不修改源码。每条诊断包含 `stage`、业务码、
`job_id`、字段、参数摘要及 `params_digest`。

| 阶段 | 触发 | 业务码 | 下游策略 |
|---|---|---|---|
| `input_validation` | LayerNorm `eps <= 0` 或非法 | `E_EPS_INVALID` | `EPS_FLOOR`，使用配置的 `eps_floor` |
| `block_statistics` | `block_size <= 0` | `E_BLOCK_SIZE_INVALID` | `BLOCK_SIZE_FALLBACK`，显式块大小降为 1 |
| `normalization_accumulation` | `stat_overflow_scale` 使统计量超过安全阈值 | `E_STAT_OVERFLOW` | 保留原始块统计，`FLOAT32_NORMALIZATION` 重算归一化 |
| `output_writeback` | `write_index_offset` 产生越界或非完整索引集 | `E_WRITE_INDEX` | `SAFE_WRITEBACK`，丢弃偏移并按原显式索引写回 |

每个降级记录都包含 `category`、`stage`、`trigger_code`、`job_id`、
`affected_blocks` 和可读策略。硬校验失败会记录 `ABORT_JOB`，不会继续写输出。
任何未归属阶段的 Python 异常都会进入 `unattributed_errors` 并产生
`E_UNATTRIBUTED`，不会被静默捕获。

演示中的归一化注入会故意得到 float32 降级结果，并在严格 `1e-12` 下同时报告
`E_TOLERANCE_EXCEEDED`。这是预期行为：既保留上游注入记录，也不隐藏降级造成的
真实偏差。

## 资源上限

`ResourceLimits` 提供：

- `max_blocks`：分块数量硬上限，超过时返回 `E_RESOURCE_LIMIT` 并 `ABORT_JOB`；
- `max_block_elements`：单块驻留元素上限，超限时在开始执行前缩小块并记录
  `BLOCK_SHRINK`；
- `max_stat_bytes`：按 `outer_count * block_count * 每块统计浮点数 * 字节宽度`
  估算；可通过 `FLOAT32_STATS` 降级，否则返回 `E_RESOURCE_LIMIT`；
- `allow_block_shrink` / `allow_float32_stats`：关闭对应降级时直接拒绝。

所有资源判断均在写出任何块之前完成。降级只改变后续执行路径，不会修改已经生成的
块数值。

## 版本化快照

`SnapshotStore` 以类别维护“当前且唯一”的快照。快照包含类别、单调版本号、内容
校验和及完整内容。类别包括：

- `block_plan`；
- `tolerance_config`；
- `job_statistics:<job_id>`。

规则：

- 新版本整体替换旧内容，绝不字典合并或增量修补；
- 低版本提交返回 `E_SNAPSHOT_ROLLBACK`，当前快照保持不变；
- 内容与校验和不符返回 `E_SNAPSHOT_CORRUPT`，错误中带
  `snapshot_management:<category>` 阶段、类别与版本号；
- 同版本不同校验和同样按损坏冲突拒绝；
- JSON 持久化可重新加载并查询；损坏文件在加载时检测。

## 跨模块自检

每次产生输出的执行都会运行 `cross_module_self_check`：

1. 校验四阶段处理的元素索引集合完全一致且覆盖全部元素；
2. 用块明细的 `element_indices`/`output_values` 重建输出；
3. 重算偏差谱并比对校验和；
4. 校验降级触发码存在、阶段合法、关联块 ID 真实存在。

失败时整条结果追加 `E_INCONSISTENT`，并在 `failed_categories` 中指出具体类别。

## 错误码

`E_OP_UNKNOWN`、`E_AXIS_INVALID`、`E_SHAPE_INVALID`、
`E_DTYPE_UNSUPPORTED`、`E_BLOCK_SIZE_INVALID`、`E_EPS_INVALID`、
`E_NORM_MODE_INVALID`、`E_INPUT_NONFINITE`、`E_RESOURCE_LIMIT`、
`E_STAT_OVERFLOW`、`E_WRITE_INDEX`、`E_TOLERANCE_EXCEEDED`、
`E_INCONSISTENT`、`E_SNAPSHOT_ROLLBACK`、`E_SNAPSHOT_CORRUPT`、
`E_SNAPSHOT_UNKNOWN`、`E_UNATTRIBUTED`。

四类必需统一校验错误均携带 `job_id` 和字段名：
`op_kind`、`axis`、`input_shape`、`dtype`。

## 实际验证结果

本机验证环境：Python `3.10.12`，NumPy `1.26.4`。

```text
python -m unittest discover -s tests -v
Ran 24 tests in 2.131s
OK
```

完整演示结果：

- `verification.passed = true`
- 四阶段记录齐全：`input_validation`、`block_statistics`、
  `normalization_accumulation`、`output_writeback`
- 四类校验码齐全：`E_OP_UNKNOWN`、`E_AXIS_INVALID`、`E_SHAPE_INVALID`、
  `E_DTYPE_UNSUPPORTED`
- 所有有输出任务的自检均通过，未归因异常数为 `0`
- CLI 两次独立运行后报告、快照和全部 `.npy` 文件逐字节一致
- 报告核心校验和：`a7fb5a265fa3a5c75fdf85db1ace0b8798df1e0833399135c190d49749f586e5`
- 报告文件 SHA-256：`2718ece9904cf30794f8ae702427eb7a3d1bc20d268e234feb40cf5165dc7096`
- 快照文件 SHA-256：`d9b54f11910fc40e93e72debc638d6acc5220b43bc8ac6bee240d273fa13d67d`

字节复现以相同 Python/NumPy 运行环境为前提；报告内部还分别记录了两次执行的
分块统计、输出和参考实现哈希。
