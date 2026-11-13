# 音频片段拼接与静音复核（project28）

把 JSON 清单中按顺序排列的多段 **PCM WAV** 短片段拼成一条连续音轨，并输出 JSON 复核报告，标出拼接后幅度过低、可能属于“意外静音”的时间段，供人工复核。

- 纯 Python 3.10+ 标准库，无第三方依赖，不使用 Docker。
- 样本由代码确定性生成，不依赖任何外部音频附件。
- 任何失败都不会留下半成品：校验全部通过后才开始写文件，先写临时文件再原子落盘。

## 文件结构

| 路径 | 作用 |
| --- | --- |
| `audio_patchwork.py` | 主程序（命令行入口） |
| `make_samples.py` | 生成确定性 PCM WAV 样本与清单 |
| `tests/test_audio_patchwork.py` | 57 项 `unittest` 测试（单元 + 端到端 + 真实子进程 CLI） |
| `demo.py` | 一键演示 13 条验收场景 |

## 启动方式（Windows / PowerShell）

```powershell
cd E:\Company\demo\projects\project28
python --version                 # 需 3.10 或更高
python -m unittest discover -s tests -v
python demo.py                   # 一键演示并逐条输出 PASS/FAIL
```

`python demo.py --keep` 会保留 `demo_workspace/` 下的演示产物，便于查看实际生成的 WAV/JSON。

## 业务操作样例

### 1. 生成样本

```powershell
python make_samples.py --clean
```

生成 8 个演示场景，每个场景自带 `manifest.json`：

```
samples/
  basic/     3 段 16bit 单声道（顺序拼接 / stdout 契约）
  silence/   0.250s 音 + 0.500s 静音 + 0.250s 音（0.5 秒静音验收）
  stereo/    2 段立体声（左 440Hz / 右 880Hz，交错顺序验收）
  mono8/     2 段 8bit 单声道（位深保留）
  bad_rate/  混入 16000Hz 片段（采样率不一致验收）
  empty/     空片段列表（“无片段可拼接”）
  missing/   引用不存在的 clips/absent.wav
  existing/  预置 out/track.wav（不覆盖验收）
```

### 2. 正常拼接

```powershell
python audio_patchwork.py samples/silence/manifest.json -o out
```

标准输出（满足“片段数 / 输出 / 静音段数”契约）：

```
片段数: 3
输出: E:\Company\demo\projects\project28\out\track.wav
报告: E:\Company\demo\projects\project28\out\report.json
静音段数: 1
```

`out/report.json`：

```json
{
  "total_duration": 1.0,
  "sample_rate": 8000,
  "channels": 1,
  "bit_depth": 16,
  "clip_count": 3,
  "amplitude_threshold": 0.01,
  "min_silence_duration": 0.1,
  "silence_segments": [
    { "start": 0.25, "end": 0.75, "duration": 0.5 }
  ]
}
```

### 3. 自定义静音参数

```powershell
python audio_patchwork.py samples/silence/manifest.json -o out --threshold 0.02 --min-silence 0.6
```

0.500 秒静音短于 0.6 秒最短时长，`silence_segments` 为 `[]`。

### 4. 省略输出目录（落到当前工作目录）

```powershell
mkdir demo_cwd; cd demo_cwd
python ..\audio_patchwork.py ..\samples\basic\manifest.json
# -> demo_cwd/track.wav 与 demo_cwd/report.json
```

### 5. 失败场景

```powershell
python audio_patchwork.py samples/bad_rate/manifest.json -o out
# 错误: 片段格式不一致（基准为首个片段 …/ok.wav，8000 Hz / 16 bit / 1 声道）:
# …/wrong.wav:
#   - 采样率不匹配: 16000 Hz，期望 8000 Hz
# 退出码 1，不生成 track.wav / report.json
```

```powershell
python audio_patchwork.py samples/empty/manifest.json -o out   # 无片段可拼接（退出码 2）
python audio_patchwork.py samples/missing/manifest.json -o out # 片段文件不存在: …/absent.wav
python audio_patchwork.py samples/existing/manifest.json -o samples/existing/out
# 错误: 输出音轨已存在，拒绝覆盖: …/track.wav（原文件字节不变）
```

