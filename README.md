# Glyph Raster

一个从零实现的本地、确定性、增量式矢量字形轮廓光栅化引擎。输入 JSON 场景与轮廓文件，输出带抗锯齿的 8 位灰度 PGM，并保存可分块恢复的持久化状态。

实现不依赖 GPU、字体文件或网络，运行时只使用 Python 3.10+ 与 NumPy。

## 快速开始

环境检查：

```bash
python3 --version
python3 -c 'import numpy; print(numpy.__version__)'
```

生成样本、执行基线渲染、应用编辑并做全量对照：

```bash
python3 tools/generate_samples.py
python3 tools/run_sample.py baseline
python3 tools/run_sample.py incremental
python3 tools/run_sample.py verify
```

运行完整测试：

```bash
python3 tools/run_sample.py test
```

`tools/run_sample.py` 只调用本地 Python 与命令行入口，不依赖 `make`、Docker 或外部服务：

| 命令 | 用途 |
|---|---|
| `python3 tools/run_sample.py sample` | 生成确定性样本 |
| `python3 tools/run_sample.py reset` | 清理样本状态与输出 |
| `python3 tools/run_sample.py baseline` | 清空生成物、生成样本并渲染编辑前基线 |
| `python3 tools/run_sample.py incremental` | 基线完成后应用编辑脚本做增量渲染 |
| `python3 tools/run_sample.py resume` | 以编辑后目标恢复中断渲染 |
| `python3 tools/run_sample.py resume-base` | 以编辑前目标恢复中断渲染 |
| `python3 tools/run_sample.py verify` | 校验分块、位图、报告并做串行全量对照 |
| `python3 tools/run_sample.py verify-base` | 校验基线版本 |
| `python3 tools/run_sample.py test` | 运行全部自动化测试 |
| `python3 tools/run_sample.py demo` | 基线、增量、全量校验一条龙 |

## CLI

```bash
python3 -m glyph_raster render  --scene samples/scene.json [--base] [--workspace .]
python3 -m glyph_raster resume  --scene samples/scene.json [--base] [--workspace .]
python3 -m glyph_raster verify  --scene samples/scene.json [--base] --full-check [--workspace .]
```

- `render`/`resume` 默认应用场景中的 `edits`；`--base` 或 `--no-edits` 渲染编辑前轮廓。
- `--apply-edits` 仍可作为显式写法使用。
- 所有结果与错误均向标准输出写单行 JSON。
- `outlineFile` 相对路径优先按场景文件所在目录解析，找不到时再按工作区解析。
- 场景中的 `stateDir`、`output`、`report` 相对路径按 `--workspace` 解析，并且必须最终位于工作区内。

## 输入格式

完整可运行示例见 `samples/scene.json` 与 `samples/glyphs.json`。

场景：

```json
{
  "schema": "glyph-raster-scene/v1",
  "outlineFile": "samples/glyphs.json",
  "font": {"unitsPerEm": 1000.0, "baselineOffset": 40.0},
  "layout": {"marginX": 18.0, "spacing": 8.0},
  "resolution": {"width": 720, "height": 240},
  "supersampling": 4,
  "tile": {"width": 48, "height": 48},
  "concurrency": 4,
  "memoryLimitBytes": 110592,
  "stateDir": "state/sample",
  "output": "out/sample.pgm",
  "report": "out/sample-report.json",
  "edits": [
    {"op": "set_point", "glyph": "G1", "point": 3, "x": 600.0, "y": 100.0},
    {"op": "set_size", "glyph": "G4", "size": 137.5},
    {"op": "set_x_offset", "glyph": "G0", "xOffset": 0.4375}
  ]
}
```

`resolution` 也可写成 `[width, height]`，`tile` 字段也接受别名 `tileSize`。

字形文件：

