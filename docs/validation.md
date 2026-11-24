# 实际验证结果

## 环境

本次验证在工程工作区内的 Linux/WSL 环境执行。目标运行环境仍是 Windows Python 3.11+；实现只使用 Python 标准库、NumPy 和可选 Tkinter，不包含平台专用服务或容器配置。

| 验证运行时 | NumPy | 结果 |
|---|---|---|
| CPython 3.12.13 | 2.5.5 | 56 项测试通过，Tk 视图实际启动 |
| CPython 3.10.12 | 1.26.4 | 56 项测试通过，Tk 项因该解释器未带 Tkinter 跳过 |

主验收使用 CPython 3.12.13 + NumPy 2.5.5。另用 NumPy 1.26.4 跑同一套测试，覆盖项目声明的最低 NumPy API 组合。

## 测试命令

```powershell
python -m unittest discover -s tests -t . -v
python -m pointcloud_workbench selftest --workdir artifacts/acceptance
```

## 自动验收结果

验收样本由固定种子 `20261125` 生成：

- 点数：6144
- 相机：3
- 分辨率：96 × 64
- 超采样：2
- 分块：16 × 16，共 24 块/相机
- 编辑区域：3 段，共 92 个点索引
- 基础数据集摘要：`73ad984a8bf19424eaa90e7c4ea893461209dc144b9e75ccc67e7b6038f9717c`
- 编辑后数据集摘要：`5589529d8b03d759788dc1adbaff64317ae823959b393f7c37054f2107fe7ecb`

| 验收项 | 结果 | 实测数据 |
|---|---|---|
| 基础渲染 vs 串行整幅重算 | PASS | 3/3 相机灰度与 PFM 深度逐像素一致 |
| 局部替换失效补渲 vs 整幅重算 | PASS | 补渲 12/9/7 块，复用 12/15/17 块，全部逐像素一致 |
| 编辑增量结果 vs 全新状态整幅结果 | PASS | 3/3 相机灰度/深度 0 个不一致像素 |
| 并发度 1 vs 并发度 4 | PASS | 3/3 相机灰度/深度 0 个不一致像素 |
| 中断边界行为 | PASS | 退出码 130、标记存在、输出目录不存在、已持久化 4 块 |
| 中断恢复 | PASS | 复用 4 块，继续完成，标记清除，输出逐像素一致 |
| 最终摘要修复 | PASS | 仅 `cam_front/b0010` 被定向补渲，输出逐像素一致 |

完整机器可读结果在 `artifacts/acceptance/acceptance_report.json`，人类可读摘要在 `artifacts/acceptance/VALIDATION.md`。可直接浏览：

- `artifacts/acceptance/out_edited/workbench.html`
- `artifacts/acceptance/out_edited/render_report.json`
- `artifacts/acceptance/out_edited/cam_front/grayscale.png`
- `artifacts/acceptance/out_edited/cam_front/depth.pfm`

## 覆盖的关键断言

测试套件覆盖：

- 非法 JSON、UTF-8、坐标、灰度、法线、世界尺度、原点、视场角、宽高比、超采样、分块和预算错误码，且失败不创建输出目录。
- 上方向与视线平行返回 `E_CAMERA`。
- 有限正数的分数超采样倍率可渲染，并与独立串行重算一致。
- 点云、半径和相机整体同比缩放时灰度逐像素一致，深度同比缩放。
- 调度波次严格按索引、线程上限、内存预算、逻辑轮次预算和停止边界执行。
- 注入块错误时返回首个按索引排序的错误且不提交待处理块。
- 编辑前依赖块与替换索引、替换点新覆盖的闭包；未失效块可复用。
- 缺失、内容篡改、像素数篡改、最终阶段损坏、不可修复摘要 `E_DIGEST`。
- 未登记版本变化导致整幅重做。
- HTML 诊断无外部 URL，Tk 诊断只读取已持久化文件。
- 固定种子样本两次生成逐字节一致。

## 结论

本地固定种子样本验证通过：分块渲染与串行整幅重算逐像素一致；局部替换只补渲依赖失效块，结果与整幅重算逐像素一致；中断仅发生在块边界且可恢复；摘要损坏可被检出并只修复对应块。

