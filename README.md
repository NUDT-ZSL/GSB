# 表格分类数值一致性诊断工作台

这是一个完全本地运行、确定性可复现的表格分类诊断工作台。它从本地 JSONL 样本训练
二分类模型，并同时比较：

- 分块训练与一次性整段训练；
- `float64`、`float32`、定点缩放整数三档精度；
- 标准化、分块累加、定点量化、舍入模式、学习率缩放各级误差；
- 严格比较与容差带比较两种收敛判定；
- 轮次边界中断与续算；
- 版本化统计量/分块/误差谱/轨迹快照。

详细数值契约、公式和错误码见 `docs/DESIGN.md`。

## 快速开始

要求 Python 3.10+、NumPy 1.24+。所有数据和快照均在本机文件系统。

```bash
python3 --version
python3 -c "import numpy; print(numpy.__version__)"

# 清空并创建本地运行目录
python3 -m numeric_workbench prepare --root .

# 生成默认 240 行确定性样本
python3 -m numeric_workbench generate --root . --config configs/default.json

# 从空状态执行完整演示
python3 -m numeric_workbench demo --root . --config configs/default.json

# 交互式诊断
python3 -m numeric_workbench interactive --root . --config configs/default.json

# 从完整报告生成紧凑验收摘要
python3 -m numeric_workbench summary --root . --job-id demo-job
```

交互命令：

```text
run | resume | status | trajectory | spectrum | dominant
switch <float64|float32|fixed_point> | checkpoint | checks | verify
errors | refresh | quit
```

常用目录：

```text
samples/     JSONL 样本与 manifest
runs/        检查点、作业注册表、完整 JSON 报告
snapshots/   版本化快照
cache/       最近报告和最近业务错误
```

`demo` 会先清空 `samples/`、`snapshots/`、`cache/`、`runs/`，再从空状态生成样本并运行。

## 配置

默认配置是 `configs/default.json`。核心字段：

| 字段 | 说明 |
| --- | --- |
| `job_id` | 作业唯一标识 |
| `model_family` | `logistic_regression` 或 `stump_ensemble` |
| `regularization_strength` | L2 强度，范围 `[0, 10]` |
| `max_iterations` | 迭代上限 |
| `learning_rate` | 学习率 |
| `convergence_tolerance` | 相对收敛容差，必须为正 |
| `min_improvement` | 最小绝对改善门限，必须非负 |
| `convergence_strategy` | `strict` 或 `tolerance_band` |
| `standardization` | `zscore` 或 `minmax` |
| `precision` | `float64`、`float32`、`fixed_point` |
| `fixed_scale` | 定点缩放因子，正整数且不超过 `2^24` |
| `rounding_mode` | `nearest_even`、`half_away_from_zero`、`toward_zero`、`floor`、`ceil` |
| `chunk_size` | 分块大小，正整数 |
| `consistency_tolerance` | 分块/整段绝对一致性容差 |

字段非法统一返回 `E_CONFIG_INVALID`，`details.field` 指出字段。

## 分块、精度与定点

分块梯度按以下显式顺序归约：

```text
chunk_index 升序
  -> chunk 内 sample_index 升序
  -> 块内求和
  -> 顺序左折叠块和
```

整段模式按 `sample_index` 升序做一次向量化归约。切换精度不会改变分块方案、样本索引、
归约顺序或收敛规则；报告中 `precision_switch.contract` 会显式校验这些不变量。

定点档使用整数缩放值和配置舍入模式。每轮操作轨迹可由 `fixed_scale + rounding_mode`
重放；报告 `fixed_replay` 给出精确重放结果和量化/反量化往返校验。

## 报告内容

完整报告位于 `runs/<job_id>/report.json`，包括：