```json
{
  "schema": "glyph-outlines/v1",
  "glyphs": [
    {
      "id": "G0",
      "advance": 700.0,
      "size": 118.0,
      "xOffset": 0.0,
      "points": [[0.0, 0.0], [100.0, 0.0]],
      "contours": [
        {
          "start": 0,
          "closed": true,
          "segments": [
            {"type": "line", "to": 1},
            {"type": "quadratic", "control": 2, "to": 0}
          ]
        }
      ]
    }
  ]
}
```

- 每个字形必须至少有一个闭合轮廓。
- `start`、`to`、`control` 均为 `points` 数组下标，必须位于范围内。
- 轮廓按 `start -> segments` 遍历，最后一条段的 `to` 必须回到 `start`。
- 所有坐标、字号、偏移、`unitsPerEm`、基线偏移和编辑值必须是有限数。
- 编辑操作支持 `set_point`、`set_size`、`set_x_offset`；目标字形和点必须存在。

## 坐标与采样

- 字体单位坐标以 Y 轴向上。
- 每字形缩放系数为 `size / unitsPerEm`。
- 字形笔位从 `layout.marginX` 开始，之后每字形推进 `advance * scale + spacing` 像素。
- 像素坐标：

```text
x_px = penX + x_font * scale + xOffset
y_px = height - baselineOffset - y_font * scale
```

- `baselineOffset` 是从画布底部到基线的像素距离。
- 每个像素使用 `supersampling × supersampling` 个采样点；第 `(sx, sy)` 个子采样点位于像素内 `(sx + 0.5, sy + 0.5) / supersampling`。
- 位图尺寸严格等于场景 `resolution`。
- 输出 PGM 中 `0` 表示空白，`255` 表示满覆盖；多个字形重叠时取覆盖最大值。

## 算法

### 曲线扁平化

- 控制点先变换到全局像素域。
- 二次贝塞尔采用确定性递归二分，像素容差为 `0.25 / supersampling`。
- 最大递归深度为 14；达到上限时使用端点弦近似。
- 细分结果只依赖控制点、字号、偏移、布局和超采样参数，不依赖线程调度顺序。

### 扫描线与覆盖

- 每个子扫描线按半开 Y 区间 `[y1, y2)` 计算边交点。
- 交点按 X 稳定排序并用累计环绕数执行非零环绕规则。
- X 采样中心使用半开区间判定后累加到像素。
- 灰度为：

```text
round_half_up(hits * 255 / supersampling^2)
```

`tests/test_engine.py` 中另有逐采样点、逐轮廓串行环绕数参考实现，与扫描线结果做逐像素断言。

## 增量依赖与版本

每个分块依赖记录包含：

- 覆盖该块的字形内容摘要；
- 每个相关字形的 `size`、`xOffset`、缩放、笔位、像素包围盒；
- `unitsPerEm`、基线、字符间距；
- 超采样倍数、扁平化容差、最大深度；
- 画布与分块矩形。

版本分为两级：

- `contentVersion`：全部轮廓内容、布局与渲染参数的规范化 SHA-256，用于位图/报告版本和清单级判定。
- `tileVersion`：只对当前块相关依赖取 SHA-256，用于精确复用。未受影响块不会因其他字形改变而改变版本。

启动判定顺序：

1. 持久化清单的 `contentVersion` 等于当前目标：复用所有摘要有效的同版本块，只补缺失块。
2. 清单等于编辑前基线，且本次应用编辑：逐块比较相关依赖版本，精确重算变化块。
3. 清单版本与目标完全无关：从头重做。
4. 无清单但存在原子持久化块（例如 SIGKILL 后）：仅复用依赖与摘要均有效的块。

因此样例编辑只重算受几何包围盒、字号、亚像素偏移或后续布局位移影响的块，不会退化为全量重算。未失效块的 JSON 文件不会被重写。

## 持久化与原子性

```text
state/sample/
  .lock
  manifest.json
  interrupted.json            # 仅在可处理信号中断后存在
  output-transaction.json     # 输出事务进行中时存在
  tiles/
    tile_00000_00000.json
    ...
  archive/
    tile_00000_00000.<old-version>.json
```

