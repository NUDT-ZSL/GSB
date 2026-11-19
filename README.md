# sparse-precision

面向 SpMM（稀疏 \(\times\) 稠密）与 SpMSpM/SpSpMM（稀疏 \(\times\) 稀疏）的可集成 Python 算子组件。重点不是追求峰值吞吐，而是让**分块策略、浮点舍入、误差谱、资源预算和业务错误码**处在同一条可复现、可审计的链路上。

组件不依赖容器、网络或外部服务，输入是本地 JSON 元数据，CSR 矩阵和 `float64` 稠密参考结果均由同一 `seed` 与元数据确定性重建。

## 能力概览

- 统一入口 `spmm`、`spspmm`、`execute_case`，支持单矩阵及 `batch_size` 批次语义。
- 严格校验非法形状、不支持的 dtype、CSR 行指针非单调/索引越界/`nnz` 声明不符。
- 自适应行分块与 K 分块、直接累加或 COO 聚合中间路径、Kahan 补偿求和、跨块归约。
- 以未分块、未补偿的 `float64` 稠密乘为唯一真值，输出误差分位数、最大偏差位置、最大相对偏差、误差放大比和逐级归因。
- 显式资源裁决：内存、分块数量和聚合缓冲上限；可行时降级并记录类别，不可行时返回 `E_RESOURCE_LIMIT`。
- 四类一致性自检：SpMM/退化 SpSpMM、转置语义、CSR/CSC 视图、部分和重算。
- 命令行可生成样本、批量运行 JSON 报告，并演示异常输入、预算降级、资源失败和零容差失败。

## 环境与安装

要求 Python 3.10+、NumPy 1.26+、SciPy 1.11+。Windows 本地可直接导入：

```bash
python -m pip install -r requirements.txt
python -c "import numpy, scipy, sparse_precision; print('ok')"
```

也可安装为本地包：

```bash
python -m pip install -e .
sparse-precision demo
```

不要求 Docker、数据库、消息队列或网络访问。

## 快速运行

```bash
# 1. 生成确定性 CSR 元数据与参考快照
python -m sparse_precision.cli generate \
  --out artifacts/samples \
  --seed 20261119

# 2. 运行全部正常案例
python -m sparse_precision.cli run \
  --samples artifacts/samples \
  --out artifacts/report.json

# 3. 生成完整演示，包含异常输入、预算降级、资源失败与容差失败
python -m sparse_precision.cli demo \
  --samples artifacts/samples \
  --out artifacts/demo_report.json \
  --seed 20261119
```

`demo` 的预期检查全部为 `true`：

```json
{
  "invalid_shape_returns_E_SHAPE_INVALID": true,
  "over_budget_plan_is_marked_degraded": true,
  "impossible_budget_returns_E_RESOURCE_LIMIT": true,
  "tight_zero_tolerance_returns_E_TOLERANCE_EXCEEDED": true
}
```

## 领域 API

```python
import json

from sparse_precision import (
    OperatorConfig,
    decide_strategy,
    diagnose_error_spectrum,
    execute_case,
    spmm,
    spspmm,
)

metadata = json.load(open("artifacts/samples/case_spmm_dense_7.json"))

# 由 seed/metadata 重建 A、D 与稠密参考，执行并返回完整报告
report = spmm(metadata, config=OperatorConfig(atol=1e-12, rtol=1e-10))

# 已有 SciPy CSR/稠密数组时可直接传入
# report = spmm(metadata, A, D, config=config)
# report = spspmm(metadata, A, B, config=config)

# 仅做策略裁决
strategy = decide_strategy(metadata, config=OperatorConfig(max_blocks=128))

# 对已知真值和结果做误差谱诊断
spectrum = diagnose_error_spectrum(reference, actual, config)
```

主要入口：

- `execute_case(metadata, a=None, b=None, config=None, reference=None)`
- `spmm(metadata, a=None, dense_b=None, config=None)`
- `spspmm(metadata, a=None, b=None, config=None)`
- `decide_strategy(metadata_or_CaseSpec, operands=None, config=None)`
- `diagnose_error_spectrum(reference, actual, config=None, stage_metrics=None)`

