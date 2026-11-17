# 本地推理批处理与数值精度诊断工作台

这是一个从零实现的、面向本地 Windows 环境的推理批处理调度与数值精度诊断工作台。系统完全离线运行，使用固定随机种子生成小型多层前馈模型和样本，支持 float32 与定点量化两条前向路径，并对每条样本输出逐层误差界、偏差归因链、批内漂移标记、版本整体替换结果、持久化快照和逐位重放校验。

不使用 Docker、数据库、网络服务或外部推理框架；核心数值计算只依赖 NumPy，界面使用 Windows 官方 Python 自带的 Tkinter。

## 交付内容

```text
infer_workbench/        Python 包：数据、配置、分批、推理、诊断、快照、CLI、Tkinter UI
configs/                演示配置（float32、fixed q=10^3、fixed q=10^2）
data/                   已预置的确定性样本、模型参数与生成元数据
tests/                  unittest 自动化验收测试
scripts/                Windows 批处理启动脚本
snapshots/              运行期快照目录（演示默认使用 demo_out/snapshots）
reports/                运行期报告目录
artifacts/              实际验证输出与技术证据
demo_out/               默认演示输出（可随时整体清理后重建）
```

预置数据规模为 12 条样本、输入维度 4、隐藏层 6/5、输出维度 3，随机种子为 `20261117`。数据和模型参数已落盘，重建命令见下文。

## 环境准备

推荐 Windows 10/11 + Python 3.10 或更高版本。Tkinter 使用 Windows 官方 Python 安装包自带组件。

```bat
cd /d path\to\uid68
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

核心与测试可在无图形环境运行；界面需要可用桌面会话和 Tk 8.6。

## 快速开始

### 1. 运行三阶段确定性演示

```bat
python -m infer_workbench demo --out-dir demo_out
```

也可运行 `python demo.py --out-dir demo_out`。

演示默认先整体清理 `demo_out`，避免旧快照、旧报告或缓存影响版本识别与重放校验；如需保留旧输出可加 `--keep`。它会依次执行：

1. float32 路径完整推理。
2. fixed q=10^3 路径推理 2/4 个批次，并在每批完成后原子写入快照。
3. 切换为 fixed q=10^2，整体替换当前结果，并输出新旧版本差异。
4. 重新打开 q=10^3 的部分快照，整体替换当前状态；逐位重放已完成批次，然后继续剩余批次。

稳定排序的诊断摘要写入 `demo_out/demo_summary.txt`，机器可读清单写入 `demo_out/demo_manifest.json`，各场景报告位于 `demo_out/reports/`。

### 2. 启动 Tkinter 诊断界面

```bat
python -m infer_workbench ui
```

或双击 `scripts\run_ui.bat`。界面包含：

- 批次列表、批内样本标识、精度模式、漂移状态和输出端相对偏差。
- 以样本为行、以层为列的累计误差界热力图。
- 逐层归因链：相对偏差、局部误差界、累计误差界、本层归因占比和累计占比。
- 当前配置版本、数据/参数摘要、快照列表、恢复和重放操作。

批次、样本、层和快照均使用稳定排序键渲染。

### 3. 重新生成确定性数据

```bat
python -m infer_workbench generate --seed 20261117 --samples 12 --input-dim 4 --hidden-dim 6 5 --output-dim 3 --data-dir data --force
```

生成器使用自实现的 SplitMix64 整数随机流，避免 NumPy 随机算法版本变化引入差异。输入和参数以百万分之一为单位生成，再转换为 float32。

当前预置摘要：

```text
data_digest      = b123f81d17b71f20572961f4ffd895934eb705490bb1ea8edf7e132ebfbbdecf
parameter_digest = 3729aadcf2f86138534745db5022f7e4c49d54460702aa5c45fa5801807d7b5a
```

## 批处理配置协议

示例：

```json
{
  "batch_size": 3,
  "sort_strategy": "input_norm",
  "precision_mode": "fixed",
  "quant_scale": 1000,
  "drift_threshold": 0.005
}
```

字段说明：

| 字段 | 规则 |
| --- | --- |
| `batch_size` | 必须为正整数，布尔值不接受 |
| `sort_strategy` | `sample_id`、`input_norm`、`input_order` 之一 |
| `precision_mode` | `float32` 或 `fixed` |
| `quant_scale` | 正整数字面量，且必须是 10 的整数幂；也可用 `quant_scale_power` 表示指数 |
| `drift_threshold` | 可选，有限非负数，默认 `0.01`；按层相对偏差判定漂移 |

版本号由以下字段的规范化 JSON 计算 SHA-256 后取前 16 个十六进制字符：

```text
batch_size, sort_strategy, precision_mode, quant_scale,
quant_scale_power, drift_threshold
```

因此批大小、排序、精度模式、量化缩放因子或漂移阈值改变，都会形成新配置版本。非法 JSON、未知枚举、非 10 的幂缩放因子等统一返回 `E_CONFIG`。

## 稳定批划分

- `sample_id`：按样本标识升序。
- `input_norm`：按 `math.fsum(x_i^2)` 升序；同范数保持原始输入顺序。
- `input_order`：保持样本文件顺序。

排序后按 `batch_size` 连续切分，批标识依次为 `B001`、`B002` 等。相同数据与配置会得到完全相同的批边界、批内顺序和样本位置。

每条样本始终走独立的、固定调用顺序的 NumPy 前向路径。这样同一配置下改变批大小或批内位置，不会因 BLAS 批形状差异改变输出或归因结果。

## 数值路径与误差界

### float32 参考路径

每一层逐样本执行 NumPy float32 乘加，隐藏层使用 ReLU，输出层为线性层。该路径同时作为定点路径的浮点参考。

### 定点路径

设缩放因子为 `S = 10^p`，整数格单位为 `1/S`：

```text
q(v) = sign(v) * floor(abs(S*v) + 0.5)          # 0.5 远离零舍入
A_j  = sum_i q(w_j,i) * q(x_i) + S*q(b_j)       # 有符号 int64 饱和
q(y_j) = round_half_away(A_j / S)               # 激活整数格饱和
```

隐藏层 `q(y)=max(0,q(y))`，输出层保留线性结果。定点权重量化、输入/激活量化、累加、偏置和重新量化全部在整数域完成。报告记录每层的 `accumulation_saturation_count` 和 `bounds_valid`。

### 整数格误差界

对第 `l` 层、扇入 `n`、缩放因子 `S`，报告以下整数格界：

```text
Local_l = ceil(max_j(0.5*sum_i|q(w_j,i)| + 0.5*sum_i|q(x_i)| + n/4) / S + 1)
Gain_l  = max_j sum_i|q(w_j,i)| / S
 Cumulative_l = ceil(Gain_l * Cumulative_(l-1) + Local_l)
