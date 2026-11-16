# DDPLAB：本地数据并行检查点与恢复一致性验证工具

面向 Windows 本地 Python 3.10+ 的确定性训练实验。项目使用固定随机种子生成合成二分类数据与小型 MLP，在 4 个逻辑工作进程上执行数据并行训练；每个 worker 持有自己的参数副本、优化器动量、数据置换/游标和随机数状态。工具支持原子多分片检查点、格式版本迁移、配置演进兼容判定、恢复续训逐位比较，以及双次恢复重放校验。

不依赖 Docker、GPU 或网络。运行时只使用 Python 标准库、NumPy 与官方 Python 自带的 Tkinter。

## 快速开始（Windows）

```powershell
cd <project-root>
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

# 环境、磁盘原子重命名、初始化摘要预检
python -m ddp_ckpt_lab check

# 三场景重复演示（默认重建 artifacts/demo_run）
python -m ddp_ckpt_lab.demo

# 打开 Tkinter 界面
python -m ddp_ckpt_lab
```

Windows 用户也可直接双击或运行：

- `run_check.bat`
- `run_demo.bat`
- `run_ui.bat`

演示输出目录 `artifacts/demo_run` 带专用工作区标记，只有带该标记的目录会自动重建。也可显式指定：

```powershell
python -m ddp_ckpt_lab.demo --config configs/demo.json --output artifacts/my_demo
python -m ddp_ckpt_lab ui --config configs/demo.json --output artifacts/ui_session
```

运行界面或写检查点前，应确认没有另一个实例正在使用同一输出目录。程序不会访问网络，也不会写入配置之外的路径。

## 配置语义

`configs/demo.json` 的所有训练参数均为整数：

| 字段 | 含义 |
| --- | --- |
| `seed` | 模型初始化及部分数据种子 |
| `data_seed` | 合成数据与 worker 数据流种子 |
| `samples` / `features` | 样本数、特征维度，必须能被全局批大小整除 |
| `hidden_dim` | MLP 隐藏层维度 |
| `workers` | 固定为 4 个逻辑 worker |
| `batch_size` | 全局批大小，必须能被 4 整除；每 worker 使用 `batch_size / 4` 个样本 |
| `train_steps` | 未中断训练总步数 |
| `learning_rate` | 整数千分位学习率，例如 `40` 表示 `0.040` |
| `momentum` | 整数千分位动量系数，例如 `900` 表示 `0.900` |
| `sharding` | `modulo` 或 `contiguous` 数据分片方式 |
| `checkpoint_every` | 演示建检查点的步间隔 |

数据、初始参数和每个 worker 的批次流都由整数种子完全决定。初始化阶段会生成两次数据、参数、批次计划和初始状态摘要并逐项比较；不一致返回 `E_INIT`。

## 训练与检查点设计

- 每个 worker 用本地批次计算交叉熵梯度；聚合时严格按 `worker_id=0,1,2,3` 顺序求和后除以 4，不依赖线程/进程调度。
- 每个 worker 独立保存当前 epoch 置换、消费位置、PCG64 随机数状态、参数与动量。
- 每步产生链式 `progress_digest`，其输入包含前一步摘要、全局步数和当前完整状态摘要；步数严格递增，摘要每步变化。
- v2 目录结构：

```text
checkpoint_00000006/
  COMPLETE                  # manifest SHA-256，完整提交标记
  manifest.json             # 格式版本、配置、分片摘要、RNG、游标、全局步数
  shards/worker_0.npz
  shards/worker_1.npz
  shards/worker_2.npz
  shards/worker_3.npz
```

- 写入先落在同盘同级 `.tmp-checkpoint_*` 目录，全部文件 `fsync`，最后写 `COMPLETE`，再以 `os.replace` 原子替换为最终目录。
- 崩溃遗留的 `.tmp-*` 或缺少完成标记的目录不会被列为可恢复检查点；严格扫描返回 `E_CHECKPOINT`。
- 每个分片摘要覆盖数组名、dtype、shape 和 C-order 原始字节；清单另记录配置摘要、worker 内容摘要和整体状态摘要。
- 恢复顺序为：核验完成标记 → 核验清单/分片摘要 → 重建并校验全部 worker 状态 → 再做配置兼容判定。分片或摘要不一致返回 `E_RESTORE` 并拒绝恢复。

## 配置演进规则

恢复一律采用 `replace_rebuild`：丢弃已有运行时状态对象，从清单与分片重新创建四个 worker 的参数、动量、RNG 和游标；不会把新旧字段增量合并。

