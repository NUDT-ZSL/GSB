# WAV 静音段批量去除工具

游戏音效素材库整理工具：扫描目录中的 WAV 文件，去除静音段，统一输出
16 位 PCM 单声道音频，并生成可追溯的 JSON 清单。纯 Python 标准库实现
（`wave` / `struct` / `argparse` / `json`），Windows 本地运行，无需网络与第三方依赖。

## 文件说明

| 文件 | 作用 |
|---|---|
| `audio_trimmer.py` | 主工具：扫描、去静音、混单声道、输出清单与摘要 |
| `make_samples.py` | 确定性样本生成器（正弦波/立体声/全静音/损坏头/中文文件名） |
| `verify_acceptance.py` | 9 项验收要求的自动化实测脚本 |
| `demo_input/` `demo_output/` | 演示输入与输出目录 |

## 快速开始（可复现演示）

```powershell
cd E:\Company\demo\projects\project49

# 1. 生成确定性测试样本（含中文文件名、立体声、全静音、损坏头）
python make_samples.py demo_input

# 2. 批量处理（默认阈值 -45dBFS、最短静音 0.3 秒）
python audio_trimmer.py demo_input demo_output

# 3. 查看清单
Get-Content demo_output\manifest.json -Encoding UTF8

# 4. 自定义参数
python audio_trimmer.py demo_input demo_output --threshold -60 --min-silence 0.1

# 5. 运行全部 9 项验收测试
python verify_acceptance.py
```

> 在 GBK 控制台下直接运行即可正常显示中文；如需管道捕获输出，
> 建议加 `-X utf8`（如 `python -X utf8 audio_trimmer.py ...`）。

## 命令行参数

```
python audio_trimmer.py <输入目录> <输出目录> [--threshold DB] [--min-silence 秒] [--manifest 路径]
```

- `--threshold`：静音判定阈值（dBFS），合法范围 **(-90, -20]**，默认 **-45**。
  传 `-90`、`-1` 等非法值时打印参数提示并以退出码 2 退出。
- `--min-silence`：最短静音时长（秒），必须 **> 0**，默认 **0.3**。
  只有不短于该时长的静音段才会被切除，更短的静音（如过零点）保留。
- `--manifest`：清单 JSON 路径，默认 `<输出目录>/manifest.json`。

## 处理规则

1. 扫描输入目录下全部 `.wav`（扩展名不区分大小写，含中文文件名），
   按文件名升序处理，非 WAV 文件忽略。
2. 以样本级精度判定有效样本（|幅值| ≥ 阈值），切除 ≥ 最短静音时长的
   静音段，剩余片段按原顺序拼接为连续音频；输出首尾样本与源非静音段一致。
3. 输出统一为 16 位 PCM 单声道（多声道按帧平均混音），采样率与源一致。
4. 整段静音的源不产生输出文件，清单标记 `无有效音频`、`output` 为 `null`。
5. 损坏/无法读取的文件记入清单 `失败` 及错误信息，不中断批处理。
6. 输出文件名为 `原名_processed.wav`；同名冲突时自动追加序号
   （`_processed_1.wav`、`_processed_2.wav`…），绝不覆盖已有文件。
7. 结束后控制台输出摘要：处理总数、成功数、失败数、无有效音频数，
   并按文件名升序列出失败文件。

## 清单格式（manifest.json）

```json
[
  {
    "source": "原始文件路径",
    "output": "输出文件路径 或 null",
    "status": "成功 | 失败 | 无有效音频",
    "error": "错误信息 或 null"
  }
]
```

## 实际验证结果

`verify_acceptance.py` 对 9 项验收要求逐项实测（每项独立目录、子进程真实运行），
本轮运行结果 **9/9 全部通过**：

- 要求1：两个 WAV + 一个 txt → 清单按文件名升序列出两个 WAV，txt 被忽略
- 要求2：2s 正弦夹 0.5s 静音 → 输出 1.5000s，首尾样本与源非静音段一致
- 要求3：立体声 44100Hz → 输出 1 声道 / 16 位 / 44100Hz
- 要求4：全静音 → 无输出文件，状态 `无有效音频`，`output: null`
- 要求5：损坏文件与正常文件同目录 → 正常文件成功，损坏条目错误信息非空
- 要求6：清单为合法 JSON，字段齐全，成功条目输出文件存在
- 要求7：预置同名输出 → 原文件内容不变，新文件带序号且清单记录一致
- 要求8：`-60/0.1` 正常（退出码 0）；`-90`、`-1` 退出码 2 并给参数提示
- 要求9：三种结局各一 → 摘要显示 总数3/成功1/失败1/无有效音频1 并列出失败名
