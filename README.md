# 离线点云分块渲染与诊断工作台

这是一个不依赖网络、扫描设备、外部数据集或容器环境的本地 Python 工作台。它把固定种子的稀疏点云按多个相机视角渲染为灰度图与世界单位深度图，并提供分块缓存、局部替换失效闭包、确定性并发、中断恢复、摘要校验和本地交互诊断。

## 环境

- Windows 本地 Python 3.11 或以上
- NumPy 1.26 或以上
- Tkinter 为 Windows Python 标准组件；静态 HTML 诊断不依赖 Tkinter
- SQLite 未使用，缓存采用同目录临时文件加 `os.replace` 的原子 NPZ 文件

```powershell
python -m pip install "numpy>=1.26"
```

项目不需要安装为平台包，直接在仓库根目录用 `python -m pointcloud_workbench` 运行。若使用 `py` 启动器，把下文的 `python` 换成 `py -3.11`。

## 快速开始

生成固定种子样本：

```powershell
python -m pointcloud_workbench generate --out samples
```

先渲染基础点云，再对局部重扫描版本做失效补渲。两次运行共用同一个状态目录：

```powershell
python -m pointcloud_workbench render --task samples/task_base.json --state .workbench-state --output outputs/base --verify-full
python -m pointcloud_workbench render --task samples/task_edited.json --state .workbench-state --output outputs/edited --verify-full
```

打开本地诊断：

```powershell
python -m pointcloud_workbench view --output outputs/edited
python -m pointcloud_workbench view --output outputs/edited --gui
```

第一条命令重新生成单文件、无外部请求的 `outputs/edited/workbench.html`。第二条命令启动读取已持久化结果的 Tk 桌面视图。两者都显示灰度投影、深度预览、分块网格与失效高亮；选择分块可查看失效理由、受影响点、依赖点集和摘要校验结果。

运行全部测试和可重复验收：

```powershell
python -m unittest discover -s tests -t . -v
python -m pointcloud_workbench selftest --workdir artifacts/acceptance
```

## 关键行为

- `E_xxx` JSON 错误：非法 JSON、点数据、相机、视场角、宽高比、超采样、分块、预算、编辑清单等均在创建输出目录前被拒绝，错误从标准输出返回并由进程退出码区分。
- 世界坐标：`world = raw * world_scale + origin_offset`，半径使用同一尺度；深度图保存沿相机视线的世界单位深度，背景为 `inf`。
- 遮挡：每个超采样像素只保留世界深度最近的点；等深时按原始点索引决定。整体世界缩放不会改变可见性。
- 分块与并发：块使用整幅超采样坐标，块内独立分配采样缓冲。准入波次严格按分块索引，受线程数、内存预算和逻辑轮次预算限制。
- 编辑复用：每块持久化投影依赖点集。重扫描替换只失效依赖集与替换索引相交、或替换点新投影进入的块；其余块复用像素并安全迁移版本号。
- 恢复：块文件先写同目录临时文件再整体替换。启动时校验版本、像素数、依赖摘要和内容摘要；损坏或缺失块只补渲自身。
- 中断：`SIGINT`/`SIGTERM` 只在完整准入波次结束后生效，写入 `INTERRUPTED.json`，退出码 130，不生成最终输出或退出报告。
- 逐像素验证：`--verify-full` 会把当前分块结果与独立的串行整幅重算逐像素比较，灰度图和 PFM 深度图均要求完全一致。

## 目录

```text
pointcloud_workbench/   实现代码与 CLI
tests/                  56 个 unittest
samples/                固定种子样本（JSON + 二进制）
docs/                   架构、格式、验证说明
artifacts/acceptance/   本次实际验收报告与本地诊断输出
```

进一步阅读：

- [架构与确定性说明](docs/architecture.md)
- [输入输出格式与错误码](docs/formats.md)
- [实际验证结果](docs/validation.md)

