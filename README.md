# 宣传片鼓点对齐工具（离线）

新版配乐上线后，镜头切点与鼓点错位。本工具在**完全离线、无容器、无网络**的条件下：

1. 读取并校验本地 WAV 配乐（采样率 / 位深 / 声道数）；
2. 检出鼓点时间（毫秒升序，过近重复检出自动合并）；
3. 读取镜头清单，按累计时长推导每条镜头的原始起点，标识超出配乐总长的部分；
4. 把每个镜头起点对齐到最近鼓点，记录对齐前后毫秒、偏移量与偏移方向；
5. 为每个已对齐镜头生成带标题与时码的切换预览图（PNG）；
6. 输出节奏核查结果 JSON，**已完成 / 未完成分区**，跳过镜头不会被伪装成对齐成功。

音频解析与鼓点检出仅使用 Python 标准库（不依赖 `numpy` / `scipy`，也不依赖
Python 3.13 起已移除的 `audioop`），唯一第三方依赖是绘图用的 **Pillow**。

## 1. 环境与启动

要求：Python 3.10+（开发与验证环境 Windows 11 + Python 3.14.7 + Pillow 12.3.0）。

```powershell
# 联网机器一次性准备离线安装包（目标机器无需网络）
python -m pip download -d wheelhouse -r requirements.txt

# 目标离线机器安装
python -m pip install --no-index --find-links=./wheelhouse -r requirements.txt
```

### 一键复现全部业务样例

```powershell
# 生成确定性演示输入（合成 WAV + 镜头清单），并依次运行 A~E 五个场景
python demo/run_demo.py
```

### 作为命令行工具使用

```powershell
python -m beat_align.cli `
  --wav demo/input/soundtrack_mono.wav `
  --shots demo/input/shots_main.json `
  --output-dir demo/generated/manual/previews `
  --report demo/generated/manual/rhythm_report.json
# 可选：--frame-ms 20 --hop-ms 10 --min-gap-ms 120
```

退出码：`0` 正常（含"预览目录不可写但 JSON 已产出"的已上报部分失败）；
`2` 业务错误（不支持的音频、WAV 损坏、清单非法等，信息打印到 stderr）；`1` 未预期错误。

### 运行测试（10 项验收要求，13 个用例）

```powershell
python -m unittest -v tests.test_all_requirements
```

## 2. 目录结构

```
beat_align/
  audio.py      WAV 读取与格式校验，PCM 软解码（8/16/24/32 bit）
  detect.py     分帧 RMS 能量突变检出鼓点，自适应阈值 + 过近候选合并 + 采样级修正
  shots.py      镜头清单读取、累计起点推导、零/缺时长跳过、超限标记
  align.py      最近鼓点对齐，偏移量与方向（等距时取更早鼓点）
  preview.py    Pillow 预览图（标题、时码、偏移核查、节奏时间轴）
  report.py     核查 JSON（completed / incomplete 分区）
  pipeline.py   流水线编排（JSON 先于预览落盘）
  cli.py        命令行入口
demo/
  generate_demo_data.py  确定性合成演示输入（固定随机种子）
  run_demo.py            A~E 五个业务场景
tests/
  test_all_requirements.py
```

## 3. 输入约定

**WAV**：无压缩 PCM，单声道，8/16/24/32 bit，采样率 8000~48000 Hz。
双声道会被明确拒绝并提示先混音为单声道；损坏文件给出读取失败原因。

**镜头清单 JSON**：顶层数组，每条含 `title` 与 `duration_ms`（整数毫秒）：

```json
[
  {"title": "开场航拍", "duration_ms": 2500},
  {"title": "人物特写", "duration_ms": 0},
  {"title": "用户访谈"}
]
```

起点不允许手工指定，一律由前面各镜头时长累计推导。`duration_ms` 为 0、缺失、
非数值或非整数时，该镜头进入 `incomplete` 并记录原因，其余镜头照常对齐。

## 4. 鼓点检出算法与精度

- 20 ms 分析窗、10 ms 帧移计算分帧 RMS；
- 起振特征 `novelty = max(0, RMS[n] − RMS[n−1])`，首帧取自身能量（0 时刻鼓点不漏检）；
- 阈值 = `max(均值 + 3×标准差, 绝对下限)`，叠加噪声底能量门防误检；
- 局部极大值为候选；间隔小于 `min_gap_ms`（默认 120 ms）的候选聚成一簇，
  整簇只保留 novelty 最大者，实现重复检出合并；
- 每个保留帧在 ±10 ms 窗口内做采样级最大正向差分修正，误差从帧级收敛到采样级。

## 5. 可复现的业务样例与实际运行结果

演示配乐由 `demo/generate_demo_data.py` 固定种子合成：16 kHz / 16 bit / 单声道，
时长 10000 ms，鼓点位于 `[0, 1000, …, 9000] ms` 共 10 个。以下为本机实际输出。

### 场景 A：主流程（6 镜头，含零时长/缺时长，总长超配乐 1 秒）

输入 `shots_main.json`（有效时长 2500+1500+2000+5000 = 11000 ms）：

