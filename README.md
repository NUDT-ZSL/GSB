# 聚类与密度估计诊断工作台

面向本地二维至八维数值样本的确定性 GMM/KDE 分析工作台。项目从空目录生成样本与配置，提供批量全量分析、按 `sample_id` 的交互查询、查询/批量一致性核对、三档精度误差谱、内容寻址缓存、版本化快照、并发提交控制和跨模块自检。

运行环境：Python 3.10+、NumPy 1.24+。不依赖 Docker、数据库、网络或外部数据集。

## 快速开始

```bash
python -m pip install "numpy>=1.24"
python -m density_workbench demo --root .
```

首次运行会在工作目录确定性生成：

- `samples.jsonl`：48 个三维样本，字段为 `sample_id`、`feature`、`group`。
- `config.json`：默认 `float64` GMM 配置。
- `cache/`：内容寻址缓存。
- `snapshots/`：版本化快照。
- `artifacts/`：批量表、误差谱、自检和演示摘要。

常用命令：

```bash
# 全量分析
python -m density_workbench run --root . --precision float64 --chunk-size 8

# 切换精度或模型后重新分析
python -m density_workbench run --root . --model kde --bandwidth 0.7 \
  --precision fixed --chunk-size 4

# 交互查询，可临时覆盖延迟预算
python -m density_workbench query sample-0000 --root . --budget-ms 100

# float64 参考误差谱
python -m density_workbench error-spectrum --root .

# 跨模块自检
python -m density_workbench self-check --root .
```

Python API：

```python
from density_workbench import Workbench, ensure_workspace

ensure_workspace("./work")
wb = Workbench("./work")
analysis = wb.analyze()
query = wb.query("sample-0000", latency_budget_ms=100.0)
spectrum = wb.error_spectrum()
check = wb.self_check()

# 切换精度/分块，或追加、修改样本后整体失效并提交新快照
wb.reconfigure(precision="float32", chunk_size=4)
wb.append_samples([{
    "sample_id": "sample-new",
    "feature": [0.1, -0.2, 0.3],
    "group": "new-group",
}])
wb.analyze()
```

## 配置字段

`config.json` 的主要字段如下，同时兼容 `components`、`covariance`、`tolerance`、`init_seed`、`scale_factor`、`rounding_mode` 等常见别名。

| 字段 | 含义 |
| --- | --- |
| `model_family` | `gmm` 或 `kde` |
| `n_components` | GMM 分量数，正整数 |
| `bandwidth` | KDE 固定带宽，正数 |
| `covariance_type` | `full`、`diag`、`spherical` |
| `max_iter` / `tol` / `min_improvement` | 迭代与收敛门限 |
| `convergence_policy` | `strict` 或 `tolerance_band` |
| `convergence_rel_band` / `convergence_abs_band` | 容差带 |
| `normalization` | `zscore` 或 `minmax` |
| `chunk_size` | 样本分块口径 |
| `precision` | `float64`、`float32`、`fixed` |
| `fixed_scale` / `fixed_rounding` | 定点缩放因子与舍入模式 |
| `seed` | GMM 初始化种子 |
| `latency_budget_ms` | 单次交互查询预算 |
| `max_responsibility_memory_mb` | 驻留责任度表内存上限 |
| `max_batch_samples` | 单批样本上限 |
| `max_parallel_queries` | 并行查询上限 |
| `consistency_tolerance` | 可选的查询/批量一致性绝对容差 |

定点舍入支持 `round_half_even`、`round_half_away_from_zero`、`floor`、`ceil`、`trunc`。

## 数值语义

### 标准化

- `zscore`：`(x - mean) / std`，方差下限由 `variance_floor` 保护。
- `minmax`：`(x - min) / (max - min)`，范围下限由 `variance_floor` 保护。
- 查询路径直接复用批量标准化统计量，使用同一算子、同一精度档和同一分块口径。

### 精度档

- `float64`：IEEE-754 binary64。
- `float32`：每步算子输出量化到 binary32，归约使用 float32 累加。
- `fixed`：输入按 `fixed_scale` 转为缩放整数；加法和分块归约使用整数累加；乘法按 `(a_int * b_int) / scale^2` 后重新量化；`exp/log/sqrt/线性求解` 等非线性操作在反量化后执行并立即按声明的舍入模式重新量化。

