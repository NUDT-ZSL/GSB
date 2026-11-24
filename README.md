# 分块医学影像分割与掩码编辑工作台

本仓库在无外部数据集、无 Docker、无运行时网络依赖的条件下，提供一条可在 Windows 本地运行的确定性验证链路：

1. 固定种子生成体数据切片、真值掩码和任务 JSON。
2. 校验任务与切片，错误返回 `E_xxx` JSON 业务码。
3. 在整幅坐标系中完成区域生长、跨块邻域取值和形态学补边。
4. 按分块网格、并行度、像素缓冲预算和逻辑时间预算进行确定性调度。
5. 原子持久化分块、摘要和版本；支持复用、归档、`E_DIGEST` 回退补算和中断恢复。
6. 记录 raw 生长掩码修订与 WYSIWYG 最终掩码修订的依赖，只失效实际受影响块。
7. 提供 PyQt5 诊断视图和原子导出。

原始任务说明保留在 `docs/REQUIREMENTS.md`，实际验证记录见 `docs/VALIDATION.md`。

## Windows 环境准备

建议使用 Python 3.11。在线安装：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install numpy==1.26.4 PyQt5==5.15.10
```

若使用本地 wheel 目录：

```powershell
python -m pip install --no-index --find-links .\wheels numpy==1.26.4 PyQt5==5.15.10
```

所有路径均通过 `pathlib.Path` 处理；状态目录和临时目录会在正式运行前创建并做可写探测。

## 快速开始

```powershell
python -m medseg_workbench generate-sample --out samples\demo --force
python -m medseg_workbench validate --task samples\demo\task.json
python -m medseg_workbench run --task samples\demo\task.json
python -m medseg_workbench gui samples\demo\task.json
```

命令行修订与增量重算：

```powershell
# 默认修订形态学后的最终掩码，属于 WYSIWYG 权威修订
python -m medseg_workbench revise --task samples\demo\task.json --chunk 001_001_001 --demo
python -m medseg_workbench run --task samples\demo\task.json

# 也支持修订形态学前的区域生长掩码，由形态学依赖传播
python -m medseg_workbench revise --task samples\demo\task.json --chunk 001_001_001 --layer raw --demo
python -m medseg_workbench run --task samples\demo\task.json

python -m medseg_workbench evaluate --task samples\demo\task.json
python -m medseg_workbench export --task samples\demo\task.json
```

一键验收：

```powershell
python -m medseg_workbench.acceptance --out artifacts\acceptance
python -m unittest discover -s tests -v
```

## 关键设计

- 分块采用半开区间 `[z0,z1)`。区域生长使用全局对齐的确定性种子网格，并按 `growth_radius` 读取跨块上下文。
- 形态学总半径为各次膨胀/腐蚀半径之和。分块先在整幅坐标盒中补够 halo，再计算并裁回核心，因此与整幅串行函数同源。
- 调度计划固定按 `z,y,x` 排序；每轮准入同时满足 `parallelism` 和保守像素缓冲槽位预算，逻辑时钟可注入，轮次和准入序列可复现。
- 每个 `chunks/<id>.npz` 原子包含 `mask`、`raw`、`chunk_digest`、`version_id` 和 `dependency_json`。写入方式为同目录临时文件、`fsync`、`os.replace`。
- `version_id` 是体数据内容摘要与全部规范化参数的摘要。版本变化时旧分块移动到 `state/archive/<old_version>/`，不会直接覆盖。
- 摘要损坏或形状不符返回 `E_DIGEST`，运行报告记录该码并自动补算。
- 可处理信号默认使用约定退出码 `130`，调度器在当前已准入分块到达边界后写 `state/INTERRUPTED.json`。`kill -9` 等强制终止不承诺报告，重启只承认存在且摘要有效的 `.npz`。
- 真值目录只在 `medseg_workbench/evaluation.py` 的评估函数中读取；分割、调度和失效判定不读取真值。

## 修订一致性语义

系统区分两层修订：

- `final`：界面拖拽和 CLI 默认层。编辑形态学后的导出掩码；全量参考在整幅分割完成后应用同一份权威编辑。
- `raw`：形态学前的生长掩码修正；全量参考先应用 raw 修正，再执行同一套形态学。

依赖图记录 `final:<chunk> -> raw:<provider>` 的补边关系。只有当实际修订体素落入某依赖的补边区间时，相关块才进入 `stale_chunk_ids`。重算后的分块记录已纳入的修订摘要；再次启动会复用，而不是永久失效。

增量结果与“相同体数据、相同参数、相同修订集合”的从头全量参考逐体素一致；自动化测试覆盖多种形状、分块、并行度、生长半径和核尺寸。

## 桌面视图

`python -m medseg_workbench gui [task.json]` 提供：

- 切片灰度与掩码红色叠加；
- 分块网格、失效块黄色高亮、选中块蓝色高亮；
- 逐块状态、修订标记和 `E_DIGEST` 标记；
- 选中块的修订历史、数据区间、阈值、形态学参数和相邻补边来源；
- Dice/IoU、失效统计、轮次信息；
- 左键拖动置 1、右键拖动置 0；
- 评估报告和掩码/报告的版本化原子导出。

Linux 无显示器环境可使用 `QT_QPA_PLATFORM=offscreen` 运行界面冒烟测试。