- 三档精度的分块损失轨迹、整段损失轨迹和逐轮逐参数偏差；
- `E_TOLERANCE_EXCEEDED` 的首轮越界位置；
- 标准化/分块/定点/舍入/学习率等各级绝对与相对偏差分位数和放大比；
- 主导偏差来源、最差分块与轮次、逐轮逐分块归因明细；
- 精度切换对照与不变量检查；
- 严格/容差带两种收敛结论；
- 已提交轮次、重建轮次、丢弃轮次；
- 样本索引一致性和轨迹/误差谱重算结论；
- 快照版本号、校验和和整体替换记录。

工作台文本视图会展示轨迹对照、误差谱、主导来源、精度切换、检查点和最近业务错误码；
`verify` 会检查样本索引与两类重算结论，不一致返回 `E_INCONSISTENT`。

## 中断与续算

```bash
# 第 3 轮提交后停止
python3 -m numeric_workbench run --root . \
  --config configs/default.json --stop-after 3

# 从检查点继续
python3 -m numeric_workbench resume --root . --config configs/default.json
```

也可模拟轮内终止：

```bash
python3 -m numeric_workbench run --root . --config configs/default.json \
  --interrupt-round 4 --interrupt-after-chunk 2
```

轮内终止不会提交第 4 轮；续算从第 4 轮起点重建。强制进程终止只依赖原子落盘的检查点，
不承诺生成退出报告。

## 快照

```bash
python3 -m numeric_workbench snapshot-show --root . --job-id demo-job
```

加载会重算每个阶段的 SHA-256。版本回退返回 `E_SNAPSHOT_ROLLBACK`，校验和损坏返回
`E_SNAPSHOT_CORRUPT`，均带阶段名和版本号。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖配置/输入错误码、确定性样本、分块/整段对照、三档精度、定点重放、误差谱明细、
门限稳定判定、中断续算、快照回退/损坏、重复 job_id、交互工作台与 `E_INCONSISTENT`。

## 实际验证记录

正式演示在仓库根目录从空状态执行：

```bash
python3 -m numeric_workbench demo --root . --config configs/default.json
```

验收摘要写到 `artifacts/verification-summary.json`，包含样本 SHA-256、主轨迹摘要、
三档精度的分块/整段最大偏差、重算一致性、快照版本和检查点轮次。生成的完整报告与大型
快照保留在本地 `runs/`、`snapshots/` 目录，不纳入版本控制。

本次实际执行结果：

- 样本：240 行，训练 180、验证 60，SHA-256
  `cbee8cfd4fd8e29a87d668a13f261b3a9491a46cd0852a3cfb61170b9f7ac94b`。
- 主精度 `float64`：36 轮收敛；分块/整段逐轮最大绝对偏差
  `2.220446049250313e-16`，最终参数最大偏差 `6.938893903907228e-18`。
- `float32`：36 轮收敛；逐轮最大偏差 `1.1920928955078125e-07`，最终参数最大偏差
  `2.9802322387695312e-08`。
- `fixed_point`（scale `65536`，`nearest_even`）：37 轮收敛；分块/整段最大偏差和最终
  参数偏差均为 `0.0`；定点操作轨迹重放与量化/反量化往返均精确一致。
- 三档除 `fixed_point` 的收敛轮次为 37 外，各档分块与整段的收敛轮次均一致；所有档位
  均通过配置容差 `1e-6`。
- 主导偏差来源：`fixed_point/fixed_point_quantization`，增量严重度
  `0.004091075581328596`，最差明细为第 33 轮第 2 块（`r00065` 到 `r00096`）。
- 样本索引一致性、轨迹重算、误差谱重算均为 `true`；三档 `chunked/full` 双重运行
  均逐字节一致；误差谱两次 SHA-256 均为
  `e6b3d34567eeb2dfa6023f68ea742fd10d716ec0c6951a9977ef3d42a7cc8b90`。
- 快照四阶段版本均为 `1`，整体替换模式为 `whole_snapshot_replace`。
- 测试：`35` 个用例全部通过，记录见 `artifacts/test-results.txt`。
