# FormBench 技术说明

FormBench 是一个纯本地、确定性的线下报名表单数字化工作台。运行时只依赖 `numpy`，账本使用标准库 `sqlite3`，测试使用 `pytest`；不使用 Docker、网络服务、Pillow 或 OpenCV。

## 目录与入口

- `seed_samples.py`：固定种子入口，等价于 `python -m formbench seed --root .`。
- `formbench/generator.py`：生成 7 张 PGM 灰度表单、逐块真值和 `samples/task.json`。
- `formbench/schema.py`：任务 JSON、真值清单和字段位置校验。
- `formbench/imaging.py`：无外部依赖的 PGM 读写、线段绘制和最近邻旋转。
- `formbench/glyphs.py`：确定性 3×5 笔画字形及词表模板构造。
- `formbench/detect.py`：表格线投影抑制、游程连通域、笔画密度聚类和整图坐标检测。
- `formbench/recognition.py`：整表统一缩放、栅格归一化和模板重叠打分。
- `formbench/scheduler.py`：纯函数轮次规划、可注入逻辑时钟、内存租约和有界线程池。
- `formbench/ledger.py`：SQLite 账本和规范化内容摘要。
- `formbench/persistence.py`：块级归一化中间量、JSON 完成标记和原子临时文件。
- `formbench/outputs.py`：版本标识感知的临时文件写入、替换和旧版本归档。
- `formbench/runner.py`：单图恢复、检测、调度、入账、一致性裁决和聚合。
- `scripts/verify_acceptance.py`：一键验收并写出 `reports/acceptance-report.json`。

## Windows 快速运行

```powershell
py -3.11 -m pip install numpy pytest
python seed_samples.py --root .
pytest -q
python scripts\verify_acceptance.py
python -m formbench run --task samples\task.json
```

批处理包含 3 张正常表单和 4 张异常注入表单，因此 `run` 的约定退出码为 `1`，表示“批次已完成但存在可区分的单图业务错误”。完整成功为 `0`，模式校验/未注入图像错误为 `2/3`，可处理终止为 `130`。

## 任务描述契约

生成的 `samples/task.json` 包含：

- `image_width`、`image_height`：所有样本的声明尺寸，当前为 `960×640`。
- `detection_threshold`：候选块最低笔画密度，必须在 `[0,1]`。
- `confidence_floor`：整表置信度下限，必须在 `[0,1]`。
- `chunk_grid.rows`、`chunk_grid.cols`：正整数，分别整除高、宽。
- `concurrency`：正整数线程上限。
- `memory_budget_bytes`：正整数并发像素缓冲预算。
- `round_logic_budget`：正数单轮逻辑时间预算。
- `vocabulary`：识别词表。
- `state_dir`、`output_dir`：账本、块中间量、中断标记和结果目录。
- `images[]`：图像标识、PGM 路径、真值路径和清单 SHA-256。
- `exception_injections[]`：四类确定性异常注入。

校验错误返回 JSON Pointer 风格字段位置，例如 `/chunk_grid/rows`、`/images/0/path`。任务 JSON 非法时 CLI 在创建状态/输出目录前退出；未在异常清单中声明的图像尺寸或真值错误也会进行只读预检并返回 `E_IMAGE`/`E_SCHEMA`，不产生输出文件。

## 检测与识别

检测始终在整幅图像上进行：

1. 用灰度截止值得到暗像素。
2. 按行/列暗像素比例寻找表格线投影带并抑制。
3. 用确定性行游程连通域去除孤立噪点。
4. 按字符间距和纵向重叠合并为文字块。
5. 用块内笔画密度和尺寸过滤偶然噪声。
6. 按 `(x, y, width, height)` 字典序编号输出。

`chunk_grid` 只用于合法性校验和结果中的 `grid_cells` 空间标注，不用于裁切检测，因此跨过网格线的标题仍输出为一个完整矩形。重复检测没有随机数、集合迭代顺序或文件顺序依赖，矩形集合完全相同。

