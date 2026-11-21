# DimDiag 技术说明

## 1. 设计边界

DimDiag 是单机、离线、确定性的诊断工作台。它不追求通用机器学习平台能力，而强调以下可验证性质：

- 同一输入、配置和精度身份下，坐标与模型矩阵逐字节一致；
- 所有数值阶段的误差都能与同一输入上的 float64 参考执行比较；
- 门限判决在容差带内有显式稳定规则；
- 每次提交是一个可校验、可整体替换的完整快照；
- 资源不足时的降级决策只依赖维度、配置和精度，不依赖运行时内存探测；
- 自检能从快照与原始样本重建单样本坐标。

## 2. 样本与编码

样本生成使用 `PCG64(seed)`。数值特征由类别中心、固定尺度和标准正态噪声组成；类别列由固定权重与同一 PRNG 状态生成；`row_id` 和 `label` 均按行号确定性构造。

CSV 特征顺序固定为：

```text
row_id, numeric columns..., categorical columns..., label
```

类别编码规则：

1. 每个类别列读取联合排序后的唯一值；
2. 使用 `column=value` 作为 one-hot 特征名；
3. 数值列在前，类别 one-hot 列在后；
4. 标签不进入特征矩阵。

## 3. 精度执行层

### 3.1 float64 / float32

输入先转换为目标 dtype。标准化均值、总体标准差和标准化值保持目标 dtype。PCA 协方差与 `eigh` 也使用目标 dtype。

### 3.2 定点

存储为 int64。设缩放因子为 `S`：

```text
quantize(x) = round_rule(x * S)
dequantize(q) = q / S
```

舍入规则：

- `half_even`: `np.rint`
- `half_away_from_zero`: 绝对值 `floor(x+0.5)` 后恢复符号
- `truncate`: 向零截断

设 `x_q = xS`、`m_q = mS`、`s_q = sS`，则：

```text
z_q = round((x_q - m_q) * S / s_q)
```

两个已缩放整数矩阵 `A_q`、`B_q` 相乘时：

```text
C_q = round(A_q @ B_q / S)
```

先检查 int64 乘法理论上界，避免静默溢出。PCA 协方差从已量化的中心化矩阵反缩放后计算，特征向量完成符号规范化再量化回定点存储。

### 3.3 精度切换

精度身份：

```json
{"mode": "fixed", "fixed_scale": 1024, "rounding": "half_even"}
```

有效快照存在且身份不同时，旧快照被归档并从 active 路径移除，持久化元数据状态变为 `invalidated`，当前运行返回 `E_PRECISION_CHANGED`。这是防止 float64 统计量、float32 坐标和定点矩阵混用的显式屏障。重试时不存在 active 快照，新精度可从完整输入重新构造。

## 4. PCA 与随机投影

### 4.1 PCA

1. 对编码后的矩阵做列中心化；
2. 计算 `Xc.T @ Xc / (n-1)`；
3. `eigh` 后按特征值降序排序；
4. 每个主成分以最大绝对载荷为正，消除特征向量符号歧义；
5. 截断到目标维或两段式中间维；
6. 坐标为 `Xc @ components.T`。

方差解释率为非负特征值占总特征值之和的比例。两段式的“方差解释率”记录第一段 PCA 在中间维上的累计值。

### 4.2 随机投影

给定输入维 `d_in`、输出维 `d_out`、seed 和 density：

```text
nnz = round(density * d_in * d_out)
positions = PCG64(seed).permutation(d_in*d_out)[:nnz]
values = PCG64(seed).standard_normal(nnz)
P[positions] = values / sqrt(density * d_in)
```

非零元数量由 density 精确决定。投影矩阵随后通过当前精度层 cast；自检会从 seed/density 重新生成并逐元素比较。

### 4.3 两段式

```text
X → PCA(k_intermediate) → Z_int → RP(seed2, density2, k_target) → Z
```

快照记录两段参数、两个矩阵 checksum、中间坐标及其 checksum。中间维必须满足：

```text
target_dim <= intermediate_dim <= encoded_feature_dim
intermediate_dim <= n_rows - 1
```

## 5. 邻域与保真度

邻域距离为坐标空间欧氏距离平方。每个样本按 `(distance, row_index)` 的稳定排序取前 k 个邻居，类一致率为邻居标签等于自身标签的比例均值。

保真度谱按维输出：

