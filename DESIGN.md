# 本地可微渲染离线管线 — 技术说明

仅依赖 Python 3.10+ 标准库与 NumPy 1.x（实测 `numpy==1.26.4`）。不使用任何
自动微分框架、渲染引擎、GPU、容器或外部数据；目标图像由确定性程序在本地生成。

## 1. 运行环境

```bash
# 在项目根目录创建虚拟环境并安装 NumPy 1.x（不使用容器）
uv venv .venv --python python3
uv pip install --python .venv/bin/python "numpy<2"

# 生成确定性样本描述
.venv/bin/python scripts/make_sample.py samples/sample.json

# 运行拟合（并行分块）
.venv/bin/python -m diff_render.cli samples/sample.json

# 串行参考路径 / 强制重新开始 / 严格摘要模式
.venv/bin/python -m diff_render.cli --serial samples/sample.json
.venv/bin/python -m diff_render.cli --restart samples/sample.json
.venv/bin/python -m diff_render.cli --strict-digest samples/sample.json
```

退出码：`0` 成功；`2` `E_SCHEMA`；`3` `E_CONVERGE`；`4` `E_DIGEST`；
`5` `E_WRITE`；`6` `E_TILE`；`130`/`143` 处理了 SIGINT/SIGTERM。

## 2. 模块结构与职责

| 模块 | 职责 |
| --- | --- |
| `diff_render/schema.py` | JSON 描述解析与严格校验，非法输入抛 `E_SCHEMA` |
| `diff_render/scene.py` | 确定性高度场法线、正交相机、规则超采样点 |
| `diff_render/shading.py` | 微表面前向着色 + 手写解析反向传播 + 数值差分参考 |
| `diff_render/target.py` | 在材质真值处渲染目标图，可缓存为 `.npy` |
| `diff_render/scheduler.py` | 行主序分块、有界线程池、内存预算、首错取消 |
| `diff_render/optimizer.py` | SGD 更新、定义域投影、双阈值收敛、梯度一致性 |
| `diff_render/digest.py` | 规范化 SHA-256 版本标识与状态载荷摘要 |
| `diff_render/state.py` | 检查点原子写、版本归档、回退恢复、中断标记 |
| `diff_render/io_utils.py` | 同目录临时文件 + `os.replace` 原子写出 |
| `diff_render/report.py` | 拟合报告与最终结果原子写出（版本不同才替换） |
| `diff_render/pipeline.py` | 端到端编排、恢复、信号接入 |
| `diff_render/cli.py` | 命令行、信号处理器（仅迭代边界生效） |

## 3. 着色模型与解析梯度

正交相机沿 -Z 观察高度场，法线由高度函数中心差分解析得到。每个采样点在两盏
方向光下计算：

- 漫反射：`Lambert = albedo/pi * max(N·L, 0)`
- 高光：GGX/Trowbridge-Reitz 法线分布 `D`、Smith(Schlick-GGX) 几何 `G`、
  Fresnel-Schlick `F`，`specular = D G F / (4 NoL NoV)`
- 损失：`L = sum_pixels sum_channels (render - target)^2`

反向传播完全由解析链式法则实现（见 `shading.py::_backward_light`），关键导数包括
`dD/dα`（含 `denom = 1 + NoH²(α²-1)` 的链式项）与 Smith 几何中
`dG1/dk = -x(1-x)/(x(1-k)+k)²`，再经 `α = roughness²` 传回粗糙度。数值差分
（中心差分，步长由描述显式给出）仅用于一致性校验，不参与训练。

梯度比较支持 `absolute`、`relative`、`absolute_or_relative` 三种模式；后者在
梯度接近零（真值点）时自动改用绝对偏差，避免相对误差被小分母放大。

## 4. 确定性与可复现

- 全程 float64，仅用逐元素 ufunc 与定序归约；像素聚合用 `np.add.at`（与归约顺序
  无关），分块结果严格按瓦片下标序累加，绝不按线程完成序。