分块 JSON 包含灰度像素、块尺寸、声明像素数、像素 SHA-256、依赖记录、依赖摘要、`tileVersion` 和 `contentVersion`。

- 分块先写同目录临时文件，`fsync` 后 `os.replace`。
- 替换前比较旧 `tileVersion`；版本变化时先把旧块移动到 `archive/`。
- 清单、中断标记、事务日志同样使用临时文件整体替换。
- PGM 头保存内容版本、尺寸、像素数和像素摘要；正式位图替换前读取旧文件的尺寸和版本，仅在不一致或缺失时替换。
- 位图与报告通过输出事务日志提交。正常完成时两者一起替换；任何异常会回滚到旧的一对文件。若进程在提交中被强杀，下次 `render` 会依据事务日志回滚后重新提交。

## 并发与资源上限

- 使用固定大小 `ThreadPoolExecutor`，工作线程数不超过 `concurrency`。
- 每个处于计算中的块按 `tileWidth * tileHeight * supersampling^2` 字节计入采样缓冲。
- 调度按分块索引顺序；若下一个块会使总量超过 `memoryLimitBytes`，后续块等待，不会跳过或降低超采样倍数。
- 单块最小缓冲需求大于上限时在创建输出前返回 `E_MEMORY`。
- 任一计算或持久化异常会停止提交后续块，并返回已完成错误中分块索引最小者。

## 信号中断与恢复

- `SIGINT` 与 `SIGTERM` 只设置停止标志，不在计算中途抛出异常。
- 已在计算中的块完成后原子持久化；尚未开始的块不再提交。
- 有未完成块时写入 `interrupted.json`，不写位图和报告。
- 约定退出码：`SIGINT = 130`，`SIGTERM = 143`。
- 中断后使用 `resume`，或重复相同 `render` 命令。
- 若目标为编辑后场景，恢复命令必须带 `--apply-edits`（默认）或使用 `resume`；编辑前基线使用 `--base`。
- `SIGKILL` 无法写中断标记；已完成块仍可通过临时文件+整体替换保证完整。重启时清理 `.tmp`，只复用摘要有效的持久化块，不承诺旧进程生成退出报告。

示例：

```bash
python3 -m glyph_raster render --scene samples/scene.json --apply-edits &
kill -TERM $!
python3 -m glyph_raster resume --scene samples/scene.json --apply-edits
```

## 摘要校验与报告

全部块完成后会再次逐块读取并校验：

- 块版本和相关依赖；
- 尺寸与声明像素数；
- 像素载荷解码后的 SHA-256。

任一缺失、摘要不匹配或像素数不符时：

1. 该块标记失效；
2. 返回业务码 `E_DIGEST`；
3. 立即回退补渲该块；
4. 使用修复后的块完成位图与报告。

因此第一次发现损坏时命令退出码为 `5`，报告 `status` 为 `recovered` 且 `code` 为 `E_DIGEST`；再次运行后会恢复为 `OK`。`verify` 也会识别并报告报告文件中记录的上一次 `E_DIGEST` 历史。

报告位于 `out/sample-report.json`，主要字段包括：

- `contentVersion`、`width`、`height`、`supersampling`、`tileSize`
- `tiles.total/reused/rendered/invalidated/archived`
- `invalidReasons`
- `perGlyph.<id>.tileCount/reusedTiles/invalidatedTiles/invalidReasons`
- `digestIssues`
- `bitmap.path/replaced/sha256/contentVersion`
- `status`、`code`、`exitCode`

`verify --full-check` 会重新串行光栅化全部块，并与持久化分块拼装结果做逐像素比较。

## 业务码

