# WAV 标记区间提取与合并工具

从同一段现场录音（单声道/立体声 16-bit PCM WAV）中按 JSON 标记区间提取片段，
按顺序合并为新音轨，并生成 JSON 报告。**仅依赖 Python 3.10+ 标准库**，无需安装任何第三方包，
不使用 Docker 或其他容器。

## 目录结构

```
project32/
├─ src/
│  └─ wav_merger.py          # 主程序（CLI，全部业务逻辑）
├─ tests/
│  ├─ make_samples.py        # 确定性生成测试 WAV 与标记 JSON（无随机数）
│  └─ run_acceptance.py      # 对照 10 条验收项的端到端测试（子进程驱动真实 CLI）
├─ samples/                  # 生成的样本（运行 make_samples 后出现）
│  ├─ wav/
│  │  ├─ stereo_source.wav   # 44.1kHz 立体声 3.0s：左声道直流 +1000，右声道静音
│  │  ├─ mono_source.wav     # 44.1kHz 单声道 3.0s：斜坡+正弦确定性信号
│  │  ├─ stereo_levels.wav   # 44.1kHz 立体声 3.0s：分段恒定电平（淡化验收用）
│  │  └─ source_8bit.wav     # 44.1kHz 单声道 8-bit（格式拒绝验收用）
│  └─ markers/               # 各类标记 JSON（含非法/空/超长/损坏用例）
└─ build/                    # 测试与演示输出（自动创建）
```

## 环境要求

- Windows 10/11，Python 3.10 或更高（本机验证版本：Python 3.10.20）
- 仅标准库：`wave`、`array`、`json`、`argparse`、`struct`、`subprocess` 等
- 建议用 `python -I`（隔离模式）运行，避免当前目录中同名模块（如 `json.py`）被意外导入

## 一键复现（本地启动方式）

在项目根目录 `project32\` 下依次执行：

```powershell
# 1. 生成确定性测试样本（可重复运行，字节级一致）
python -I tests\make_samples.py

# 2. 运行全部 10 条验收项（含 2 个附加项，共 14 个断言）
python -I tests\run_acceptance.py
```

验收脚本退出码 `0` 表示全部通过，任一失败退出码为 `1` 并打印失败明细。
本机实测结果：**14/14 通过，EXIT=0**（见文末“实际验证结果”）。

## 业务操作样例（CLI）

命令格式：

```
python -I src\wav_merger.py <输入WAV> <标记JSON> <输出WAV> [--report 报告路径] [--crossfade 毫秒]
```

标记 JSON 为区间数组，`start`/`end` 单位为秒、可为小数：

```json
[
  {"start": 0.5, "end": 1.0},
  {"start": 1.5, "end": 2.0}
]
```

### 样例 1：常规合并（输出路径含空格）

```powershell
python -I src\wav_merger.py samples\wav\stereo_source.wav `
  samples\markers\markers_two_halves.json "build\demo output\merged.wav"
```

输出：`merged.wav`（1.0s，44100 帧，立体声 44.1kHz）与同目录 `merged_report.json`。

### 样例 2：连接处 10ms 交叉淡化

```powershell
python -I src\wav_merger.py samples\wav\stereo_levels.wav `
  samples\markers\markers_crossfade.json "build\demo output\crossfaded.wav" --crossfade 10
```

淡化只作用于片段连接处（前段尾部淡出、后段头部淡入），**总帧数与不加淡化完全相同（88200 帧）**。

### 样例 3：区间末尾超出音频长度（自动截断 + 警告，不中断）

```powershell
python -I src\wav_merger.py samples\wav\stereo_source.wav `
  samples\markers\markers_overflow.json "build\demo output\overflow.wav"
```

标记 `end=3.5s` 超出 3.0s 音频，截断到 3.0s，报告 `warnings` 中记录该标记。

### 样例 4：空标记列表（生成 0.5s 静音）

```powershell
python -I src\wav_merger.py samples\wav\stereo_source.wav `
  samples\markers\markers_empty.json "build\demo output\silence.wav"
```

输出 22050 帧（0.5s）立体声静音，警告含“无片段输入”。

### 样例 5：错误输入（非零退出，不生成输出文件）

```powershell
# 8-bit WAV -> stderr 含“不支持的样本格式”，退出码 1
python -I src\wav_merger.py samples\wav\source_8bit.wav `
  samples\markers\markers_two_halves.json "build\demo output\x.wav"

# 不存在的标记文件 -> stderr 含“标记文件不存在”，退出码 1，输出目录无新 WAV
python -I src\wav_merger.py samples\wav\stereo_source.wav `
  samples\markers\missing.json "build\demo output\x.wav"