- 线程只产出互不重叠的瓦片偏导，驱动端在主线程做定序求和，因此 `max_workers`
  取 1/2/4/8 时损失序列与参数轨迹**逐位相同**（测试 `test_concurrency_trajectory_bitwise_identical`）。
- 几何用闭式规则与固定网格，不调用任何随机数生成器。

## 5. 收敛、投影与诊断

- 双阈值：窗口内损失相对变化（带 `loss_abs_tol` 绝对下限）与梯度范数同时满足。
- 更新后若参数越界，按 `clamp` 投影回 `albedo∈[0,1]`、`roughness∈[1e-3,1]`，
  每次投影记录参数名、迭代号、上下界与投影前后数值。
- 到达迭代预算仍未收敛返回 `E_CONVERGE`，报告保留最后 `tail_size` 轮损失与梯度范数。

## 6. 分块并行与内存预算

- 瓦片按行主序生成；`ThreadPoolExecutor` 配 `BoundedSemaphore` 限制在途瓦片数，
  在途采样/梯度缓冲按保守字节估算且总量不得超过 `buffer_byte_budget`。
- 单个瓦片工作集超过预算立即 `E_TILE`；任一瓦片异常时取消其余待处理瓦片并返回
  首个错误（`test_tile_failure_cancels_and_returns_first_error`）。
- 串行与并行结果逐位一致（`test_serial_parallel_bitwise_identical`）。

## 7. 状态持久化、版本与中断恢复

- 版本摘要覆盖目标图字节、相机、材质（真值/学习率/投影）、灯光、影响数值轨迹的
  优化阈值与梯度校验参数；`iterations`、`checkpoint_every`、`tail_size` 等不改变
  单步数学的字段不纳入摘要，因此改迭代预算仍识别为同一版本并续跑。
- 检查点 `state.json` 经同目录临时文件 + `os.replace` 落盘；同版本旧状态转入
  `state.backup.json`，版本变化转入 `state.archive.json`，绝不直接覆盖旧版本。
- 每个信封含外层版本摘要与内层载荷摘要；载荷被篡改/半写导致内层摘要失效时，
  依次回退同版本备份、归档版本。默认容错回退到全新开始，`--strict-digest` 时
  严格返回 `E_DIGEST`。
- SIGINT/SIGTERM 仅在迭代边界被观察：写 `interrupt.json`、以 130/143 退出，
  未完成迭代不会留下“已完成”状态；重启后消费中断标记并从最近有效检查点继续，
  结果与一次跑完逐位一致。

## 8. 报告与原子结果写出

- `out/run/report.json`：每轮损失/梯度范数、最终参数与真值偏差、解析 vs 数值
  梯度最大偏差、投影次数与事件、收敛判定、调度与内存统计、版本摘要。
- `out/run/fit_result.json`：最终参数与损失序列。
- 两者均先写同目录临时文件再整体替换；已存在且字节相同则不替换，写失败保留旧
  文件并返回 `E_WRITE`。

## 9. 实测验证结果

```text
.venv/bin/python -m unittest discover -s tests
Ran 14 tests in ~3.3s
OK

.venv/bin/python tests/manual_resume_demo.py
[1] independent fresh runs are bitwise identical
[2] partial run persisted a valid checkpoint at iteration 20
[3] resume reproduces the full-run result exactly
OK
```

样本（24×24、4 超采样、两盏灯）端到端结果：

- 58 轮收敛，最终参数 `albedo≈0.54999`、`roughness≈0.25000`，真值为
  `(0.55, 0.25)`，参数偏差约 `8.5e-6 / 1.9e-6`。
- 解析梯度与中心差分最大绝对偏差约 `1.7e-10`，远严于描述容差 `1e-6`。
- 串行/并行及并发度 1/2/4/8 的损失与参数轨迹逐位相同。
- SIGTERM 退出码 143、中断标记写入、恢复后与完整运行一致。
- 12 类非法描述均返回 `E_SCHEMA` 且不创建任何输出目录/文件。
