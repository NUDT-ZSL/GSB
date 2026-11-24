# 验证记录

验证环境：Linux 容器（等价行为在 Windows 本地 Python 3.11 上运行，仅路径与信号发送方式不同）、
Python 3.10.12、NumPy 1.26.4。代码语法兼容 3.10+，任务要求的 3.11+ 可直接运行。
验证日期：2026-11-25（Asia/Shanghai）。

## 1. 样本生成与 JSON 可解析

`python generate_samples.py` 使用固定种子 `20261125` 确定性产出：

- `samples/level.json`：48×36 格网，6 个不重叠房间（269 格）、91 个走廊格、8 个门、
  墙厚 2、光照半径 14.0、5 个单调不减灰度控制点。
- `samples/render_task.json`：384×288 输出、每格 8 像素、12×9 分块（108 块）、
  并发 3、在途上限 4 块、单轮预算约 8 块、容差 0。
- `samples/sample_manifest.json`：样本统计摘要。

三份文件均通过 `json.load` 解析。

## 2. 单元测试

`python -m unittest discover -s tests` 结果：**32/32 通过，OK**。

覆盖范围：

- `test_validation.py`（11 项）：宽高正整数、越界/重复格、门邻墙规则、墙厚上限、
  光照半径有限正、调色板单调且覆盖 [0,1]、分块完整覆盖、内存下限、布尔值拒绝。
- `test_raster.py`（5 项）：分块拼装与单块整幅逐像素一致、重复渲染确定性、uint8 量化、
  非整格对齐分块无缝、门开口两侧一致。
- `test_scheduler.py`（6 项）：轮次预算装箱、并发无关规划、每块恰好一次有序执行、
  在途像素门控、并发槽限制、停止事件仅在边界生效。
- `test_persistence.py`（4 项）：块往返与摘要、损坏抛 `E_DIGEST`、旧版本归档不覆盖、PGM 头往返。
- `test_pipeline.py`（6 项）：整幅参照一致、二次运行全复用、跨并发像素与计划一致、
  中断标记与恢复、摘要失效自愈、`E_SEAM` 首个不一致定位。

## 3. 端到端验收

`python run_acceptance.py` 结果：**14/14 PASS**。关键观测：

- 拼装灰度图与描边图相对整幅单块参照：`gray_max_delta=0`、`outline_max_delta=0`。
- 全部 108 块块缝指标为 0，无亮度台阶或错位描边。
- 并发 1/2/4 下轮次规划完全相同，像素完全一致；非对齐分块（10×7，块宽非格像素整数倍）仍逐像素一致。
- 在途像素上限收紧到单块（1024）时仍精确完成，证明双预算联合裁决生效。
- 人为篡改块数组后重跑：报告记录 `E_DIGEST`，失效块补渲后整幅偏差回到 0。
- 注入停止事件：退出码 130、写 `interrupted.json`、无正式图像产出；重启后只渲缺失块并清除标记。
- 修改光照半径产生新版本：旧状态被移动到 `archived_versions/<旧摘要>/` 后才写新块。
- 发布的 PGM 头含尺寸 384×288 与版本摘要；同版本重跑不替换文件，版本变化才替换。

## 4. 信号与错误码（命令行实测）

- 真实 `SIGINT`：退出码 130，状态目录保留中断标记且无半成品输出，重启恢复后退出码 0。
- `SIGTERM`（强制终止代表）：Python 默认终止（-15），重启仅依赖摘要有效块恢复，不产生退出报告。
- 非法任务（分块不整除分辨率）与非法关卡（重复走廊格）：退出码 2，stderr 为带 `E_SCHEMA` 的 JSON，
  状态/输出目录均未创建，无任何图像产出。

## 5. 复现实验命令

```bash
python generate_samples.py
python -m unittest discover -s tests
python run_acceptance.py
python -m dungeon_render.cli samples/level.json samples/render_task.json
```
