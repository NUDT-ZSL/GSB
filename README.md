# 鼓组多轨对齐工具

以基准轨为准测算各分轨实际起始偏移，与描述 JSON 中的期望偏移比较，产出逐轨对齐报告、对齐后的分轨 WAV 及可导入剪辑软件的片段包（对齐 WAV + 汇总清单）。

## 环境

- Python 3.10+，依赖 `numpy`、`soundfile`（无容器，纯本地运行）
- 安装依赖：`pip install numpy soundfile`

## 文件说明

| 文件 | 说明 |
|---|---|
| `generate_samples.py` | 确定性生成样例（固定随机种子）：基准轨 `reference.wav`、5 条分轨 WAV（每条 10 秒）与描述 `tracks.json` |
| `align_tracks.py` | 对齐工具主程序（命令行入口） |
| `run_checks.py` | 验收检查脚本，覆盖需求 1–10 |
| `samples/` | 已生成的本地样例输入 |
| `output/` | 对齐结果输出（片段包，与输入目录分离） |

## 启动说明

```bash
# 1. 生成确定性样例（已有 samples/ 可跳过）
python generate_samples.py --out samples

# 2. 运行对齐（输入与输出目录必须分离）
python align_tracks.py --input samples --output output

# 3. 运行验收检查
python run_checks.py
```

运行后终端打印：每条分轨名称与期望偏移、逐轨对齐报告、片段包路径及汇总数量（对齐片段数 / 需返工数 / 缺失不可用数）。

## 输入格式（tracks.json）

```json
{
  "reference": "reference.wav",
  "sample_rate": 44100,
  "tracks": [
    {"name": "kick", "file": "kick.wav", "channels": 1, "expected_offset_ms": 0.0}
  ]
}
```

`expected_offset_ms` 可缺省，缺省时报告提示“期望偏移未知”，该轨仍照常对齐出片段。

## 输出（片段包，写入独立输出目录，不修改输入文件）

- `<原轨名>_aligned.wav` — 每条可用分轨的对齐片段，长度与基准轨相差不超过一个采样帧，起点为实测偏移处
- `alignment_report.txt` — 逐轨对齐报告，按分轨名升序，每行含实测/期望偏移、误差、是否需返工（误差 >5ms 标“是”）与备注
- `manifest.csv` — 汇总清单，逐条对应原分轨名与偏移（UTF-8 BOM，可直接用 Excel 打开）

异常处理：分轨文件缺失/损坏时在报告中列出并跳过；采样率与描述不符时标注该轨不可用；均不影响其余分轨对齐。

## 实际检查结果（2026-11-15，本地 Windows 11，Python 3.10.20，numpy 2.2.6 / soundfile 0.14.0）

`python run_checks.py` 实测输出：**10/10 项通过**。

| # | 检查项 | 结果 |
|---|---|---|
| 1 | 运行后输出每轨名称与期望偏移毫秒值 | PASS |
| 2 | 偏移误差超 5ms 的分轨标注需返工（snare +12.00ms、overhead +8.00ms 标“是”） | PASS |
| 3 | 对齐片段与基准轨时长差 ≤1 帧，重新测残余偏移均为 0 帧 | PASS |
| 4 | 片段包含全部 5 条分轨片段，manifest.csv 逐条对应原分轨名与偏移 | PASS |
| 5 | 删除 tom.wav 后其余 4 轨正常出片段，报告列出 tom 缺失 | PASS |
| 6 | hihat 改为 48000Hz 后报告标注“采样率不符”，其余 4 轨结果与基线一致 | PASS |
| 7 | 输出为 `原轨名_aligned.wav`，输入目录文件 SHA-256 运行前后不变 | PASS |
| 8 | 报告行序为分轨名升序（hihat/kick/overhead/snare/tom），每行含误差值与返工标记 | PASS |
| 9 | 删除 snare 期望偏移字段后报告提示“期望偏移未知”，5 轨均正常出片段 | PASS |
| 10 | 命令行打印片段包路径与汇总数量（对齐 5 / 需返工 2 / 不可用 0） | PASS |

基准运行报告（`output/alignment_report.txt`）：

```
分轨名             实测偏移(ms)     期望偏移(ms)    误差(ms)    需返工  备注
------------------------------------------------------------
hihat              +2.99        +3.00     -0.01      否
kick               +0.00        +0.00     +0.00      否
overhead           +8.00        +0.00     +8.00      是
snare             +12.00        +0.00    +12.00      是
tom                +2.00        +5.00     -3.00      否
```
