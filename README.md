# 过场动画字幕安全区预览工具

把字幕按「安全区－右侧血条面板」的约束叠加到过场动画帧上，输出：
- 带字幕的预览帧 PNG 序列（`preview_<序号>_<时间戳>ms.png`）
- 一份说明 JSON（`render_report.json`）：每段字幕的放置帧、文本框像素坐标、起音偏移与处理结果。

纯本地运行，不联网、不使用容器。

## 环境

- Python 3.10+（本机验证使用 3.14.7）
- Pillow（本机验证使用 12.3.0）

```powershell
python -m pip install Pillow
```

中文字体默认按顺序探测 `C:\Windows\Fonts\msyh.ttc` 等系统字体；也可在清单的 `style.font_path` 指定。
找不到系统字体时退回 Pillow 内置字体（不保证中文覆盖）。

## 本地启动

```powershell
# 1. 生成确定性样例（帧、字幕表、面板配置）
python demo/make_demo.py

# 2. 运行主样例
python -m subtitle_preview.cli --manifest demo/samples/main/manifest.json --out demo/out/main

# 3. 一键核对需求 1–14 的验收点
python demo/verify_demo.py
```

上一次真实运行结果：主样例输出 31/31 帧、5 条字幕（4 条已放置、1 条未放置、1 条标记偏移过大），
`demo/verify_demo.py` 共 281 项检查全部通过，退出码 0。

## 清单格式

路径相对于清单所在目录解析。字段名带 `*` 的为必填。

```json
{
  "canvas": {"width": 640, "height": 360},
  "safe_area": {"left": 32, "top": 18, "right": 608, "bottom": 342},
  "panel": {"left": 528, "top": 60, "right": 600, "bottom": 300},
  "frames": [
    {"time_ms": 0, "path": "../../assets/frames/frame_0000.png"}
  ],
  "subtitles": [
    {"start_ms": 300, "end_ms": 700, "text": "风起于青萍之末", "onset_ms": 260}
  ],
  "style": {
    "font_size": 30,
    "line_spacing": 1.35,
    "padding": 12,
    "stroke_width": 2,
    "fill": "#FFFFFF",
    "stroke_fill": "#000000",
    "onset_review_threshold_ms": 500,
    "font_path": null
  }
}
```

- `canvas` 缺省时从第一张可解码帧推断；`safe_area` 缺省时取画布 5% 内缩。
- `panel` 允许写成 `{"x", "y", "width", "height"}`；缺失或坐标非法时按「无面板」处理并在 `warnings` 说明。
- `subtitles` 中 `text` 为空白（含仅换行/空格）的条目会被忽略并计入 `warnings`。
- `frames` 中时间戳为整数毫秒；`time_ms` 落在 `[start_ms, end_ms]` 闭区间内的帧才渲染该字幕。

## 处理规则

| 规则 | 实现 |
| --- | --- |
| 安全区不越过面板 | 屏幕坐标系中面板落在右侧时，可用区域为安全区中面板左侧的竖带；文本框整体落在该区域内，因此四边不与面板相交 |
| 垂直/水平居中 | 文本框垂直居中于安全区，水平居中于面板左侧可用区域；无面板时居中于整个安全区 |
| 两行上限 | 安全区可用高度小于「两行文本框高度」时整段不放置；文本折行后超过 2 行同样不放置 |
| 跨帧坐标固定 | 文本框按清单静态计算，与帧内容无关，同一字幕所有显示帧使用同一整数坐标 |
| 起音偏移 | `start_ms - onset_ms`，整数毫秒；无起音为 `null`；绝对值 > 500ms 时 `result` 为 `偏移过大` |
| 坏帧 | 文件缺失或 PNG 无法解码时跳过该帧（不输出文件，但条目保留），在 `frames[].problem` 与 `warnings` 记录 |
| 输出安全 | 输出目录不存在则创建；写入用同目录临时文件 + 原子替换，失败时清理已写文件并返回非零退出码 |
| 可复现 | 判定的输入、字体与压缩参数固定，同一输入两次运行说明与 PNG 逐字节一致；说明为 UTF-8 无 BOM、中文原字符 |

退出码：`0` 成功；`2` 输入/清单错误；`3` 输出目录或写入错误。

## 说明 JSON 结构

- `subtitles[]`：按 `start_ms` 升序（相同则按输入顺序），字段包括
  `start_ms` / `end_ms` / `text` / `onset_ms` / `onset_offset_ms` / `offset_exceeds_threshold`
  / `placed` / `result` / `reason` / `box` / `frames`
  - `box`：`left/top/right/bottom/width/height`，未放置时为 `null`
  - `frames[]`：该字幕时间窗内的每个帧（`index`/`time_ms`/`output`/`rendered`）
- `frames[]`：清单中每个帧条目（`path`/`output`/`subtitle_indices`/`problem`），坏帧 `output` 为 `null`
- `panel.status`：`ok` / `missing` / `invalid`
- `usable_region`、`two_line_capacity_height`：定位判定所依据的几何量
- `warnings`、`summary`：提示与计数

## 目录

```
subtitle_preview/
  cli.py      命令行入口
  models.py   清单解析与校验
  layout.py   字体、折行、居中与放置决策
  render.py   帧渲染、说明生成、原子写入
demo/
  make_demo.py    确定性样例生成
  verify_demo.py  需求 1–14 的本地验收脚本
  assets/         生成的样例帧
  samples/        各场景清单
  out/            运行输出
```

## 已知限制

- 折行按字符宽度贪心处理，未做标点禁则（避免行首标点）等排版优化。
- 多段字幕时间窗重叠时会叠加渲染；本工具不做避让，文本框坐标按各自可行区域独立计算。
- 帧尺寸与 `canvas` 不一致时按实际帧尺寸叠加，并在该帧 `problem` 中提示，不做缩放。