| 变化 | 判定 | 处理 |
| --- | --- | --- |
| `learning_rate` 变化 | 可兼容迁移 | 保留参数/动量/游标，未来更新使用新学习率，报告迁移步骤 |
| `train_steps` / `checkpoint_every` 变化 | 运行时参数 | 可恢复，不影响已有训练状态 |
| 检查点格式版本 `v1 -> v2` | 可迁移 | 识别 v1 字段与摘要口径，全量重建为 v2 状态 |
| `hidden_dim` / `features` / `samples` 变化 | 不可恢复 | `E_CONFIG`，列出冲突字段 |
| `sharding` / `batch_size` / `workers` 变化 | 不可恢复 | 数据流或形状语义已变化，`E_CONFIG` |
| `seed` / `data_seed` 变化 | 不可恢复 | 初始数据或参数来源不同，`E_CONFIG` |
| `momentum` 变化 | 不可恢复 | 优化器状态语义变化，`E_CONFIG` |

同一配置下，从检查点恢复后继续训练会与不中断训练逐位比较参数、动量、数据游标、RNG 状态与全局步数。首个差异定位到 `step`、`worker_id`、`parameter_name` 与 `state_kind`，并返回 `E_DIVERGE`。

重放校验会对同一检查点连续恢复两次并执行相同步数，比较逐步与最终状态摘要；同时验证原始检查点文件指纹未变化。配置或分片方式导致不能复现时返回 `E_REPLAY`。

## 业务码

| 业务码 | 触发条件 |
| --- | --- |
| `E_INIT` | 数据/参数/批次计划摘要重复生成不一致，或与报告期望不符 |
| `E_CHECKPOINT` | 原子写入失败、检查点已存在、临时残留、完成标记/清单损坏 |
| `E_RESTORE` | 格式不支持、分片缺失/损坏、摘要不符、游标/RNG 非法 |
| `E_CONFIG` | 模型维度、优化器非学习率参数、种子或分片方式冲突 |
| `E_DIVERGE` | 恢复续训与不中断训练出现首个逐位差异 |
| `E_REPLAY` | 两次恢复重放不一致、配置/分片不可复现，或原检查点被修改 |

## Tkinter 界面

界面固定按 worker ID、检查点步数/格式/ID 排序，展示：

- 4 个 worker 的参数摘要、动量摘要、epoch/游标/置换长度、worker 状态摘要；
- 检查点列表与格式版本、状态摘要、清单摘要；
- 当前全局步数、配置摘要、数据/初始参数摘要；
- 冷启动建检查点、恢复续训一致性、配置与格式迁移、重复重放、差异定位结果。

界面操作均在本地同步执行，训练规模很小，不会启动隐藏后台进程。每个 UI 输出目录使用跨平台非阻塞文件锁；第二个界面实例会在修改目录前被拒绝，避免并发重置同一工作区。

## 测试与验证

```powershell
python -m unittest discover -s tests -v
python -m ddp_ckpt_lab check
python -m ddp_ckpt_lab.demo
```

测试覆盖 23 项，包括：初始化/训练逐位确定性、进度摘要变化、v2 原子写入、重复检查点保护、写入失败清理、临时残留 `E_CHECKPOINT`、分片损坏 `E_RESTORE`、v1→v2 全量重建、学习率迁移、维度/分片/动量冲突、恢复续训等价、首个动量的差异定位、重放复现与原文件不变。

冷启动演示按 `checkpoint_every=6` 自动创建步数 6、12、18 的三个 v2 检查点，并额外生成一个 v1 检查点用于格式迁移验证。当前工作区已实际生成：

- `artifacts/demo_run/summary.json`：三场景稳定总结与六类业务码结果；
- `artifacts/demo_run/reports/*.json`：初始化、基线训练、续训、配置演进、重放、负向探测报告；
- `artifacts/test-results.txt`：完整单元测试运行记录；
- `artifacts/preflight.json`：Python/NumPy、存储与初始化预检结果；
- `artifacts/ui-smoke.txt`：真实 Tk 8.6 窗口构建、检查点列表渲染与正常退出记录。

本次验证使用的确定性演示结果可在 `artifacts/demo_run/summary.json` 中复查。当前验证容器为 Linux，NumPy 核心与文件系统路径已实际执行；系统 Tkinter 缺失，验证时使用离线 Tk 8.6 组件成功创建真实窗口、渲染 6/12/18 三个检查点并正常退出。Windows 官方 Python 环境仍需执行一次 `python -m ddp_ckpt_lab` 做本机界面与中文字体验收。

## 项目结构

```text
ddp_ckpt_lab/
  canonical.py        # 规范 JSON、数组摘要、RNG 状态
  config.py           # 整数训练配置与校验
  data.py             # 合成数据、分片、可恢复 worker 数据流
  model.py            # MLP、交叉熵、SGD + momentum
  state.py            # 4-worker 运行时状态与状态摘要
  training.py         # 固定顺序梯度聚合训练循环、E_INIT
  compatibility.py    # 显式配置迁移规则
  checkpoints.py      # 原子检查点、v1/v2 加载、E_RESTORE/E_CHECKPOINT
  verification.py     # 续训逐位比较、首个差异、重放
  reports.py          # 稳定 JSON 与文本总结
  demo.py             # 三场景演示与负向业务码自检
  ui.py               # Tkinter 界面
  cli.py / __main__.py
configs/demo.json
tests/
artifacts/demo_run/
```