```
鼓点数量   : 10
鼓点时间ms : [0, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000]
总长核查   : 镜头总长 11000 ms 超出配乐时长 10000 ms，超出 1000 ms，超出部分仍参与对齐但无配乐覆盖
镜头统计   : 共 6 条，已对齐 4 条，未完成 2 条
  #01 开场航拍: 0 -> 0 ms (偏移 0 ms, 无偏移, 鼓点 0 ms)
  #02 城市快切: 2500 -> 2000 ms (偏移 500 ms, 向前(负偏移), 鼓点 2000 ms)
  #04 产品全景: 4000 -> 4000 ms (偏移 0 ms, 无偏移, 鼓点 4000 ms)
  #06 品牌落版: 6000 -> 6000 ms (偏移 0 ms, 无偏移, 鼓点 6000 ms)
  #03 人物特写: 未完成（duration_ms 为 0，时长必须大于 0）
  #05 用户访谈: 未完成（缺少 duration_ms 字段）
```

逐条可核算：例如 #02 原始起点 2500 ms，最近鼓点 2000 ms，
`offset_ms = |2000 − 2500| = 500`，方向 `earlier`（带符号偏移 −500）。

### 场景 B：三镜头 → 三张预览图（需求 5）

```
  #01 开场航拍: 0 -> 0 ms    (偏移 0,   无偏移)
  #02 产品全景: 2200 -> 2000 ms (偏移 200, 向前(负偏移))
  #03 品牌落版: 5500 -> 5000 ms (偏移 500, 向前(负偏移))
预览图     : 3 张
```

文件名按镜头顺序零填充：`shot_001_开场航拍.png`、`shot_002_产品全景.png`、
`shot_003_品牌落版.png`。每张 1280×720，画面含镜头序号/标题、对齐后时码
`HH:MM:SS.mmm`、原始/对齐起点、偏移量与方向、所用鼓点，以及底部节奏时间轴
（蓝点=全部鼓点，橙线=原始起点，绿线=对齐后起点）。

### 场景 C：双声道被拒绝（需求 1）

```
[错误] 不支持 2 声道音频：本工具仅支持单声道 WAV，请先将配乐混音为单声道后重试
（文件：.../soundtrack_stereo.wav）
退出码: 2
```

### 场景 D：损坏 WAV 读取失败且零产出（需求 7）

```
[错误] 配乐读取失败，文件不是合法 WAV：.../soundtrack_corrupt.wav（unknown format: 3226）
运行前后输出目录内容差异: []（预期为空）
退出码: 2
```

流水线在第一步即中止，输出目录与 JSON 均不会创建，杜绝剪辑误用旧预览。

### 场景 E：预览目录不可写，核查 JSON 仍完整（需求 9）

用同名文件占用输出路径（跨平台稳定复现；Windows 目录只读位不可靠）：

```
preview_error: 预览图输出目录不可写或无法创建：...（WinError 183 ...）。
               核查结果 JSON 已在写出预览前落盘，可正常打开。
JSON 条目: completed=4, incomplete=2
```

实现保证：**JSON 先于任何预览图写出**；预览失败只产生警告，已落盘 JSON 含全部条目。

## 6. 核查结果 JSON 说明（需求 8、10）

顶层字段：`audio`（配乐元信息）、`detector`（检出参数与全部鼓点毫秒）、
`overhang`（总长超限标记）、`summary`（计数）、`completed`、`incomplete`。

`completed` 条目（顺序与镜头清单一致）：

```json
{
  "index": 2,
  "title": "城市快切",
  "status": "aligned",
  "duration_ms": 1500,
  "original_start_ms": 2500,
  "aligned_start_ms": 2000,
  "offset_ms": 500,
  "signed_offset_ms": -500,
  "direction": "earlier",
  "direction_label": "向前(负偏移)",
  "beat_time_ms": 2000,
  "aligned_timecode": "00:00:02.000",
  "original_timecode": "00:00:02.500",
  "preview_image": "D:/.../shot_002_城市快切.png"
}
```

`incomplete` 条目只含 `status: "skipped"` 与 `reason`，**不含任何对齐字段**，
无法被下游误读为对齐成功。核算恒等式（测试中强制断言）：

```
offset_ms        = |aligned_start_ms − original_start_ms|
aligned_start_ms = beat_time_ms
```

## 7. 验收要求与测试对照

| 需求 | 测试用例 |
| --- | --- |
| 1 WAV 校验，双声道拒绝 | `test_01_mono_wav_loads_stereo_rejected`、`test_01b_other_formats_and_depths` |
| 2 鼓点检出 ≤1 帧误差、重复合并 | `test_02_beat_detection_accuracy_and_merge` |
| 3 累计起点、超 1 秒标识且仍参与对齐 | `test_03_cumulative_starts_and_overhang` |
| 4 偏移量=毫秒差绝对值、方向可判断 | `test_04_align_offset_and_direction` |
| 5 三张有序预览图、含标题时码 | `test_05_preview_images`、`test_05b_timecode_format` |
| 6 零/缺时长跳过，其余正常 | `test_06_zero_or_missing_duration_skipped` |
| 7 损坏 WAV 报错且无任何产出 | `test_07_corrupt_wav_no_outputs` |
| 8 JSON 顺序与逐字段核算 | `test_08_report_json_order_and_fields` |
| 9 目录不可写有提示、JSON 可读 | `test_09_unwritable_dir_but_report_readable` |
| 10 已完成/未完成分区不混淆 | `test_10_completed_vs_incomplete_separation`、`test_10b_no_beats_all_incomplete` |

本机最近一次测试结果：`Ran 13 tests … OK`（全部通过）。
