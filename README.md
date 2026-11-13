# 播客片段预处理工具（podcast_prep）

对每期节目母版切出的单声道 16bit/44.1kHz PCM WAV 片段做剪辑前统一处理：

- **静音标记**：检测连续静音区间（低于阈值且持续不少于最小时长），以毫秒起点 + 毫秒时长写入报告；
- **平滑过渡**：片段首尾各 0.2 秒线性淡入/淡出到零，中段逐采样保持原样；
- **规格把关**：仅处理单声道 / 44100Hz / 16bit PCM，其余在报告中写明跳过原因；损坏文件记为失败，不影响其余片段；
- **JSON 报告**：含总数、各状态计数与逐文件条目（名称、状态、静音区间、跳过原因、覆盖标记），条目顺序与扫描/清单顺序一致。

仅依赖 Python 标准库，Python 3.10+。

## 快速开始（可复现演示）

```bash
# 1. 确定性生成测试样本（合规 / 双声道 / 8bit / 48kHz / 损坏 / 全静音 / 满幅 / 含哼声）
python make_samples.py demo/input

# 2. 运行预处理（默认阈值 -40dBFS、最短静音 0.5s）
python podcast_prep.py demo/input -o demo/output

# 3. 查看报告
cat demo/output/report.json

# 4. 一键跑全部 15 项验收
python verify.py
```

## 命令行参数

```
python podcast_prep.py <输入目录> [-o 输出目录]
    [--threshold-db -40]      静音阈值（dBFS）
    [--min-silence 0.5]       静音最小时长（秒）
    [--list 清单.txt]         只处理清单内文件（忽略空行与 # 注释）
    [--report 报告路径]       默认 <输出目录>/report.json
    --help / --version
```

## 业务操作样例

**日常批处理**（扫描整个目录）：

```bash
python podcast_prep.py recordings/ep042 -o output/ep042
```

**按清单处理**（运营给定的本期片段清单，顺序即报告顺序）：

```bash
# list.txt：每行一个文件名，# 开头为注释
python podcast_prep.py recordings/ep042 -o output/ep042 --list list.txt
```

**调参重跑**（现场底噪偏高，收紧阈值并放宽最短时长）：

```bash
python podcast_prep.py recordings/ep042 -o output/ep042 --threshold-db -35 --min-silence 0.8
```

**修正后重跑**：被跳过/失败的片段会在 stderr 汇总列出文件名，本次运行退出码仍为 0；
修正源文件后重跑同一命令即可，已存在的同名输出会被覆盖并在报告条目标注 `"overwritten": true`。

## 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 运行完成（即使存在跳过/失败片段，便于修正后重跑） |
| 2 | 致命错误：输入目录不存在、清单文件不可读、参数错误 |

## 文件说明

- `podcast_prep.py` — 主工具
- `make_samples.py` — 确定性测试样本生成器（无随机数，重复运行结果一致）
- `verify.py` — 自动化验收脚本，覆盖任务要求 1–15
