# 数值一致性诊断工作台技术说明

## 1. 总体结构

一次训练固定走以下流水：

```text
JSONL 输入
  -> 确定性训练/验证切分
  -> 数值列标准化 + 类别列固定 one-hot
  -> 逻辑回归或浅层决策树桩加性模型
  -> 分块梯度计算（或整段梯度计算）
  -> 显式顺序归约
  -> 参数更新
  -> 损失/验证指标
  -> 严格/容差带收敛判定
  -> 轮次提交点
```

代码分层：

| 模块 | 职责 |
| --- | --- |
| `numeric_workbench/config.py` | 配置解析与字段级校验 |
| `numeric_workbench/data.py` | 确定性样本生成、JSONL 加载、重复 row_id 检测、分层切分 |
| `numeric_workbench/precision.py` | float64、float32、定点整数共用算子 |
| `numeric_workbench/features.py` | z-score/min-max、类别编码、分块划分 |
| `numeric_workbench/convergence.py` | 严格比较、容差带比较、次正规保护 |
| `numeric_workbench/training.py` | 整段/分块训练、检查点状态、定点重放 |
| `numeric_workbench/diagnostics.py` | 逐轮对照、误差谱、主导来源、重算校验 |
| `numeric_workbench/snapshots.py` | 版本化、校验和、整体替换快照 |
| `numeric_workbench/reporting.py` | 作业编排、报告、错误缓存和续算 |
| `numeric_workbench/workbench.py` | 本地交互式文本工作台 |

## 2. 确定性输入与切分

- 样本生成使用固定 `PCG64(seed)` 和固定 JSON 键序。
- `row_id` 在加载时按文件顺序检查；发现重复立即返回 `E_DUPLICATE_ROW_ID`。
- 训练/验证切分对每个类别按原始行序取末尾固定比例作为验证集，再恢复全局行序。
- 标准化统计量只用训练索引计算。
- 类别水平使用全体样本中的字符串字典序，固定规则为 `sorted(unique(values))`。
- 特征名、参数名、样本索引和分块索引均显式写入报告。

## 3. 标准化与特征

数值列支持：

```text
z-score: (x - mean_train) / std_train
min-max: (x - min_train) / (max_train - min_train)
```

零方差/零极差列输出全零，并在统计量快照中记录 `constant=true`。

类别列使用排序后的固定 one-hot。当前模型中的 `bias` 是最后一维参数，学习率和 L2
正则化不作用于 bias。

模型族：

- `logistic_regression`：标准化数值列 + 类别 one-hot + bias。
- `stump_ensemble`：每个数值列在标准化空间生成
  `x<=-0.5`、`x<=0`、`x<=0.5` 三个深度为 1 的树桩特征，再与类别 one-hot、bias
  做加性逻辑回归训练。参数即各树桩和类别特征权重。

## 4. 分块与归约契约

分块口径是 `chunk_size`，按训练集内部索引升序切连续块。

分块模式：

```text
chunk_index 升序
  -> chunk 内 sample_index 升序
  -> 块内梯度求和
  -> 对块梯度做顺序左折叠累加
```

整段模式：

```text
sample_index 升序 -> 单次向量化归约
```

两种模式的样本索引集合完全一致，差异只来自浮点累加顺序。定点整数加法在不溢出的
范围内精确，因此分块与整段的定点梯度通常完全相同。

## 5. 三档精度

### float64 / float32

同一 `PrecisionEngine` 代码路径，数组 dtype 分别固定为 `float64` 和 `float32`。
标准化统计、特征、概率、损失、梯度、参数和验证指标均在对应 dtype 中计算。

### 定点缩放整数

配置：

```json
{"fixed_scale": 65536, "rounding_mode": "nearest_even"}
```

整数存储值满足 `q = round(x * scale)`，还原为 `x = q / scale`。

支持舍入：

- `nearest_even`
- `half_away_from_zero`
- `toward_zero`
- `floor`
- `ceil`

梯度和更新使用整数比值舍入：

```text
w_next = w - round_ratio(
    lr_units * (
        round_ratio(gradient_sum, n)
        + round_ratio(reg_units * w, scale)
    ),
    scale
)
```

报告中的 `fixed_replay` 保存缩放因子、舍入模式、操作轨迹校验和，并通过重新执行每轮
整数更新验证最终参数完全一致；同时验证 `quantize(dequantize(final)) == final`。

## 6. 收敛策略

每轮记录：

- 上一轮损失、当前损失、绝对改善和相对改善；
- 最小改善门限；
- 相对收敛容差；
- 有效绝对门限 `max(min_improvement, convergence_tolerance * loss_scale)`；
- 次正规刷新值；
- 严格比较结论；
- 容差带比较结论；
- 配置选中的策略及其结论。

两种策略：

- `strict`：在有效门限外增加统一的 float32 量级 tie guard，使门限附近不因精度舍入翻转。
- `tolerance_band`：把 `[threshold, threshold + band]` 作为一个稳定判定带。