因此三档共用同一算子顺序和分块顺序，但舍入边界显式可追踪。数值结果不依赖当前时间、进程哈希随机化或未固定随机种子。

### GMM

- 初始化由 `seed` 驱动，确定性选择初始均值并构造协方差。
- E 步按块升序计算责任度和块对数似然。
- 块对数似然按块索引顺序逐个累加。
- M 步按块累计责任度、加权均值与协方差充分统计量，并按块索引顺序归约。
- `full` 协方差使用带确定性抖动保护的 Cholesky 求解；`diag` 和 `spherical` 共用相同更新语义。
- 批量结果保留最终责任度、分量、密度、对数密度、对数归一化项及置信摘要。

### KDE

固定带宽高斯核：

```text
p(x) = 1 / (n * (2*pi*h^2)^(d/2))
       * sum_i exp(-||x - x_i||^2 / (2*h^2))
```

每个源样本块先计算该块对全部查询点的核贡献，再按块索引升序归约。查询点与批量的源块集合、归约顺序、标准化统计量完全一致。

### 收敛保护

`strict` 使用严格门限比较；`tolerance_band` 使用容差带比较。两种策略都带：

- 门限附近精度保护：将 binary64/binary32/定点的数值噪声纳入保护带。
- 次正规数保护：配置 `subnormal_floor > 0` 时，低于下限的改善量按保护值处理。
- 保护轮次和跳过的 M 步逐项写入 `protected_rounds`、`skipped_records`。

非法 `tol`、`min_improvement` 等字段返回 `E_CONFIG_INVALID` 并给出字段名。

## 交互查询

`Workbench.query(sample_id)` 返回：

- GMM：责任度分布、所属分量、密度、对数密度、置信摘要。
- KDE：密度、对数密度、置信摘要；责任度为空且分量为 `null`。
- `latency`：围绕真实查询路径（快照解析、必要时的首次准备和计算循环）使用 `time.perf_counter_ns` 测得的实际耗时。
- `estimate`：样本数、分块数、核函数/GMM 分量求值次数。
- `consistency`：与批量同 `sample_id` 结果比较的容差、最大绝对误差、标准化统计量哈希和分块集合。

查询与批量同时校验：

1. 精度档一致。
2. 标准化统计量哈希一致。
3. 参与计算的分块 ID 集合一致。
4. 责任度、所属分量、密度、对数归一化项等数值在声明容差内。

任一偏差返回 `E_INCONSISTENT`，包含首个偏差位置、查询路径、批量路径及两侧中间量。超过真实耗时预算返回 `E_LATENCY_BUDGET`，包含已完成块、总块数、实际消费预算和部分结果；不会伪造或压缩处理耗时。

## 内容寻址缓存

缓存对象以规范化 JSON 内容的 SHA-256 命名，`keys/` 保存缓存键到内容哈希的原子映射。标准化统计量、标准化值、GMM 分块责任度、KDE 密度分块贡献、GMM 拟合结果和批量结果均落盘。

缓存键纳入：

- 样本内容哈希；
- 分块口径；
- 精度档及定点缩放/舍入参数；
- 初始化种子；
- 上游统计量、模型、标准化值和分块产物哈希。

相同键直接复用；不同键必须重算。失效报告输出：

- `recomputed_samples`、`reused_samples`、`changed_samples`；
- 每个缓存阶段的命中/未命中动作；
- 复用对象哈希和写入对象哈希。

`recomputed_samples` 与 `reused_samples` 始终无交集，并集覆盖当前分析的全部受影响样本。GMM 的参数为全局依赖，KDE 的每个密度行也依赖全部源样本；样本追加、统计量变化或模型变化会按依赖整体失效，而不是对旧数值做增量修补。

## 快照

`snapshots/bundles/<version>.json` 保存完整的三类快照：

- `statistics`
- `model`
- `batch`

每类携带类别、版本号、内容校验和与完整负载。`snapshots/current.json` 是唯一原子替换的当前指针。提交过程：

1. 获取非阻塞进程锁和线程锁；冲突立即返回 `E_STAGE_BUSY`。
2. 从当前版本单调递增分配新版本。
3. 写入不可变完整 bundle。
4. 原子替换 `current.json`。