## 元数据契约

单个案例文件示例：

```json
{
  "case_id": "spmm_dense_7",
  "kind": "spmm",
  "shape_mnk": [72, 96, 7],
  "nnz_a": 520,
  "nnz_b": 672,
  "dtype": "float64",
  "seed": 20261220,
  "block_candidates": [8, 12, 16, 24, 32, 48, 72],
  "b_storage": "dense",
  "generation": {
    "a_stream": 1,
    "d_stream": 3,
    "a_value_scale": 1.0,
    "b_value_scale": 0.75
  },
  "checksums": {
    "a": ["..."],
    "b": ["..."],
    "reference": "..."
  }
}
```

语义：

- `shape_mnk` 为 `[m, k, n]`，或带批次的 `[batch, m, k, n]`；也允许 `[m, k, n] + batch_size`。
- `nnz_a`、稀疏 B 的 `nnz_b` 都是**所有批次的总和**。
- `kind="spmm"` 时 B 为稠密操作数，`nnz_b` 表示稠密元素总数 `batch*k*n`。
- `kind="spspmm"` 时 B 必须为 CSR，`nnz_b` 表示真实非零元数。
- 仅支持 `float32`、`float64`；计算和输出统一提升为 `float64`。
- 固定随机流：`numpy.random.SeedSequence([seed, stream, batch])` + `PCG64`。
- CSR 坐标按行主序排序，`indptr/indices/data` 通过同一随机序列重建。
- `checksums` 可选；存在时会校验 A、B/稠密 B、参考矩阵，防止 seed 或生成参数被静默修改。

`generate` 同时写出 `reference_<case_id>.npy` 审计快照。运行时不会把该文件当作不可验证真值：组件始终从 seed/metadata 重建 `float64` 稠密参考，并校验元数据中的参考指纹。API 的 `reference=` 参数仅用于传入审计快照；它必须与重建参考逐位一致，否则返回 `E_CSR_INVALID`，不会用不可重建信息替代真值。

## 执行与真值口径

参考实现：

```python
reference = A.astype(float64).toarray() @ B.astype(float64).toarray()
```

参考路径不分块、不使用补偿求和。所有正常、降级、聚合和直接路径都在**同一个完整参考矩阵**下比较。

分块路径：

1. 行维按 `row_block_size` 分块，K 维按 `k_block_size` 分块；均使用 `ceil` 覆盖，不要求整除。
2. 每个 `(row_block, k_block)` 计算一个部分和。
3. 稀疏 B 且 COO 贡献缓冲小于稠密行块工作区时，可选择聚合中间路径；否则使用 CSR 行直接累加。
4. 同一输出行的 K 分块部分和按选定顺序做跨块归约。
5. `compensated_sum=true` 时，局部累加和跨块归约使用 Kahan 补偿；未补偿路径可在 K 分块较多时使用 pairwise 归约。

执行后会核对分块网格覆盖的行、K 和 A/B 非零元数量，避免切片遗漏。

## 策略决策与资源取舍

候选由 JSON 的 `block_candidates` 给出。每个候选记录：

- 行块尺寸、K 块尺寸、行块数、K 块数、总块数；
- 每块平均 nnz、聚合贡献数、稠密工作区、部分和缓冲、输出缓冲、峰值内存估计；
- 精度风险估计、是否走聚合路径、累加顺序、资源违规项；
- 行/K 覆盖证明。

默认显式优先级：

1. 形状与 CSR 正确性；
2. 资源硬上限；
3. 容差判定口径不可改变；
4. 最小精度风险；
5. 最小估计峰值内存；
6. 更大行块作为确定性平局规则。

因此更小行块意味着更短的局部稀疏累加，但会增加部分和/分块数量；更大行块减少总中间缓冲，但局部累加路径更长。冲突时，实际选择会与“无预算限制的 precision-first 理想方案”比较，并把差异写入：

