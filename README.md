# 角色配音批处理工具（Python 标准库，零外部依赖）

读取单声道 16 位 PCM WAV 与 JSON 片段配置，按毫秒区间裁剪、统一响度（目标 -20 dBFS）、
按配置顺序拼接（片段间 50 ms 静音），输出可播放 WAV 与 UTF-8 文本报告。

## 环境要求

- Windows + Python 3.10 或更高版本（本机验证版本：Python 3.10.20）
- 仅使用标准库（`wave` / `struct` / `json` / `math` / `argparse` 等），无需 pip 安装
- 不使用 Docker 或任何容器

## 目录结构

```
project31/
├─ src/
│  └─ voice_clip_tool.py     # 主工具
├─ tests/
│  ├─ make_samples.py        # 确定性测试样本生成器（无随机数）
│  └─ verify.py              # 11 项验收要求的端到端自动校验
├─ testdata/                 # 生成器产出的测试 WAV / JSON（独立目录，与脚本分离）
├─ output/                   # 演示输出（WAV + 报告 + 错误日志）
└─ README.md
```

## 快速开始（可复现的本地启动方式）

所有命令在项目根目录 `project31/` 下执行。建议加 `-I` 隔离模式，
避免当前目录或 `PYTHONPATH` 中同名模块（如伪造的 `json.py`）被导入。

### 1. 生成确定性测试样本

```powershell
python -I tests/make_samples.py testdata
```

生成内容（16000 Hz、单声道 16 位 PCM，源时长 2000 ms，4 个特征区段）：

| 时间区间 | 特征 | 预期结果 |
|---|---|---|
| 0–500 ms | 正弦波幅度 3000，RMS 约 -23.8 dBFS | 已平衡（+3.8 dB → -20 dBFS） |
| 500–1000 ms | 全零采样 | 静音，不合并 |
| 1000–1500 ms | 正弦波幅度 250，RMS 约 -45.4 dBFS | 增益受限（封顶 +20.0 dB） |
| 1500–2000 ms | 正弦波幅度 32000，接近满幅 | 可能削波，仍平衡并合并 |

另含两条非法区间配置（越界、起止逆序）、损坏 JSON、缺字段 JSON、立体声 WAV。

### 2. 执行业务处理

```powershell
python -I src/voice_clip_tool.py `
  --wav testdata/voice_mono.wav `
  --config testdata/config_ok.json `
  --out-wav output/demo_merged.wav `
  --out-report output/demo_report.txt
```

成功时退出码 `0`，生成：

- `output/demo_merged.wav`：单声道 16 位 PCM，时长 = 3×500 ms + 2×50 ms = **1600 ms**
- `output/demo_report.txt`：UTF-8（无 BOM）逐片段报告

### 3. 运行完整验收（11 项）

```powershell
python -I tests/verify.py verify_result.txt
```

全部通过时退出码 `0`，终端打印并向 `verify_result.txt` 写入可读的 UTF-8 留档。

## 配置文件格式

顶层为 JSON 数组（或 `{"segments": [...]}`），每个片段三个必要字段，顺序即处理顺序：

```json
[
  {"id": "line-001", "start_ms": 0,    "end_ms": 500},
  {"id": "line-002", "start_ms": 500,  "end_ms": 1000}
]
```

- 毫秒按采样率四舍五入换算为采样下标；区间为左闭右开 `[start, end)`。
- 区间无效的条件：负值、`start >= end`、结束位置越过音频总时长（含换算后零长度）。

## 处理规则与状态

| 状态 | 触发条件 | 是否合并 | RMS/增益字段 |
|---|---|---|---|
| 区间无效 | 越界或起止不合法 | 否 | null / null |
| 静音 | 有效区间内所有采样 `|x| < 5` | 否 | null / null |
| 增益受限 | 所需增益 > +20 dB，按 +20.0 dB 封顶 | 是 | 实际值 / 20.0 |
| 可能削波 | `|x| ≥ 32767×90%` 的采样占比 > 1% | 是（仍做平衡） | 实际值 |
| 已平衡 | 非静音、未受限、未触发削波，达到 -20 dBFS | 是 | 实际值 |

说明：

- RMS 按 16 位满幅 32768 折算 dBFS：`20*log10(rms / 32768)`。
- 所需增益 = `-20 - 原始RMS`；仅正向增益受 +20 dB 上限约束，负向衰减不限。
- 缩放后采样钳制到 `[-32768, 32767]`，保证输出始终为合法 16 位 PCM。
- 保留片段按配置顺序拼接，相邻片段之间插入 50 ms（采样率换算取整）纯静音。
- 若所有片段均未保留，仍输出仅含 WAV 头的空音轨并在报告中注明保留数为 0。

## 退出码与错误提示

| 退出码 | 场景 | stderr 提示 | 是否生成输出 |
|---|---|---|---|
| 0 | 正常（含存在无效/静音片段） | — | 是 |
| 1 | JSON 不可解析 / 缺必要字段 / 配置文件不存在 | 含 `配置文件错误` | 否 |
| 2 | WAV 不存在或非单声道 16 位 PCM | 含 `不支持的音频格式` | 否 |

前置校验全部通过后才创建输出文件，因此错误路径不会留下半成品 WAV/报告。

## 报告样例（output/demo_report.txt 节选）

```
[1] seg-normal | 已平衡 | -23.78 | 3.8 | 已施加 3.78 dB 增益，响度平衡至目标 -20.0 dBFS
[2] seg-silence | 静音 | null | null | 片段内所有采样绝对值均低于 5，判定为静音，不参与合并
[3] seg-quiet | 增益受限 | -45.36 | 20.0 | 所需增益 25.36 dB 超过上限，实际施加 +20.0 dB，未达到目标 -20.0 dBFS
[4] seg-loud | 可能削波 | -3.22 | -16.8 | 满幅 90% 以上采样占比超过 1.00%，仍完成响度平衡并参与合并
[5] seg-out-of-range | 区间无效 | null | null | ...
[6] seg-reversed | 区间无效 | null | null | ...
```

## 错误场景手动演示

```powershell
# 损坏 JSON → 退出码 1
python -I src/voice_clip_tool.py --wav testdata/voice_mono.wav `
  --config testdata/config_broken.json --out-wav output/e.wav --out-report output/e.txt

# 立体声 WAV → 退出码 2
python -I src/voice_clip_tool.py --wav testdata/voice_stereo.wav `
  --config testdata/config_ok.json --out-wav output/e.wav --out-report output/e.txt
```