```

路径均支持 Windows 绝对路径与相对路径，目录/文件名中的空格无需转义（整体加引号即可）。

## JSON 报告格式

默认与输出 WAV 同目录、同名加 `_report.json` 后缀，也可用 `--report` 自定义：

```json
{
  "output_path": "E:\\...\\overflow.wav",
  "source_wav": "E:\\...\\stereo_source.wav",
  "sample_rate": 44100,
  "channels": 2,
  "merged_segments": 1,
  "total_frames": 44100,
  "duration": 1.0,
  "crossfade_ms": 0.0,
  "warnings": [
    "标记 #0 区间末尾 3.5s 超出音频时长 3.0s，已截断到音频末尾"
  ]
}
```

无警告时 `warnings` 为空数组 `[]`。

## 关键设计说明

| 关注点 | 处理方式 |
|---|---|
| 格式校验 | 仅接受 16-bit（`sampwidth==2`）整数 PCM、1~2 声道；8-bit/24-bit/多声道明确报错，退出码 1 |
| 样本边界 | 秒数 × 采样率后 `round` 对齐到整样本帧，按帧（非字节）切片，立体声帧含 L/R 两个样本 |
| 声道处理 | 按帧交错切片与拼接，各声道独立、永不混合；淡化增益逐声道分别计算 |
| 超长区间 | `end` 超界截断到末尾并写警告；`start` 超界或 `start>=end` 属硬错误，直接失败退出 |
| 交叉淡化 | 非重叠线性淡化：前段尾部 N 帧增益 1→0，后段头部 N 帧增益 0→1，N=毫秒×采样率；不重叠所以总样本数不变；N 自动钳制到相邻两段的较短者 |
| 空标记 | 输出 0.5s 全零静音 WAV，保留原声道/采样率，`merged_segments=0`，警告“无片段输入” |
| 失败安全 | 所有输入校验在写文件之前完成；标记缺失/JSON 损坏/格式非法时不创建任何输出 WAV |
| 中文编码 | 程序启动时将 stdout/stderr 重配置为 UTF-8，规避 Windows GBK 控制台/管道的编码崩溃 |

## 实际验证结果（2026-11-14，Windows 11 + Python 3.10.20）

`python -I tests\run_acceptance.py` 实测：**14/14 通过，退出码 0**。

| # | 验收项 | 实测结果 |
|---|---|---|
| 1 | 8-bit WAV 拒绝 | 退出码 1，stderr 含“不支持的样本格式: 样本宽度为 1 字节（8 bit）”，无输出文件 |
| 2 | start=end=1.5 拒绝 | 退出码 1，stderr 含“区间无效，要求 start < end” |
| 3 | 立体声两段各 0.5s | 输出 44100 帧（=22050+22050），2 声道 / 44100Hz / 16-bit 不变 |
| 4 | end 超 0.5s 截断 | 输出 44100 帧有效 WAV，warnings 含“已截断到音频末尾”，退出码 0 |
| 5 | 10ms 交叉淡化 | 淡化/无淡化均为 88200 帧；连接处尾部 1→0、头部 0→1 线性渐变（实测每帧增量约 9） |
| 6 | 报告字段 | 含字符串 `output_path`、整数 `merged_segments`、数组 `warnings`、浮点 `duration` |
| 6b | 无警告 | 正常合并场景 `warnings == []` |
| 7 | 空标记 | 22050 帧（0.5s）立体声全零静音，警告含“无片段输入” |
| 8 | 声道独立 | 左声道全程 +1000 保持偏移，右声道全程 0 静音，无混合 |
| 9a | 标记文件不存在 | 退出码 1，stderr 含“标记文件不存在”，输出目录无新 WAV |
| 9b | JSON 损坏 | 退出码 1，stderr 含“JSON 格式错误”，输出目录无新 WAV |
| 10 | 空格+相对路径 | 在 `spaced dir\output folder\merged track.wav` 成功创建并可回读，帧数正确 |
| 附 | 小数边界对齐 | 0.333s→1.667s 单声道输出 58830 帧，与 `round` 整样本换算一致 |
| 附 | start 超出时长 | 退出码 1，stderr 含“超出音频时长” |