```json
{
  "degradation": {
    "degraded": true,
    "category": "larger_row_block",
    "ideal_plan": {"row_block_size": 16, "use_aggregation_path": true},
    "selected_plan": {"row_block_size": 32, "use_aggregation_path": false},
    "semantics_unchanged": true,
    "tolerance_unchanged": true
  }
}
```

降级类别包括 `smaller_row_block`、`larger_row_block`、`aggregation_disabled`、`resource_replanned`。没有可行方案时返回 `E_RESOURCE_LIMIT`，并在 `details.limits`/`details.violations` 中给出每个候选的越限值。若配置 `resource_policy="error"`，即使存在可行降级方案也会返回 `E_RESOURCE_LIMIT`，并记录理想方案与实际可行方案。

## 误差谱与归因

`diagnostics` 包括：

- `abs_error_quantiles`: `p0/p50/p90/p99/p100`；
- `max_abs_error_position`: 批次、行、列、参考值、实际值、绝对偏差；
- `max_relative_error` 与位置（仅对非零参考元素定义）；
- `abs_error_mean`、`abs_error_rms`、`error_amplification_ratio`；
- `worst_violations`: 超容差的最坏元素及 `allowed_error`、`excess`；
- `stage_attribution.metrics`: 局部稀疏求和、跨块归约、行分块三级指标。

容差公式固定为：

```text
abs(actual - reference) <= atol + rtol * abs(reference)
```

不会因为路径降级而放宽容差。任何元素越界即返回 `E_TOLERANCE_EXCEEDED`，顶层 `details` 同时给出越界数量和最坏元素。

误差放大比定义：

```text
max_abs_error / (float64_eps * max(1, max_abs(reference)))
```

局部与跨块指标均由可重建的稠密分块参考测量。行块本身是输出行的不相交划分，不会让两个块共同写入同一元素，因此独立行分块效应按构造为 0；其端到端块级包络另存为 `stage_attribution.envelope`，不重复计入主导来源。

## 一致性自检

每次正常执行默认运行：

- `spmm_vs_degenerate_spspmm`: 同一 A 上稠密宽度 1 与稀疏 B 路径互核。
- `transpose_semantics`: 校验 `(A@B)^T == B^T@A^T`。
- `row_major_column_major_view`: CSR/CSC 往返视图结果一致。
- `partial_reduction_recompute`: 由记录的部分和重算每个块及最终矩阵。

失败返回 `E_INCONSISTENT`，`details.failed_categories` 指出类别，顶层 `case_id` 保留案例身份。

## 业务码

| 业务码 | 触发条件 |
|---|---|
| `OK` | 校验、资源、容差与一致性检查全部通过 |
| `E_SHAPE_INVALID` | 非法维度、批次不匹配、操作数形状不符合元数据 |
| `E_DTYPE_UNSUPPORTED` | 元数据或实际操作数 dtype 不在 `float32/float64` |
| `E_CSR_INVALID` | 行指针非单调、索引越界、数组长度不一致、`nnz` 声明不符、NaN/Inf、指纹不符 |
| `E_TOLERANCE_EXCEEDED` | 至少一个元素超过固定容差 |
| `E_RESOURCE_LIMIT` | 内存、分块数或聚合缓冲无可行策略/实际执行超限 |
| `E_INCONSISTENT` | 非零元覆盖或跨模式自检失败 |
| `E_METADATA_INVALID` | 元数据字段缺失、类型或格式非法 |
| `E_CONFIG_INVALID` | 配置字段未知或配置组合非法 |

所有错误载荷至少包含 `code`、`case_id`、`phase`、`field`、`message`；资源、容差和一致性错误还带可审计 `details`。

## 项目结构