| 业务码 | 退出码 | 含义 |
|---|---:|---|
| `OK` | 0 | 成功 |
| `E_JSON` | 2 | JSON 读取或语法错误 |
| `E_SCHEMA` / `E_VALIDATION` | 2 | 场景结构或有限数值校验失败 |
| `E_CONTOUR` | 2 | 轮廓未闭合或闭合结构错误 |
| `E_EDIT` | 2 | 编辑操作、目标或新值非法 |
| `E_PATH` | 2 | 输出/状态路径越出工作区 |
| `E_IO` / `E_STATE` | 3 | 状态或文件系统错误 |
| `E_MEMORY` | 4 | 资源上限不足以容纳最小块采样缓冲 |
| `E_DIGEST` | 5 | 分块摘要/缺失/像素数异常，已进入补渲流程 |
| `E_RENDER` | 6 | 分块计算失败 |
| `E_INTERNAL` | 1 | 未预期内部错误 |
| `E_INTERRUPTED` | 130/143 | SIGINT/SIGTERM 边界中断 |

非法场景与编辑输入会在创建状态目录或输出文件之前失败。错误输出示例：

```json
{"status":"error","code":"E_EDIT","message":"edits[0] point index is out of range","details":{}}
```

## 样本与实测结果

样本由 `tools/generate_samples.py` 确定性生成：

- 6 个字形，每个含 1 个带二次曲线的外轮廓和 1 个反向环绕的内孔；
- 720×240 画布，48×48 分块，共 75 块；
- 每像素 4×4 超采样；
- 并发上限 4，分块采样缓冲上限 110592 字节，即最多同时计算 3 块；
- 编辑脚本覆盖控制点、字号、亚像素偏移以及字号变化引发的后续布局位移。

本机实际验证结果（Python 3.10.12，NumPy 2.2.6）：

- 基线：75 块全部渲染，`OK`。
- 应用编辑：27 块失效重算、48 块复用、27 个旧版本归档；失效原因计数为 `geometry_bbox=6`、`size=9`、`x_offset=6`、`layout_shift=6`。
- 增量结果内容版本：`18aaf4543bddf4153ab84feff75325b0cac61a4217b94efaf70095a0e4ac190e`。
- 增量位图像素 SHA-256：`95493ddd5f78af2b8156a633a1d6c48ce508c097b46b969f2afc7dc12801d6f0`。
- `verify --full-check`：75 块均通过，增量拼装与串行全量逐像素一致，`OK`。
- 分块摘要破坏：仅损坏块进入补渲，命令返回 `E_DIGEST`/退出码 5，修复后位图与全量一致。
- SIGTERM 演练：在飞行块完成并持久化，未启动块保持等待；写出 `interrupted.json` 与退出码 143，不写位图/报告；恢复后只补渲缺失块。
- SIGKILL 演练：强杀时已持久化 33 块；重启后复用 33 块、重算 42 块，最终 `verify --full-check` 通过。
- 并发/内存测试：4 并发、2 块内存上限时最大同时计算数为 2，采样缓冲不超上限，输出与并发度 1 逐像素相同。
- 自动化测试：`16` 项全部通过，耗时约 `23s`。

## 项目结构

```text
glyph_raster/
  cli.py          # render/resume/verify 与信号处理
  engine.py       # 增量判定、调度、摘要补渲、报告、验证
  errors.py       # 业务码与退出码
  geometry.py     # 布局、扁平化、扫描线、依赖与分块
  model.py        # JSON 解析、严格校验、内容版本
  state.py        # 原子状态、归档、PGM、输出事务
samples/
  glyphs.json
  scene.json
tests/test_engine.py
tools/
  generate_samples.py
  run_sample.py
  reset_sample_outputs.py
```

## 说明与边界

- 该实现输出 P5 PGM，不引入 Pillow 或真实字体依赖。
- 多字形重叠采用最大灰度覆盖，不执行额外 alpha 混合。
- 非法输入不会创建输出；渲染运行期 I/O 失败则可能保留已成功持久化的分块，以便后续恢复。
- 状态写入通过进程内主调度线程和文件锁串行化；不要同时运行两个写同一状态目录的渲染进程。
