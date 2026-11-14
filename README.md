# 分镜预览片字幕/转场时序质检

读取 SRT 字幕、转场清单 JSON、画幅配置 JSON,检测**字幕跨转场、字幕互叠、时间倒置、安全区越界**,输出逐条帧区间 JSON 报告,并为每条有效字幕渲染一张与画幅同尺寸的代表帧 PNG(含安全区框与字幕文本)。纯本地运行,仅依赖 Pillow,不做任何媒体解码。

## 环境

- Python 3.10+
- Pillow:`pip install pillow`(或 `pip install -r requirements.txt`)

## 启动

```bash
# 1. 生成确定性样例输入(samples/)
python tools/make_samples.py

# 2. 运行质检(输出 output/report.json 与 output/frames/*.png)
python main.py

# 指定自己的输入
python main.py --srt my.srt --transitions my_transitions.json --canvas my_canvas.json --out my_output

# 3. 自检(逐项核对 11 条验收标准)
python tools/selfcheck.py
```

## 输入格式

**canvas.json** — `safe_area_ratio` 可选,缺省时取宽高各 10% 作为安全区边距:

```json
{"width": 1280, "height": 720, "fps": 25, "safe_area_ratio": 0.1, "font_size": 80, "line_height": 100}
```

**transitions.json** — 顶层数组或 `{"transitions": [...]}`;`description` 仅用于报告标注,不参与判定:

```json
{"transitions": [{"start_frame": 50, "end_frame": 75, "description": "叠化:天台到街道"}]}
```

**subtitles.srt** — 标准 SRT。空数组/缺失文件的处理见下。

## 判定规则

- **帧换算**:统一 `round(毫秒 × fps / 1000)`,报告与预览图共用同一结果(25fps 下 `00:00:01,000` → 第 25 帧)。
- **帧区间**为闭区间,相交判定 `s1 <= e2 且 s2 <= e1`。
- **时间倒置**:结束帧 ≤ 起始帧,仅列入 `errors`,不参与其他检测、不生成 PNG。
- **安全区**:按字号量宽、按安全区宽度贪心换行,文本块底部对齐安全区、水平居中,计算四向边距;任一边距小于安全区边距即判越界。
- **容错**:任一输入缺失/格式错误时记入报告 `input_errors` 并继续;转场清单缺失或为空数组 `[]` 时跳过转场检测,其余结果不变。

## 实际检查结果(样例,`python main.py` 实跑)

样例含 6 条字幕,覆盖全部冲突类型。实跑汇总(`output/report.json` → `summary`):

| 指标 | 数量 | 明细 |
|---|---|---|
| 字幕总数 / 有效 | 6 / 5 | #4 被剔除 |
| 时间倒置 | 1 | #4:起始 100 帧、结束 90 帧,仅在 `errors` |
| 转场冲突 | 2 | #3 [60,80] ×「叠化:天台到街道」[50,75];#5 [125,200] ×「闪白转场」[150,170] |
| 互叠配对 | 1 | #1 [10,30] × #2 [25,40] |
| 安全区越界 | 1 | #5 换行 6 行,上边距 48px < 安全区 72px |

- 报告条目顺序与源文件一致(#1,#2,#3,#5,#6),逐条给出帧区间、行数、四向边距与冲突列表。
- 5 张代表帧 PNG 均为 1280×720,安全区框位于 (128,72)–(1152,648);越界条目红框、正常条目绿框,框位置与报告边距一致。
- `python tools/selfcheck.py` 实跑 **15/15 项通过**(对应 11 条验收标准,含转场清单缺失、空清单、缺省/指定安全区比例等边界)。

## 目录

```
main.py               CLI 入口
subtitle_qc/          解析(parsers)、检测(core)、渲染(render)
tools/make_samples.py 确定性构造样例输入
tools/selfcheck.py    验收自检
samples/              样例输入(由 make_samples 生成)
output/               报告与代表帧(运行产物)
```
