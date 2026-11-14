# srt_check — SRT 字幕与固定帧率无声视频适配核对工具

后期合成前的本地核对工具：读取 SRT 字幕与 JSON 画幅配置，输出逐条帧号区间、
行数、边距与冲突清单（`check_report.json`），并渲染代表帧预览 PNG 供人工核对。
纯本地运行，不依赖网络、媒体解码器或外部服务。

## 环境要求

- Python 3.10+
- Pillow（`pip install Pillow`）

## 启动说明

```powershell
# 1. 生成确定性业务样例（samples/ 目录）
python make_samples.py

# 2. 运行核对
python srt_check.py --srt samples\full.srt --config samples\config.json --out-dir out_full

# 3. 查看参数说明
python srt_check.py --help
```

参数：

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--srt` | SRT 字幕文件路径（单文件不超过 500 条） | 必填 |
| `--config` | JSON 配置路径 | 必填 |
| `--out-dir` | 输出目录（check_report.json 与预览 PNG） | 必填 |
| `--max-previews` | 最多渲染的代表帧预览数量 | 5 |

## 配置字段

必填：`frame_rate`（帧率）、`width` / `height`（画幅宽高）、`margin_ratio`（安全边距比例）。
可选：`font_size`（默认 48）、`chars_per_line`（每行最多全角字符数，默认 18）、
`font_path`（字体文件，缺省自动查找系统中文字体）、
`background_color` / `text_color`（`#RRGGBB`，默认黑底白字）。

安全区 = 画幅四边各内缩 `margin_ratio`。例如 1920×1080、边距 0.05 时，
左右边距各 96px，可用区域 1728×972。

## 核对规则

- **帧号换算**：起帧 = round(开始秒 × 帧率)，止帧 = round(结束秒 × 帧率) − 1，
  起止帧均包含在显示区间内。25fps 下 1.000s–2.000s → 帧 25–49，共 25 帧。
- **invalid**：结束帧不晚于开始帧（时间倒置/零时长），或文本为空/仅空白；
  给出具体原因，不影响其他条目，也不为其渲染预览。
- **重叠**：任意两条显示区间重叠时，在 `overlaps` 清单记录双方序号与重叠帧数，
  不阻止帧号输出。
- **行数与宽度**：全角字符计 1 单位、半角计 0.5，每行最多 18 单位估算行数；
  文本估算宽度 = 最宽行单位数 × font_size，超出安全区宽度时给出具体超出像素值。
- **预览 PNG**：最多 5 张，覆盖首条、末条与存在问题（超宽/重叠）的字幕开始帧，
  绘制安全区边框与对应文本。
- **失败路径**：SRT 缺失/无法解析、配置缺字段或数值非法时，标准错误输出具体
  原因并以非零退出码结束，不产出任何结果文件。

## 输出

`check_report.json`：每条含 `index`、`start_frame`、`end_frame`、`frame_count`、
`lines`、`margin_left`、`margin_right`、`margin_ok`、`overflow_px`、`status`
（`ok` / `overflow` / `invalid`）、`reasons`，按开始帧升序排列（invalid 条目
无帧号，按序号列于末尾）；另含 `overlaps` 冲突清单与 `safe_area` 信息。
输入不变时重复运行，JSON 与 PNG 内容（哈希）完全一致。

## 业务样例与实际运行结果

`samples/full.srt`（8 条）覆盖全部核对场景，运行：

```
python srt_check.py --srt samples\full.srt --config samples\config.json --out-dir out_full
```

实际输出：

```
核对完成: 共 8 条字幕, invalid 2 条, 超宽 1 条, 重叠 1 组
核对报告: out_full\check_report.json
预览帧: out_full\preview_seq001_frame000025.png
预览帧: out_full\preview_seq003_frame000100.png
预览帧: out_full\preview_seq004_frame000115.png
预览帧: out_full\preview_seq006_frame000225.png
预览帧: out_full\preview_seq008_frame000350.png
```

关键核对结果（与预期一致）：

- 条目 1（1.000s–2.000s @25fps）：帧 25–49，共 25 帧；
- 条目 2（3.000s→2.500s 倒置）：`invalid`，原因“结束帧不晚于开始帧”；
- 条目 3/4：重叠 10 帧，双方帧号照常输出；
- 条目 5（31 个全角字符）：估算 2 行，边距满足；
- 条目 6（42 个全角字符）：超宽，`overflow_px = 288`（2016px − 1728px），其余条目不受影响；
- 条目 7（空白文本）：`invalid`，不渲染预览；
- 预览共 5 张：首条、末条、超宽条目与两条重叠条目的开始帧。

失败路径验证：

```
python srt_check.py --srt samples\nope.srt --config samples\config.json --out-dir out_err1
# SRT 错误: SRT 文件不存在: samples\nope.srt   （退出码 3，无输出文件）

python srt_check.py --srt samples\full.srt --config samples\config_missing.json --out-dir out_err2
# 配置错误: 配置缺少必要字段: frame_rate        （退出码 2，无输出文件）
```

确定性验证：连续运行两次，`check_report.json` 与全部 PNG 的 SHA256 哈希一致。

颜色配置：省略颜色字段时预览为黑底白字；使用 `samples\config_colors.json`
（`#102030` 底 / `#FFE080` 字）时按配置着色。
