# wav_editor — 本地 WAV 剪辑拼接命令行工具

仅依赖 **Python 3.10+ 标准库**（`wave` / `json` / `array` / `argparse` / `shutil` 等），
从 16-bit PCM WAV 录音与 JSON 剪辑方案裁剪、拼接短音效，应用线性淡入/淡出与增益，
输出 WAV 并在标准输出打印 JSON 报告。单个片段失败不影响其他片段。

## 目录结构

```
project60/
├── wav_editor/
│   ├── __init__.py
│   ├── __main__.py        # 支持 python -m wav_editor
│   └── cli.py             # 全部业务逻辑（参数/校验/渲染/备份/报告）
├── tools/
│   └── make_samples.py    # 确定性测试样本生成器（WAV + 方案 JSON）
├── tests/
│   └── test_wav_editor.py # 16 个 unittest 验收测试
├── demo_data/             # 生成的样本（运行生成器后出现）
└── demo_run/              # 演示输出目录
```

## 快速开始（本地复现）

```powershell
# 1. 生成确定性样本（44.1kHz 正弦波 WAV + 各类方案 JSON）
python tools/make_samples.py demo_data

# 2. 运行工具：源WAV  方案JSON  输出WAV
python -m wav_editor demo_data\tone_16bit_mono_1sec.wav demo_data\plan_gain_minus6.json demo_run\out.wav

# 3. 运行全部验收测试
python -m unittest -v tests.test_wav_editor
```

## 方案 JSON 格式

顶层为对象，含 `segments` 数组；每个片段**必须**含以下 5 个数字字段：

| 字段 | 含义 | 单位 |
|---|---|---|
| `start` | 起点时间 | 秒 |
| `end` | 终点时间 | 秒 |
| `fade_in` | 线性淡入时长 | 秒 |
| `fade_out` | 线性淡出时长 | 秒 |
| `gain_db` | 增益（负值衰减） | dB |

```json
{
  "segments": [
    {"start": 0.0, "end": 0.5, "fade_in": 0.01, "fade_out": 0.01, "gain_db": -6.0}
  ]
}
```

## 处理规则与边界约定

- 仅接受 **16-bit PCM WAV**；8-bit、非 PCM 压缩格式直接拒绝，退出码 2，且不产生输出文件。
- 片段按方案顺序拼接；每个采样点应用 `增益(10^(dB/20)) × 淡变系数`，增益后限幅到 [-32768, 32767]。
- 淡入为线性（首帧 1/N、末帧 1.0），淡出对称，避免拼接处爆音。
- 淡入/淡出超过片段长度时按比例收缩；两者重叠时自动缩短至互不重叠。
- 以下片段被**跳过**并在报告中记录中文原因，其余片段照常输出：
  负参数、`end <= start`（零/负时长）、起点越过源时长、裁剪后帧数为零。
- 全部片段被跳过时，仍生成 **44 字节、仅含标准 RIFF 头**的空 WAV，退出码 0。
- 输出路径已存在时，先备份为 `<输出路径>.bak`（`shutil.copy2` 保留元数据）；
  备份失败则中止，原输出保持不变。写出采用同目录临时文件 + 原子替换。
- 输出的通道数、采样宽度、采样率与源完全一致。

## JSON 报告（stdout）

```json
{
  "sample_rate": 44100,
  "channels": 1,
  "sample_width_bytes": 2,
  "source_total_frames": 44100,
  "output_total_frames": 44100,
  "output_total_samples": 44100,
  "segments_total": 1,
  "segments_ok": 1,
  "segments_skipped": 0,
  "all_skipped": false,
  "segments": [
    {"index": 0, "status": "ok", "start_frame": 0, "end_frame": 44100, "frames": 44100, "gain_db": -6.0}
  ]
}
```

- `status` 仅取 `ok` 或 `skipped`；跳过时改为 `reason` 字段说明原因。
- 报告条目数与方案片段数始终一致。致命错误信息走 stderr，不与 stdout 的 JSON 混淆。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 正常完成（含全部片段被跳过的空 WAV 情形） |
| 2 | 致命错误：参数缺失、文件不存在/不可读、JSON 非法、方案字段缺失/类型错误、非 16-bit PCM、备份失败、输出目录不存在等 |

## 验收对照（实测结果）

| # | 验收点 | 实测 |
|---|---|---|
| 1 | 缺参数/文件不可读报错含缺失项或路径，非零退出 | argparse 报 `output_wav` required；缺文件报完整路径；退出码 2 |
| 2 | 字段缺失/类型错误报片段序号与字段名 | `片段 0 缺少必填字段 'gain_db'`；字符串类型同样被拒 |
| 3 | 8-bit WAV 拒绝、无输出 | `源文件为 8-bit…仅支持 16-bit`，退出码 2，输出路径无文件 |
| 4 | -6dB 峰值约减半、采样率一致 | 源峰值 20000 → 输出 10023，理论 10000，**相对误差 0.23% ≤ 1%** |
| 5 | 越界片段跳过、有效片段照常 | 1 越界 + 2 有效 → 输出 44100 帧（两段），报告含跳过原因 |
| 6 | stdout 合法 JSON，状态仅 ok/skipped | `json.loads` 通过，条目数与方案一致 |
| 7 | 全跳过生成 44 字节空 WAV，退出码 0 | 文件大小 44 字节，`all_skipped=true`，退出码 0 |
| 8 | 输出通道/位宽与源一致、标准 RIFF 头 | 立体声 2ch/16bit/44100Hz/44100 帧，wave 正常读取 |
| 9 | 覆盖前备份 .bak；备份失败中止 | `.bak` 与原内容字节一致；只读 .bak 时报错退出码 2，原文件不变 |

测试：`python -m unittest tests.test_wav_editor` → **Ran 16 tests … OK**。
