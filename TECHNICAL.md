# 技术说明与验收记录

## 1. 流水线模型

设：

- 表逻辑形状为 `(V, D)`。
- 索引形状为 `(T, K)`，`T` 为 token 位置数，`K` 为每个 token 的命中数。
- gathered 形状为 `(T, K, D)`。
- 位置权重形状为 `(T, D)`。

流水线：

```text
index validate / pad mask
  -> quantized table build
  -> dequantize
  -> gather
  -> reduce
  -> weighted accumulate
  -> per-token output
```

`gather_axis=1` 时物理表为 `(D, V)`，但逻辑表仍为 `(V, D)`，因此索引约束和输出形状不变。

## 2. 确定性

表、索引、权重使用三个相互独立的 NumPy 随机流：

```python
table_rng = np.random.default_rng(np.random.SeedSequence([seed, 0]))
index_rng = np.random.default_rng(np.random.SeedSequence([seed, 1]))
weight_rng = np.random.default_rng(np.random.SeedSequence([seed, 2]))
```

嵌入表和位置权重为 `standard_normal` 的 float64 数组。权重再乘以 `scale`。索引生成后按描述覆盖重复位置和 padding 位置。

掩码摘要和报告 JSON 都采用排序键、无多余空白的规范序列化。测试通过两次 CLI 运行比较完整报告字节验证确定性。

## 3. 定点量化

量化档：

- `qint8`: `[-128, 127]`
- `qint16`: `[-32768, 32767]`

```text
q = round_mode(truth / qscale + zero_point)
q = clip(q, qmin, qmax)
dequant = (q - zero_point) * qscale
```

量化完成后，`dequantize_table` 从存储表、步长、零点和数值配置独立重算一次，报告记录 `independent_recompute_diff`。默认样例 qint8 使用 `0.03` 步长，覆盖生成表范围，`clipped_values=0`。

float32 档通过 float32 存储再转回 float64，模拟低精度存储；float64 档不产生存储损失。可选的 `dequant_rounding_step` 在所有档上统一施加显式舍入网格。

## 4. 归约与重算

定义 `active`：

- `exclude`: 非 padding 命中。
- `zero_weight`: 所有命中参与结构，但 padding 的系数强制为 `0`。

系数：

```text
sum:      alpha = 1 for active
mean:     alpha = 1 / denominator for active
first:    alpha = 1 for first active, else 0
concat:   alpha = 1 for active and blocks are concatenated
```

`mean` 的分母：

- `exclude`: 非 padding 命中数。
- `zero_weight`: 固定为 `K`。

因此同一 padding 输入在两种口径下会有明确、可审计的数值差异。

非 concat 输出：

```text
output[t,d] = sum_k alpha[t,k] * position_weight[t,d] * table[index[t,k],d]
```

concat 输出按命中顺序为 `[hit0 vector, hit1 vector, ...]`，每个 block 长度为 `D`。权重明细中的 `output_slice` 与这个布局一致。

自检中的重算不调用流水线主体，而是读取 `token_weight_details` 和反量化表，按 `token-major, hit-major` 顺序执行同样的舍入与次正规数策略，然后比较最终输出。默认样例的重算差为 `0.0`。

## 5. 误差谱

每级统计：

```text
diff = actual - reference
abs_dev = abs(diff)
rel_dev = abs_dev / max(abs(reference), observed_resolution)
error_interval = [min(diff), max(diff)]
amplification_ratio = current_peak / previous_peak
```

当上一级峰值为零而当前级非零时，放大比记为字符串 `"inf"`，避免输出非标准 JSON 的 `Infinity`。

数值分辨率：

- qint 档：量化步长。
- float32：按数值尺度的 float32 精度。
- float64：按数值尺度的 float64 精度。
- 开启累加舍入时：累加阶段优先记录舍入步长。

## 6. 主导来源分解

仅比较逐级峰值会把“位置权重放大已有反量化误差”误判为新的累加误差。因此归因采用隔离路径：

```text
base = 实际反量化表 + 实际归约/位置权重 + 禁用累加舍入

dequantization_contribution =
  max(abs(base - float64_reference))

weighted_accumulation_contribution =
  max(abs(final - base))

reduction_weights_contribution =
  当实际与参考归约系数完全相同时为 0
```

报告同时记录 `reduction_weight_delta_max_abs`。若未来把归约权重本身量化，系数差将变为非零，归因明细可以扩展为输出级反事实路径。

当前主导来源选择非负隔离贡献最大的项，顺序为：

```text
dequantization > reduction_weights > weighted_accumulation
```

全零时返回 `none`。

## 7. 输出区间与容差

每个输出元素：

```text
tolerance = tolerance_abs + tolerance_rel * max(1, abs(reference))
lower = min(actual, reference) - tolerance
upper = max(actual, reference) + tolerance
```

区间中心与半宽也按上述上下界计算。区间规则和容差策略均写入报告。容差不会根据精度档或观测误差自动扩大。

## 8. 快照状态机

```text
current absent
  -> valid snapshot: loaded

incoming.version == current.version
  -> checksum same: unchanged
  -> checksum different: E_SNAPSHOT_CORRUPT

incoming.version > current.version
  -> checksum valid: whole replacement
  -> checksum invalid: E_SNAPSHOT_CORRUPT

incoming.version < current.version
  -> checksum invalid first: E_SNAPSHOT_CORRUPT
  -> checksum valid: E_SNAPSHOT_ROLLBACK
```

替换时创建新内容副本，不合并旧字段。报告示例使用仅在 v1 存在的字段验证替换后旧字段消失。

## 9. 自检

`self_check_job` 执行三项检查：

1. `token_position_sets_consistent`
   - 索引校验、Gather、归约、累加看到相同的 token 位置集合。
2. `error_intervals_ordered`
   - 每个阶段均满足 `lower <= upper`。
3. `output_recomputable_from_table_and_weights`
   - 使用导出的权重明细和反量化表重算，记录最大差与显式比较容差。

报告汇总 `self_checks_passed / self_checks_total`。

## 10. 测试覆盖

`tests/` 使用标准库 `unittest`，覆盖：

- 索引生成、padding 掩码、摘要复现。
- 首个越界位置、空索引、非法 padding 索引。
- qint 表独立重算、量化参数错误、舍入档切换。
- `gather_axis=0/1` 一致性与越界归因。
- 重复命中明细、sum/mean/first/concat。
- `exclude` 与 `zero_weight` 的均值分母差异。
- 可切换累加舍入与次正规数策略。
- 全精度零误差、四档误差排序、固定容差、主导来源。
- 全部结构化错误注入及阶段/任务归因。
- 快照整体替换、回退、损坏、同版本不同校验和。
- JSONL 重复任务检测。
- 完整报告结构和 CLI 两次运行字节一致性。

本轮运行：

```text
Ran 30 tests in 0.754s
OK
```

## 11. 边界与假设

- 确定性保证针对相同 Python/NumPy 版本与相同运行平台；固定种子和规范 JSON 避免时间、路径、字典顺序引入差异。
- `gather_axis=1` 的公开接口要求调用方传入转置物理表 `(D, V)`。
- 输出区间使用快照内 float64 参考真值包络，适用于诊断和验收；生产部署若没有参考路径，应改用由量化步长独立传播的保守区间。
- 当前归约系数直接由 float64 掩码和命中数计算，因此归约权重隔离贡献为零；阶段误差仍完整记录。
- 位置权重为确定性 NumPy 样本，不使用外部数据集。