`band` 至少包含配置相对容差、统一 tie guard 和次正规保护下限。报告同时保留两种结论，
实际停止轮次只由配置选择的策略决定。

## 7. 一致性对照与误差谱

每个精度档同时训练：

- 分块模式；
- 整段模式。

逐轮输出：

- 两种损失；
- 全部参数逐项偏差；
- 每轮最大绝对偏差；
- 最终参数偏差谱；
- 收敛轮次是否一致；
- 第一个越界轮次、位置和参数名。

任一分块/整段偏差超出 `consistency_tolerance`、轮数不一致或收敛轮次不一致时，作业报告
状态为 `ERROR`，业务码为 `E_TOLERANCE_EXCEEDED`，并记录首轮越界位置。

误差谱以 float64 整段训练为参考，包含以下层级：

1. `standardization`：目标精度统计量和变换，模型算术仍用 float64。
2. `chunk_accumulation`：同精度下分块归约相对整段归约。
3. `fixed_point_quantization`：在相同定点标准化特征上，定点算术/存储相对 float64
   整段训练；`metrics_vs_reference` 同时保留其相对 float64 真值的累计偏差。
4. `rounding_mode`：替代舍入模式相对配置舍入模式。
5. `learning_rate_scaling`：学习率先表示到目标精度，再用于 float64 整段训练。
6. `arithmetic_precision`：float32 整段算术相对 float64（补充定位 float32 算术损失）；
   定点算术损失已由 `fixed_point_quantization` 单独表示。

每一层同时给出：

- `metrics_vs_reference`：相对 float64 整段真值的偏差；
- `incremental_effect`：相对该层比较基线的纯增量；
- 绝对/相对偏差的 p50、p90、p99、max、mean；
- 误差放大比 `max_abs / float64_roundoff_floor`；
- `round_chunk_details`：逐轮、逐分块的样本索引、行号、梯度各参数绝对偏差、梯度范数、
  梯度校验和等明细。

主导来源按纯增量 `severity` 选择。`dominant_source` 自带最差分块/轮次以及完整
`round_chunk_details`；同一明细也保留在误差谱对应阶段。

## 8. 检查点与续算

检查点只在轮次提交后原子写入：

```text
params
parameter_trajectory
loss_trajectory_hex
validation_trajectory
both convergence decisions
fixed update trace
committed_rounds
```

停止发生在：

- 轮次边界：提交该轮后停止；
- 轮内指定分块后：该轮不更新、不写检查点，检查点仍保留上一提交轮次。

续算从 `rounds_completed + 1` 重建。未提交轮次自然被丢弃。恢复后的完整损失、参数和验证
指标与不间断训练做核心摘要逐字节比较。

强制进程终止时只依赖已原子落盘的检查点；不保证生成退出报告。

## 9. 快照

每个作业一个 `snapshot.json`，包含四个阶段：

- `normalization_statistics`
- `chunk_plan`
- `precision_error_spectra`
- `trajectory_summaries`

每阶段携带：

```json
{"version": 2, "checksum": "sha256...", "payload": {}}
```

校验和覆盖阶段名、版本和 payload。提交使用临时文件 + `os.replace`，整体替换旧快照。

- 加载时重算校验和；不符返回 `E_SNAPSHOT_CORRUPT`，带 `stage`、`version`。
- 提交版本低于当前版本返回 `E_SNAPSHOT_ROLLBACK`，带阶段和版本。
- 同版本但内容校验和变化时，作为整体替换提交并记录
  `checksum_change_same_version`。

## 10. 业务错误码

| 错误码 | 场景 |
| --- | --- |
| `E_CONFIG_INVALID` | 配置字段缺失/类型/范围非法 |
| `E_DUPLICATE_JOB_ID` | 注册表中 job_id 重复 |
| `E_DUPLICATE_ROW_ID` | JSONL 中 row_id 重复 |
| `E_TOLERANCE_EXCEEDED` | 分块/整段偏差、轮数或收敛轮次越界 |
| `E_INCONSISTENT` | 样本索引集合或轨迹/误差谱重算不一致 |
| `E_SNAPSHOT_ROLLBACK` | 旧版本覆盖新版本 |
| `E_SNAPSHOT_CORRUPT` | 快照校验和不符 |
| `E_RESUME_NOT_AVAILABLE` | 无可恢复检查点或作业已完成 |
| `E_RESUME_MISMATCH` | 输入/配置/分块计划与检查点不符 |

## 11. 复现边界

同一 Python 进程/同一版 NumPy 上重复运行保证逐字节一致。跨 NumPy 大版本或跨 CPU
浮点环境时，float64/float32 的底层 `exp/log/sum` 实现可能变化；定点整数结果不受这些
浮点核函数顺序影响，但 sigmoid/log 仍用于量化前计算。工作台对每次完整运行执行双重
重算并在报告中给出摘要；不一致时返回 `E_INCONSISTENT`。