```

推导依据：

- `q(x)=S*x+e_x`、`q(w)=S*w+e_w`，其中 `|e_x|<=0.5`、`|e_w|<=0.5`。
- 展开点积得到两个交叉项和 `n/4` 的尾项。
- 额外 `+1` 格覆盖偏置量化的 `0.5` 格和输出重新量化的 `0.5` 格。
- ReLU 是 1-Lipschitz 的，不放大误差；线性层的无穷范数增益为最大绝对权重行和。
- 上层累计误差乘 `Gain_l` 后与当前层局部误差相加并向上取整。

所有界均以整数格表示，`1` 格等于原始数值单位 `1/S`。每层的 `bound_inputs` 同时记录扇入、量化权重绝对行和、激活绝对和、传播增益等中间量，便于审计。

### 归因与漂移

对每条样本、每层：

```text
r_l = max|fixed_l - float_l| / max(max|float_l|, 1e-12)
d_l = r_l - r_(l-1)
increase_l = max(0, d_l)
share_l = increase_l / sum(increase)
```

若所有增量均为零但确有误差，则退化为按该层绝对误差归一化。报告输出：

- 每层相对偏差、局部/累计误差界、增量、归因占比和累计占比。
- 相对偏差最大的层 `max_relative_error_layer`。
- 归因最大的层 `dominant_layer` 及其占比。
- 输出端相对偏差 `output_relative_deviation`。

若任一层 `r_l > drift_threshold`，该批次标记 `E_DRIFT`，并记录按批内样本顺序、层顺序找到的首个超限样本和层。

## 版本整体替换与差异

`InferenceSession` 同一时刻只保留一个活动配置版本：

1. 新配置版本形成时，旧批次深拷贝后标记 `stale=True`，只保留为历史归档。
2. 当前批结果列表整体清空，不追加、不合并旧批结论。
3. 新版本从第 1 批开始完整执行。
4. 报告只统计当前版本批次；`stale_versions` 仅列出被替换版本及数量。
5. `version_transition` 稳定列出差异批次、样本增删、差异层、误差界变化方向和漂移变化方向。

同一配置重复运行时也先整体清空再重算，输出和诊断逐位一致。

## 快照、恢复与重放

快照包含：

- 数据摘要、参数摘要、规范化配置和配置版本。
- 完整批计划（批边界与样本顺序）。
- 已完成批次的完整输出、输出摘要、逐层局部/累计误差界和归因结果。
- `next_batch_index`、完成状态和整个快照的规范化摘要。

每完成一批后可通过 `--snapshot` 原子写入快照，因此中断时持久化的只有已完成批次。

恢复规则：

1. 先校验快照摘要、schema、配置版本和批次连续性。
2. 为被打开的快照创建不可覆盖的只读备份 `snapshots/backups/*.readonly.json`。
3. 无论当前是否已有批次，都先整体清空当前批次状态与结果缓存。
4. 比较快照与当前会话的数据摘要、参数摘要和配置版本。
5. 数据/参数摘要一致时加载已完成批次并继续；数据摘要不一致则进入 `E_DATA` 不兼容状态，不加载旧结论。
6. 配置版本与当前不同并不做增量拼接，而是采用快照配置版本整体替换。

重放校验会对已完成批次按快照配置和批计划重新推理，逐位比较：

- 每条样本 float32 输出与浮点参考输出。
- 定点输出整数、每层局部和累计误差界。
- 每层相对误差、归因占比和累计占比。
- 输出摘要与样本顺序。

首个不一致会定位到 `batch_index`、`sample_id`、`layer` 和具体字段，并返回 `E_REPLAY`。若调用方指定的当前配置与快照配置版本或量化缩放因子不一致，报告 `unreproducible_items` 明确列出不可复现项。

## 业务码与进程退出码

| 业务码 | 含义 | CLI 退出码 |
| --- | --- | --- |
| `E_DATA` | 数据/参数摘要不一致，整体拒绝推理 | 3 |
| `E_CONFIG` | 配置非法 | 2 |
| `E_DRIFT` | 某批次数值偏差超过阈值（批次标记，不中止整次推理） | 0 |
| `E_REPLAY` | 重放不一致或配置/量化参数不可复现 | 4 |
| `E_SNAPSHOT` | 快照损坏或结构非法 | 5 |
| `E_IO` | 本地文件读写错误 | 6 |

## 常用 CLI

```bat
python -m infer_workbench run --config configs/float32_b4.json --report reports/float.json
python -m infer_workbench run --config configs/fixed_q3_b3.json --stop-after 2 --snapshot snapshots/partial.json --report reports/partial.json
python -m infer_workbench restore --snapshot snapshots/partial.json --replay --continue-run --config configs/fixed_q3_b3.json --report reports/restored.json
python -m infer_workbench replay --snapshot snapshots/partial.json --expected-config configs/fixed_q3_b3.json --report reports/replay.json
```

`python -m infer_workbench --help` 可查看全部参数。

## 实际验证结果

验证环境：Python `3.11.0rc1`，NumPy `2.4.6`，Tk `8.6`。详细输出见 `artifacts/`。

- 核心测试：`20` 项中 `19` 项通过、`1` 项因当前 Linux 基础 Python 未安装 Tkinter 而跳过。
- 加载 Tk 8.6 组件后：`20/20` 项全部通过，包含真实 Tk 窗口构造与稳定列顺序检查。
- 预置数据重建摘要与落盘摘要一致：`data_digest=b123f81d...fbdecf`，`parameter_digest=3729aadc...7d7b5a`。
- float32 版本：`4d209fefe37b9461`，3 批全部完成，无定点诊断或漂移。
- fixed q=10^3 版本：`7f8bc68f91c83e13`，4 批；最大累计误差界按层约为 `5/30/90` 个整数格，未触发 `E_DRIFT`。
- fixed q=10^2 版本：`565544ee227761ab`，4 批全部触发 `E_DRIFT`；与 q=10^3 比较，差异批次为 `1,2,3,4`，三个层均变化，偏差方向为 `higher`。
- 部分快照恢复：当 q=10^3 已完成 `2/4` 批时写快照，切换到 q=10^2 后恢复，当前 q=10^2 结果被整体替换；只读备份生成成功。
- 重放校验：恢复后重放 `2` 批、`6` 条样本，输出、误差界和归因全部逐位一致；继续完成至 `4/4` 后再次重放 `4` 批、`12` 条样本，`first_mismatch=none`。
- 故障注入测试：手工修改一份快照中的第 2 批第 1 条样本 `hidden_1.cumulative_error_bound_grid` 后重新封装摘要，重放正确定位到批次 2、样本标识、层 `hidden_1`，返回 `E_REPLAY`。
- 摘要篡改：样本文件摘要不一致返回 `E_DATA`；非法批大小、排序、精度模式或缩放因子返回 `E_CONFIG`；损坏未重封装快照返回 `E_SNAPSHOT`。

### 确定性边界

- 定点路径依靠整数运算和显式整数舍入，跨平台一致性最强。
- float32 参考路径固定了逐样本调用顺序和 NumPy 归约顺序，但仍应在相同 Python/NumPy 版本和本地 CPU 环境内做逐位比较；本项目不承诺跨 BLAS/CPU 架构的浮点位级一致性。
- 报告中不使用时间戳作为排序或版本键；快照 ID 由配置版本和已完成批数构成，报告 JSON 使用稳定键序和稳定列表序。
