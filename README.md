# DimDiag

面向交叉组合与降维的本地可视化诊断工作台，核心关注数值精度、误差传播和快照一致性。项目从本地固定种子样本出发，把一次降维建模为可检查的计算链：

```text
标准化统计量估计
  → PCA / 随机投影 / PCA+随机投影
  → 降维坐标计算
  → 邻域类一致性评估
  → float64 参考误差谱
  → 门限判决留痕
  → 版本化快照整体提交
  → 跨路径一致性自检
```

不依赖网络、Docker 或外部数据集。已在本机 Python 3.10.12、NumPy 1.26.4、SciPy 1.13.1 下验证。

## 快速开始

```bash
python3 -m pip install -r requirements.txt
python3 -m dimdiag generate
python3 -m dimdiag demo
python3 -m dimdiag workbench --open
```

工作台默认监听 `http://127.0.0.1:8765/`，仅绑定本机回环地址。

常用命令：

```bash
# 运行单个方法并提交快照
python3 -m dimdiag run --config configs/demo.json --method pca
python3 -m dimdiag run --config configs/demo.json --method random_projection
python3 -m dimdiag run --config configs/demo.json --method pca_random_projection

# 切换精度档
python3 -m dimdiag run --config configs/demo.json --precision float32
python3 -m dimdiag run --config configs/demo.json --precision fixed --fixed-scale 16384

# 一致性自检、查看与归档
python3 -m dimdiag selfcheck --config configs/demo.json
python3 -m dimdiag inspect --config configs/demo.json --json
python3 -m dimdiag reset --config configs/demo.json

# 测试与完整验收
python3 -m unittest discover -v
bash scripts/run_validation.sh
```

## 本地样本与配置

`dimdiag.data` 使用 `numpy.random.Generator(PCG64(seed))` 生成 240 行、12 个数值列、2 个类别列、4 个类别的表格。`row_id`、数值列、类别列和 `label` 均确定性写出。生成脚本同时写入：

- `data/demo.csv`
- `data/demo.meta.json`
- `configs/demo.json`

配置支持 `csv` 与 `generated_table` 两种数据来源。示例配置包含：

- `method`: `pca`、`random_projection`、`pca_random_projection`
- `target_dim`
- `standardize`
- `random_projection.seed/density`
- `two_stage.intermediate_dim/seed/density`
- `precision.mode/fixed_scale/rounding`
- 类一致率与方差解释率门限、策略、容差和次正规数下限
- 逐级误差容差
- 特征数、矩阵内存、投影缓冲上限与溢出策略

## 精度语义

| 档位 | 存储 | 关键规则 |
|---|---|---|
| `float64` | `<f8` | 全精度参考实现 |
| `float32` | `<f4` | 标准化、协方差、PCA 特征分解、投影与坐标均使用 float32 算子 |
| `fixed` | `<i8` 缩放整数 | `q = round(x * scale)`；支持 `half_even`、`half_away_from_zero`、`truncate`；矩阵乘先在整数域执行，再按缩放因子归约 |

定点档的标准化使用显式统计量公式：

```text
z_q = round((x_q - mean_q) * scale / std_q)
```

PCA 协方差与特征向量构造完成后，特征向量被量化回缩放整数；坐标矩阵乘使用 int64，避免隐式混用浮点中间量。精度身份包括 `mode`、`fixed_scale` 和 `rounding`。

若已有快照使用不同精度身份，下一次提交会：

1. 将旧快照移入 `runtime/snapshots/invalidated/`；
2. 标记持久化元数据为 `invalidated`；
3. 返回 `E_PRECISION_CHANGED`；
4. 明确要求重试，重试后以新精度整体建立快照。

## 误差谱

每个阶段都以同一输入、同一有效方法、同一资源裁决下的 `float64` 执行为参考，输出：

- 绝对误差的 min/max/mean/p95/RMS；
- 相对误差的 min/max/mean/p95/RMS；
- 误差区间；
- 相对上一级的误差放大比；
- 最大偏差元素及其 reference/observed/abs/rel；
- 主导误差来源归因。

阶段固定为：

```text
standardization
model_construction                 # 主成分或投影矩阵
coordinates
two_stage_intermediate             # 仅两段式
```

主导来源映射为：

```text
standardization                 → standardization
model_construction              → projection_matrix_construction
coordinates                     → coordinate_computation
two_stage_intermediate          → two_stage_combination
```

任一阶段越过 `error_tolerance` 会返回 `E_TOLERANCE_EXCEEDED`，并携带越界阶段、元素索引、参考值、观测值、绝对/相对偏差和限制值。

## 门限稳定性

支持两种策略：

- `strict`：直接执行 `value > threshold`，可能被临界舍入影响，报告会标记 `stability_guaranteed=false`。
- `tolerance_band`：`|value-threshold| <= tolerance` 时使用显式 `accept/reject` 边界裁决；带外按严格方向判断。带内判决不随舍入方向翻转。

次正规数先按 `subnormal_floor` 规范到零，且该下限参与有效容差计算。每次判决在快照中记录 value、threshold、strategy、tolerance、delta、comparator、边界策略、次正规保护与稳定性标记。

## 快照

快照写入 `runtime/snapshots/active.json`，使用临时文件 + `os.replace` 整体替换。每个分节包含：

```json
{
  "version": 6,
  "checksum": "...",
  "content": {}
}
```