```text
sparse_precision/
  api.py            统一入口、业务码裁决
  validation.py     元数据、CSR、dtype、nnz 校验
  generator.py      seed 驱动的确定性 CSR/稠密矩阵重建与参考
  strategy.py       代价模型、确定性选择、预算降级
  kernel.py         分块执行、Kahan、聚合路径、部分和快照
  diagnostics.py    误差谱、容差裁决、误差归因
  consistency.py    跨模式一致性自检
  sample_data.py    本地样本定义与参考快照
  cli.py            generate/run/demo 离线命令
tests/              28 个标准库 unittest 用例
artifacts/          生成的样本、普通报告与完整演示报告
```

## 实际验证结果

环境：Python 3.10.12、NumPy 2.2.6、SciPy 1.15.3。

`python -m unittest discover -s tests -v`：**28/28 通过**。

`artifacts/report.json` 的正常案例摘要：

| case_id | 批量 | 行/K 块 | 聚合 | 分块数 | 最大绝对偏差 | 最大相对偏差 | 放大比 | 主导来源 |
|---|---:|---:|:---:|---:|---:|---:|---:|---|
| `spmm_dense_7` | 1 | 24/24 | 否 | 12 | 1.776e-15 | 5.804e-13 | 0.912 | 局部稀疏求和 |
| `spmm_vector_1` | 1 | 32/32 | 否 | 8 | 4.441e-16 | 2.050e-15 | 0.870 | 局部稀疏求和 |
| `spspmm_aggregated_32` | 1 | 16/16 | 是 | 20 | 2.220e-16 | 2.160e-15 | 0.528 | 局部稀疏求和 |
| `spspmm_balanced_40` | 1 | 24/24 | 否 | 16 | 4.441e-16 | 4.139e-13 | 0.381 | 局部稀疏求和 |
| `spspmm_batched_24` | 2 | 16/16 | 否 | 24 | 4.441e-16 | 5.069e-15 | 0.543 | 局部稀疏求和 |
| `spspmm_float32_mini` | 1 | 14/14 | 否 | 12 | 5.551e-17 | 4.341e-16 | 0.157 | 局部稀疏求和 |

六个案例均返回 `OK`，四个一致性检查全部通过；`spspmm_aggregated_32` 实际执行了 COO 聚合路径（1368 个贡献项）。所有 A/B 非零元均被分块网格覆盖。
`spspmm_float32_mini` 的生成参数包含 1% 次正规数缩放样本，用于覆盖下溢附近的舍入路径。

同一命令连续运行两次，`report.json` 与 `demo_report.json` 的 SHA-256 分别保持为
`61379c9c007e46a95b33ddbbd71b0a6d6522511d9a370ae066e516cbe8ac0036`、
`f6144c03eadeb081c914a2b7bda77f0428a8e01f6568038647fe8a794d3c489b`，确认策略记录和报告无时间戳类漂移。

`artifacts/demo_report.json` 的异常与预算演示：

- 注入 `shape_mnk[0]=0`：`E_SHAPE_INVALID`，`field="shape_mnk"`，`case_id="demo_invalid_shape"`。
- 预算降级：理想方案行块 16、聚合开启；在 `max_blocks=8`、`max_aggregation_bytes=1` 下选择行块 32、聚合关闭，返回 `OK`，`degradation.category="larger_row_block"`，容差口径不变。
- 不可行预算：`E_RESOURCE_LIMIT`，阶段 `strategy`，列出每个候选的 `max_blocks`、`max_memory_bytes` 越限情况。
- 零容差探针：未补偿、逆序累加路径产生 35 个越界元素，返回 `E_TOLERANCE_EXCEEDED` 并列出最坏元素。

## 已知边界

- 稠密真值参考需要构造 `A`、`B` 的稠密视图，适用于本任务要求的可审计样本规模，不应直接用于超大生产矩阵。
- 当前支持浮点 `float32/float64`；整数和复数 dtype 明确拒绝。
- 随机结构使用 NumPy `PCG64/SeedSequence` 与 `choice(replace=False)`；同一 Python/NumPy 环境下可确定性重建，跨大版本 NumPy 升级时应保留元数据与指纹做审计。
- 资源控制依据显式代价模型和实际部分和容量检查，不读取操作系统实时可用内存。