重启加载时校验当前指针、bundle 校验和以及每个类别校验和。若版本或校验和变化，读取完整当前快照并整体替换内存状态。请求旧版本或覆盖已存在版本返回 `E_SNAPSHOT_ROLLBACK`；类别损坏或校验和不符返回 `E_SNAPSHOT_CORRUPT`，并指明类别与版本号。

## 误差谱

`Workbench.error_spectrum()` 在同一数据集和配置下额外计算 `float64` 参考批量结果，输出四级误差：

1. `standardization`：标准化矩阵与 float64 参考的偏差。
2. `chunk_reduction`：最终密度/责任度按显式分块归约后的偏差。
3. `quantization_rounding`：float64 标准化参考经过候选精度量化器后的表示误差。
4. `convergence_decision`：最终对数似然、迭代轮次与收敛标记偏差。

每级包含：

```text
absolute deviation = |candidate - reference|
relative deviation = |candidate - reference| / max(|reference|, 1e-300)
error interval = [min(candidate-reference), max(candidate-reference)]
amplification ratio = max_abs_deviation / max(unit, upstream_error, 1e-300)
```

主导来源限定为 `chunk_reduction`、`quantization_rounding`、`convergence_decision` 之一。自检会从每级 `details` 的参考值、候选值、单位和上游误差重新计算摘要，并验证与落盘摘要完全一致。

## 业务错误码

| 错误码 | 触发条件 |
| --- | --- |
| `E_DUPLICATE_SAMPLE_ID` | 重复 `sample_id` |
| `E_FEATURE_DIM` | 特征维度不在 2..8 或数据集内不一致 |
| `E_CONFIG_INVALID` | 分量数/带宽/容差/门限/分块等非法 |
| `E_PRECISION_UNKNOWN` | 未知精度档 |
| `E_SAMPLE_NOT_FOUND` | 查询不存在的样本 |
| `E_LATENCY_BUDGET` | 真实查询耗时超预算 |
| `E_INCONSISTENT` | 查询与批量结果或元数据不一致 |
| `E_RESOURCE_LIMIT` | 样本数、责任度内存或并行查询超限 |
| `E_STAGE_BUSY` | 并发快照提交冲突 |
| `E_SNAPSHOT_ROLLBACK` | 回退或覆盖旧版本 |
| `E_SNAPSHOT_CORRUPT` | 快照类别/版本/校验和损坏 |

## 自检

`Workbench.self_check()` 校验：

- 查询索引键集合、顺序与批量行一致；
- 每行结果校验和可重算；
- 误差谱每级可由偏差明细重算；
- 重算集合与复用集合无交集，并集覆盖全部受影响样本；
- 当前快照完整加载；
- 批量缓存键包含样本哈希、分块口径、精度档、种子和上游产物哈希。

自检结果写入 `artifacts/self_check.json`。

## 验证记录

验证日期：2026-11-24。环境：Python 3.10.12、NumPy 1.26.4。

```text
$ python -m unittest discover -v
...
Ran 19 tests in 5.672s

OK
```

覆盖的验收点：

- GMM 与 KDE 在 `float64`、`float32`、`fixed` 三档下的批量重跑字节一致；所有模式查询结果与批量结果最大绝对偏差为 `0.0`。
- 19 项测试覆盖查询/批量一致性、真实延迟超限、跨精度收敛轮次稳定、误差谱重算与定位、缓存命中/失效、快照版本递增/损坏/回退/并发冲突、资源上限、输入校验和自检。
- 默认 48 样本演示运行生成 6 个分块、快照版本 1；批量校验和 `dc6ca5f21e49b77d6f4aa60a983b28421dc15c94372f5c747fb374f42da3e637`；查询 `sample-0000` 密度 `0.06523965843419186`，查询/批量偏差 `0.0`，自检无失败项。
- 两个独立临时工作目录、两个独立 Python 进程重复执行 `run --force`，得到相同批量校验和。

数值可复现性以同一 Python/NumPy/CPU 运行时为前提；所有业务计算顺序、归约顺序、随机源和序列化均显式固定，系统时间只用于延迟观测，不参与数值结果或缓存键。