分节包括 `config`、`standardization`、`model`、`coordinates`、`evaluation`、`error_spectrum`、`selfcheck`；顶层另有整体校验和。数组以固定小端 dtype、C 顺序和 Base64 编码保存，确保矩阵/坐标可按字节复核。

版本规则：

- 版本单调递增；新提交整体替换 `active.json`；
- 旧版本提交返回 `E_SNAPSHOT_ROLLBACK`；
- JSON 损坏、分节校验和或整体校验和不符合返回 `E_SNAPSHOT_CORRUPT`；
- 重启后重新校验版本、分节 checksum 与整体 checksum。

## 资源裁决与降级

`resources` 可限制：

- `max_features`
- `max_dim_matrix_bytes`
- `max_projection_buffer_bytes`

内存估算完全由维度与精度字节数决定，不探测真实进程内存，因此决策可复现。`overflow_policy=degrade_random_projection` 时：

- 从 PCA 或两段式切换到稀疏随机投影；
- 在结果、快照和 UI 中记录 `degradation.category`，例如 `pca_to_random_projection`；
- 降级后重新检查随机投影路径是否仍超限；
- 自检使用快照中的有效方法与矩阵重建坐标，保证降级后仍可核对。

无法降级时返回 `E_RESOURCE_LIMIT`，附估算值、限制和触发字段。

## 一致性自检

每个提交在落盘前执行：

1. 整体与分节 checksum 校验；
2. 评估记录中的坐标 checksum 是否等于当前坐标；
3. 评估坐标版本是否等于当前快照版本；
4. 单样本从原始行、标准化统计量、编码映射和方法矩阵重建坐标；
5. 随机投影矩阵使用 seed/density 重新生成并逐元素比较；
6. 两段式第二段矩阵重新作用于中间坐标；
7. 将 PCA 基与第二段矩阵组合成等价直接投影，与两段式最终坐标对比相同 seed/density 下的结果。

失败返回 `E_INCONSISTENT`，`details.categories` 指出失败类别。

## 错误码

| 错误码 | 含义 |
|---|---|
| `E_INTERMEDIATE_DIM` | 两段式中间维度低于目标维度或高于编码/可用 PCA 维度 |
| `E_PRECISION_CHANGED` | 精度身份变化，旧快照已失效，需要重试 |
| `E_TOLERANCE_EXCEEDED` | 逐级误差超过配置容差 |
| `E_CONFIG_INVALID` | 配置字段、门限、容差或资源参数非法 |
| `E_SNAPSHOT_ROLLBACK` | 旧版本或同版本不同内容试图覆盖新快照 |
| `E_SNAPSHOT_CORRUPT` | 快照不可读或 checksum 不符 |
| `E_RESOURCE_LIMIT` | 特征/矩阵/缓冲超限且无法降级 |
| `E_INCONSISTENT` | 跨路径、坐标版本或单样本重建自检失败 |
| `E_INPUT_INVALID` | 本地 CSV 缺失、列角色错误、非有限数值等 |

## 工作台

工作台页面包含：

- PCA、随机投影、两段式路径切换；
- 精度、定点缩放、目标维度、标准化和门限策略控件；
- 前两维投影散点路径与类别图例；
- 每维方差份额柱状图与累计保真度曲线；
- 逐级误差谱、容差结果和主导误差来源；
- 门限判决留痕；
- 快照版本、整体/分节 checksum、资源估算与降级类别；
- 自检结果、最近业务错误码和三路径演示摘要。

前端不加载 CDN 或外部资源，全部由本地 Python HTTP 服务提供。

## 实际验证结果

执行 `python3 -m unittest discover -v`：23 项测试全部通过，覆盖确定性、PCA/RP/两段式、定点整数、one-hot 定点缩放、精度切换、误差谱、门限、快照回滚/损坏、资源降级、自检和 HTTP 工作台。

执行 `python3 -m dimdiag demo`：

| 方法 | 快照 | 坐标 checksum 前缀 | 类一致率 | 方差解释率 | 自检 |
|---|---:|---|---:|---:|---|
| PCA | v13 | `3d1285265890db22` | 0.966071 | 0.579057 | PASS |
| 随机投影 | v14 | `b055cd3ac030e83b` | 0.461905 | N/A | PASS |
| 两段式 | v15 | `ec7e133df793ae1c` | 0.697024 | 0.799935 | PASS |

两段式自检的等价直接投影最大绝对差为 `1.3322676295501878e-15`，单样本重建最大绝对差为 `3.3306690738754696e-16`。完整结果见：

- `artifacts/demo_report.json`
- `artifacts/demo_summary.md`
- `artifacts/test_results.txt`

## 目录

```text
dimdiag/
  config.py         配置默认值与校验
  data.py           确定性样本与 CSV 载入
  precision.py      三档精度算子
  core.py           标准化、PCA、随机投影、邻域评估、资源规划
  diagnostics.py    误差谱与门限稳定性
  snapshot.py       版本与 checksum
  engine.py         运行编排、快照组装、自检
  demo.py           三路径演示
  workbench.py      本地 HTTP 服务
  static/index.html 诊断界面
configs/demo.json
data/demo.csv
runtime/{cache,snapshots}/
tests/
docs/ARCHITECTURE.md
```

更详细的公式、序列化格式和自检设计见 `docs/ARCHITECTURE.md`。