## 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `manifest`（位置参数） | — | JSON 清单路径，缺失/不可解析即终止 |
| `-o, --output-dir` | 当前工作目录 | 输出目录；清单中的相对 `output`/`report` 以此为根 |
| `--threshold` | `0.01` | 静音幅度阈值（归一化 0~1） |
| `--min-silence` | `0.1` | 最短静音时长（秒） |

## 清单格式

```json
{
  "clips": ["clips/a.wav", "clips/b.wav"],
  "output": "track.wav",
  "report": "report.json"    // 可省略，默认 <输出文件名>_report.json
}
```

- `clips` 必须是非空字符串数组；**顺序即拼接顺序**。
- 片段相对路径以**清单文件所在目录**为基准解析。
- `output` / `report` 相对路径以**命令行输出目录**为基准解析；可写子目录（自动创建）。

## 关键行为与实现说明

- **格式一致性**：所有片段须与首个片段在采样率、位深、声道数上完全一致；报告会逐个列出具体不匹配项（含文件路径、实际值、期望值）。位深支持 8/16/24/32 bit PCM；非 PCM（如压缩 WAV）拒绝。
- **拼接**：直接顺序连接各片段的原始帧字节，不改动样本，因此声道交错与位深原样保留。WAV 头由 `wave` 模块按原格式写入。
- **静音判定**：以“帧”为单位；多声道时**所有声道**的样本绝对值都低于阈值才算静音帧。幅度阈值按位深换算为整数阈值（8bit 按无符号减 128 后取绝对值）。持续时长 ≥ 最短静音时长（按帧数向上取整）才报告。起止时间四舍五入到三位小数。
- **原子性**：
  1. 先校验清单、路径、全部片段格式，全部通过后才写文件；
  2. 音轨与报告先写入各自目标目录下的临时文件；
  3. 音轨用“仅当不存在时创建”的方式安装（硬链接，失败回退到独占创建），**绝不覆盖已有文件**；
  4. 报告在成功安装音轨后原子替换；若报告安装失败，撤回本次新建的音轨；
  5. 任何异常都会清理临时文件。
- **报告语义**：失败时完全不触碰报告路径；已有报告内容保持不变。
- **退出码**：`0` 成功；`1` 业务错误（清单/片段/格式/输出冲突/参数）；`2` 空片段列表。

## 实际验证结果

| 验收项 | 结果 |
| --- | --- |
| 1 清单缺失/非 JSON 报错且无输出 | 通过（退出码 1，明确提示） |
| 2 采样率不一致指出具体项且无产物 | 通过（`采样率不匹配: 16000 Hz，期望 8000 Hz`） |
| 3 时长=之和，样本字节=顺序连接，头信息正确 | 通过（0.600s，9600 字节逐字节相等，8000Hz/16bit/1ch） |
| 4 0.500s 静音起止三位小数 | 通过（`start=0.25, end=0.75, duration=0.5`） |
| 5 无静音时 `silence_segments == []` | 通过（`total_duration=0.3`） |
| 6 空列表提示“无片段可拼接”且无新增文件 | 通过（退出码 2） |
| 7 片段缺失报告路径且无音轨 | 通过（含绝对路径 `…\absent.wav`） |
| 8 输出存在时报错且原文件不变 | 通过（前后 sha256 一致） |
| 9 失败不产生/不修改报告 | 通过（sentinel 内容 sha256 不变） |
| 10 stdout 含片段数/输出/静音段数 | 通过 |
| 11 单/立体声声道数保持、交错正确 | 通过（1ch/2ch；字节与输入顺序连接一致） |
| 12 参数默认 0.01/0.1 与自定义值 | 通过（默认检出；0.6s 阈值下不报告） |
| 13 省略输出目录落到当前工作目录 | 通过（子进程实跑验证） |

`python -m unittest discover -s tests` 共 **57 项测试全部通过**；`python demo.py` 共 **19 项场景断言全部通过**。