```text
variance_share_i = Var(Z_i) / sum_j Var(X_j)
cumulative_share_i = sum_{t<=i} variance_share_t
```

对 PCA 可同时观察第一段特征值解释率；随机投影没有 PCA 意义上的方差解释率，因此门限判决标记为 `not_applicable`。

## 6. 误差谱

参考执行强制使用 float64，但保留当前有效方法（包括资源降级后的随机投影）、相同 seed/density、相同标准化选择和相同类别映射。

绝对误差：

```text
e_abs = |observed - reference|
```

相对误差分母：

```text
max(|reference|, RMS(reference_stage), subnormal_floor)
```

这样既保留元素级相对偏差，又避免参考值接近零时相对误差无界放大。报告同时给出绝对与相对误差区间、RMS、p95、最大元素、容差限制和误差放大比。

主导来源按阶段绝对误差 RMS 最大者确定。所有阶段误差为零时主导来源为 `none`。

## 7. 门限稳定性

严格模式：

```text
decision = protected_value > threshold
```

容差带模式：

```text
band = max(configured_tolerance, subnormal_floor * max(|value|, |threshold|, 1))
if delta > band: accept
else if delta < -band: reject
else: boundary_decision
```

容差带内不依赖舍入方向，因此相邻浮点数或不同精度下落在同一带内的判决保持一致。严格模式仍允许用于对照，但会明确标记 `stability_guaranteed=false`。

## 8. 快照格式

顶层：

```json
{
  "schema_version": 1,
  "snapshot_version": 6,
  "status": "active",
  "precision": {"mode": "float64", "fixed_scale": 1024, "rounding": "half_even"},
  "input": {"checksum": "...", "rows": 240, "encoded_features": 19},
  "config_checksum": "...",
  "resource_plan": {},
  "sections": {
    "config": {"version": 6, "checksum": "...", "content": {}},
    "standardization": {"version": 6, "checksum": "...", "content": {}},
    "model": {"version": 6, "checksum": "...", "content": {}},
    "coordinates": {"version": 6, "checksum": "...", "content": {}},
    "evaluation": {"version": 6, "checksum": "...", "content": {}},
    "error_spectrum": {"version": 6, "checksum": "...", "content": {}},
    "selfcheck": {"version": 6, "checksum": "...", "content": {}}
  },
  "overall_checksum": "..."
}
```

数组编码：

```json
{
  "dtype": "<f8",
  "shape": [240, 4],
  "data_b64": "..."
}
```

JSON 使用排序键和稳定分隔符计算 checksum。提交时先写临时文件、`fsync`，再 `os.replace` 到 active 路径；不存在增量修补旧快照的路径。

元数据单独记录 `latest_version`，因此快照失效后版本号仍保持单调。读取 active 快照时检查：

- schema 与版本；
- 每个分节版本；
- 每个分节内容 checksum；
- 顶层整体 checksum；
- 与元数据记录的 active checksum 是否一致；
- active 版本是否低于已记录最新版本。

## 9. 资源规划

估算只使用显式维度：

```text
PCA 矩阵预算 ≈ max(d_feature^2, d_feature * d_target) * bytes
两段式矩阵预算 ≈ max(PCA 矩阵, d_intermediate * d_target) * bytes
随机投影矩阵预算 = d_feature * d_target * bytes

PCA/两段式缓冲 ≈ 中心化输入 + 中间坐标 + 输出坐标
随机投影缓冲 ≈ 输出坐标
```

`degrade_random_projection` 策略按固定顺序检查特征上限、矩阵预算、投影缓冲。触发降级后重新估算并检查随机投影路径；仍有超限则返回 `E_RESOURCE_LIMIT`。同一配置的决策序列因此可复现。

## 10. 自检

提交前自检使用待提交快照本身，不依赖内存中尚未持久化的数组：

1. `verify_snapshot`；
2. 解码坐标并重算数组 checksum；
3. 比较评估分节中的 `coordinates_version`；
4. 从 `data/demo.csv` 的第 0 行重建编码、中心化和坐标；
5. 针对每个随机投影阶段：seed/density 重新生成矩阵；
6. 两段式：对中间坐标重新应用第二段矩阵；
7. 两段式：构造 `components.T @ second_stage_matrix`，与两段式路径在 float64 下比较。

任何一项失败都会在 `E_INCONSISTENT.details.categories` 中给出类别。`selfcheck_current` 可以重启后从 active 快照再次执行全部检查。