归一化缩放比由同一张表所有候选块高度的中位数一次性求出，并与统一的 `NormalizationConfig`、同一词表模板集合一起传给每个块。单块识别函数不读取相邻块文本或调度结果。

## 预算调度

`plan_rounds()` 是纯函数，按块序号处理待办块，并依次检查：

- 轮内块数是否达到 `concurrency`；
- 已预留像素缓冲加下一块是否超过 `memory_budget_bytes`；
- 已计量逻辑成本加下一块是否超过 `round_logic_budget`。

如果单个块自身超过内存预算，返回 `E_MEMORY_BUDGET`；如果单个块逻辑成本超过轮预算，仍准入该块并在轮后结束本轮。相同输入、并发度和预算会得到相同的轮次数、每轮索引和准入顺序。

执行器使用 `ThreadPoolExecutor(max_workers=concurrency)`。每个块在提交前预留保守像素字节数，worker 的 `finally` 释放租约，`MemoryTracker.peak_bytes` 可验证峰值不超预算。任一块失败时，未提交轮次不再调度，已排队 future 被取消，运行中的块在安全返回后释放缓冲，执行器等待线程退出并把剩余块标记为 canceled。

`ManualClock` 是线程安全的可注入逻辑时钟；worker 按确定性的 `width*height/512` 逻辑成本推进时钟。生产 CLI 也默认使用它，因此调度报告可复现。

## 账本、持久化与恢复

状态目录布局：

```text
state/
  ledger.sqlite3
  INTERRUPTED.json
  images/<image_id>/
    version.json
    blocks/block-0000.json
    blocks/block-0000.normalized.bin
```

块先写同目录 `.tmp` 文件，再原子替换为 `.bin` 和 `.json`。其中 JSON 是“完成标记”；只有 `.tmp`、缺少 JSON 或摘要不符的块不会被误认为完成。

版本标识是以下规范化 JSON 的 SHA-256：图像 SHA-256、尺寸、检测阈值、网格、词表、归一化参数、预算参数和检测器/识别器版本。版本一致时读取有效 JSON/BIN 并把缺失账本记录补齐；版本不一致时删除该图块目录和账本行后整图重做。

SQLite 表主键为 `(image_id, block_index)`，从结构上防止同图同序号重复。每笔事务写入版本、块标识、文本、置信度、规范化摘要和 `content_digest`。内容摘要覆盖图像标识、序号、块标识、文本、置信度和规范化数组 SHA-256。只有账本中该图当前版本的块数等于检测块数且所有摘要校验通过，才生成整表结果。

## 信号与原子输出

CLI 安装 `SIGINT`、`SIGTERM`，Windows 上还安装 `SIGBREAK`。信号处理器只调用 `StopController.request_stop()`，不直接退出或写文件。调度器在轮次边界检查停止状态，已准入的并行块安全完成并入账，未开始块不再执行；随后写 `state/INTERRUPTED.json` 并以 `130` 退出。

整表结果、诊断和单图结果都先写到同目录唯一临时文件，`flush + fsync` 后通过 `os.replace()` 替换正式文件。替换前读取旧文件的 `version_id`：版本相同则保留旧文件并删除临时文件；版本不同则把旧文件归档到 `output/archive/` 后替换。

## 异常注入业务码

| 注入 | 稳定原因码 | 行为 |
| --- | --- | --- |
| `corrupt_pixels` | `E_CORRUPT_PIXELS` / `E_IMAGE` | SHA-256 与清单不符，不聚合该图 |
| `grid_not_divisible` | `E_GRID_NOT_DIVISIBLE` / `E_SCHEMA` | 注入的 3×7 网格不能整除 960×640 |
| `ledger_digest_mismatch` | `E_LEDGER_DIGEST` | 篡改指定行摘要并在一致性裁决时失败 |
| `vocabulary_missing` | `E_VOCAB_MISSING_INJECTED` / `E_VOCAB_MISSING` | 构造模板时删除指定词 |

其余正常图像继续处理并写出结果。
