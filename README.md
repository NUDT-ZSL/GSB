# 游戏音效批处理工具（audio_pipeline）

对按子目录组织的单声道 16bit PCM WAV 音效统一执行**静音裁剪、线性淡入淡出、增益**，
并为每个成功处理的素材生成引擎可读的同名 JSON 元数据。

- **仅依赖 Python 3.10+ 标准库**（`wave` / `struct` / `json` / `pathlib` / `dataclasses` 等），无需 pip 安装任何第三方包。
- 不使用 Docker 或其他容器；所有测试样本均为本地确定性生成，不访问网络。
- 无效素材（非 WAV、8bit、采样率越界、文件损坏）被**跳过并在报告中说明原因**，不影响其他素材。
- 配置无法解析或取值非法时，**在处理任何素材之前直接报错退出，不创建任何输出**。

---

## 1. 目录结构

```
project50/
├── audio_pipeline/            # 核心包（纯标准库）
│   ├── __init__.py
│   ├── config.py              # 配置加载与严格校验（ConfigError）
│   ├── wav_io.py              # WAV 读写与格式校验（只支持 单声道/16bit/PCM/8k–48k）
│   ├── processing.py          # 三个 DSP 步骤：trim / fade / gain（含削波保护）
│   ├── pipeline.py            # 目录遍历、按序处理、WAV+JSON 输出、报告汇总
│   └── cli.py                 # 命令行入口与控制台报告
├── process_audio.py           # 业务入口（支持 python -I 隔离模式）
├── run_tests.py               # 单元测试入口
├── verify_outputs.py          # 对真实产出文件的独立数值验收脚本
├── tests/
│   ├── make_fixtures.py       # 确定性测试样本生成器
│   └── test_audio_pipeline.py # 30 个 unittest 用例，逐条对应 10 项验收
├── configs/                   # 配置样本（完整/缺省/空/各类非法）
│   ├── full.json
│   ├── partial.json
│   ├── empty.json
│   ├── order_gain_first.json
│   ├── bad_gain.json
│   ├── bad_threshold.json
│   ├── bad_fade.json
│   ├── bad_order.json
│   └── malformed.json
├── demo.ps1 / demo.sh         # 一键演示（PowerShell / Bash）
└── testdata/                  # 运行后生成：input 为样本，output_* 为产出
```

---

## 2. 配置文件说明

配置为一个 JSON 对象，**所有字段均可缺省**，缺省值见下表。未知字段、类型错误、
取值越界一律判定为配置错误。

| 字段 | 含义 | 允许范围 | 默认值 |
|------|------|----------|--------|
| `gain_db` | 增益（分贝） | `-20.0` ～ `+20.0` dB | `0.0` |
| `threshold_db` | 静音判定阈值（满幅相对分贝） | `-60.0` ～ `-20.0` dB | `-50.0` |
| `fade_ms` | 首尾线性淡入淡出时长；`0` 表示不做淡变 | `0` 或 `10.0` ～ `500.0` ms | `0.0` |
| `order` | 处理顺序，3 个步骤各出现一次 | 元素仅可为 `trim` / `fade` / `gain` | `["trim","fade","gain"]` |

完整配置示例（`configs/full.json`）：

```json
{
  "gain_db": 6.0,
  "threshold_db": -45.0,
  "fade_ms": 100.0,
  "order": ["trim", "fade", "gain"]
}
```

空对象 `{}` 与只含部分字段的配置均合法，缺失项自动取默认值。

**严格校验行为**：布尔值不接受为数字、`NaN`/`Infinity` 拒绝、未知键拒绝、
`order` 缺步骤/重复/含未知步骤拒绝。任何错误都带中文说明，例如：

```
配置错误，已取消处理（未处理任何素材）：配置项 'gain_db' 取值 40.0dB 超出允许范围 [-20.0, 20.0]dB
```

---

## 3. 本地启动方式

### 前置要求

- Python 3.10 或更高版本（开发与验证环境为 Python 3.10.20，Windows 11）。
- 无需安装任何第三方库。

### 一键演示（推荐先跑）

PowerShell：

```powershell
./demo.ps1
```

Git Bash / macOS / Linux：

```bash
bash demo.sh
```

脚本会依次：生成测试样本 → 完整配置批处理 → 独立数值验收 → 非法配置拦截演示。

### 分步命令（业务操作样例）

```powershell
# 1) 生成确定性测试样本（含有效 WAV、8bit WAV、文本、损坏 WAV、96kHz WAV、子目录）
python -I tests/make_fixtures.py --out testdata/input

# 2) 完整配置批处理，并落盘 JSON 报告
python -I process_audio.py `
    --input  testdata/input `
    --output testdata/output_full `
    --config configs/full.json `
    --report testdata/output_full/report.json

# 3) 空配置（全部走默认值：0dB / -50dB / 0ms / trim→fade→gain）
python -I process_audio.py -i testdata/input -o testdata/output_empty -c configs/empty.json

# 4) 自定义顺序（先增益→裁剪→淡变）
python -I process_audio.py -i testdata/input -o testdata/output_order -c configs/order_gain_first.json

# 5) 非法配置：退出码 2，输出目录不会被创建
python -I process_audio.py -i testdata/input -o testdata/output_bad -c configs/bad_gain.json

# 6) 独立数值验收 + 单元测试
python -I verify_outputs.py
python -I run_tests.py
```

> `-I`（隔离模式）保证不读取当前目录下任何同名恶意模块（如伪造的 `json.py`），
> 与“下载素材不可信”的安全约束一致；两个入口脚本均已自行处理该模式下的模块搜索路径。

### 命令行参数与退出码

```
python -I process_audio.py --input <输入目录> --output <输出目录> --config <配置JSON> [--report <报告JSON>]
```

| 退出码 | 含义 |
|--------|------|
| `0` | 运行完成，所有有效素材成功（允许存在被跳过的无效素材） |
| `2` | 配置错误，未处理任何素材、未创建输出 |
| `3` | 存在素材处理失败（报告 `failed` 段可定位） |
| `4` | 参数或输入目录错误 |

---

## 4. 输出与元数据

输出目录**保持输入的相对子目录结构**，每个成功素材产出同名 `.wav` 与 `.json`：

```
testdata/output_full/
├── report.json                 # 本次运行的完整报告（成功/跳过/失败 + 每步细节）
├── sfx/
│   ├── clean_44100.wav
│   ├── clean_44100.json
│   ├── short_8000.wav
│   ├── short_8000.json
│   ├── tone_22050.wav
│   └── tone_22050.json
└── sub/
    ├── nested_16k.wav
    └── nested_16k.json
```

每个素材的同名 JSON（引擎可读，5 个字段）：

```json
{
  "source_file": "clean_44100.wav",
  "duration_seconds": 1.02,
  "sample_rate": 44100,
  "bit_depth": 16,
  "channels": 1
}
```

- `duration_seconds` 保留 3 位小数；与输出 WAV 实际帧数/采样率的误差小于 1ms。
- 输出 WAV 的采样率、位深、声道数与输入**完全一致**，不做重采样或位深转换。

控制台报告与 `report.json` 按相对路径区分三类结果：

- **成功 [✓]**：输出路径、时长、三个步骤各自的实际作用（裁了多少样本、淡变样本数、是否触发峰值保护）。
- **跳过 [⊘]**：无效素材及具体原因（非 WAV / 8bit / 采样率越界 / 数据损坏等）。
- **失败 [✗]**：读写异常等个别文件问题，隔离到该文件，不中断其余素材。

---

## 5. 处理规则与关键设计

### 5.1 支持的输入格式

- 未压缩 PCM、单声道、16bit、采样率 8000–48000Hz（含边界）。
- 非 `.wav` 扩展名直接按“非 WAV”跳过；WAV 容器但压缩格式/位深/声道/采样率不符，
  或数据长度与头部帧数不一致（损坏），按“WAV 格式不符”跳过并给出原因。

### 5.2 静音裁剪（trim）

- 阈值按满幅换算：`|样本| < 32767 × 10^(threshold_db/20)` 判为静音。
- 从首尾向内找到第一个/最后一个超过阈值的样本，裁剪外侧静音，
  **每侧至少保留 10ms 静音**（按采样率向上取整，保证实际保留不少于 10ms）。
- 整段都低于阈值时保留原样并在报告中标注，避免产出空音频。

### 5.3 线性淡入淡出（fade）

- 淡入系数 `i/N`（首样本为 0，线性上升）；淡出在最后一个样本归零。
- 淡入与淡出长度自动防止重叠；`fade_ms=0` 时跳过该步骤。

### 5.4 增益与削波保护（gain）

- 线性系数 `10^(gain_db/20)`，如 +6dB ≈ 1.9953 倍。
- 处理全程使用 float，仅在写盘时四舍五入量化回 16bit，避免多步骤累计量化误差。
- **绝不削波**：若放大后峰值超过满幅，不做逐样本硬截断，而是对整段统一压缩到
  刚好不溢出（全局峰值保护），并在该素材的报告步骤中标记“已触发峰值保护”。

### 5.5 处理顺序（order）

- 默认 `trim → fade → gain`；可在配置中显式指定为三步的任意排列。
- 顺序真实作用于最终输出：例如先增益会把低电平噪声抬过裁剪阈值，改变裁剪边界
  （单元测试 `test_order_changes_result_when_noise_crosses_threshold` 专门验证）。
- 配置先于一切文件操作完成校验，因此配置非法时输出目录不会被创建。

---

## 6. 实际验证结果记录

验证环境：Windows 11 + Python 3.10.20，全部命令以 `python -I` 运行。

### 6.1 单元测试（30 个用例，全部通过）

```
Ran 30 tests in 3.1s
OK
```

覆盖关系（`tests/test_audio_pipeline.py`）：

| 任务验收 | 对应测试类/用例 |
|----------|-----------------|
| 1 WAV 参数原样 | `TestAcceptance01WavParams`（44100/22050/8000/16000 多采样率） |
| 2 增益不削波 | `TestAcceptance02Gain`（+6dB 倍数 ±5%、峰值保护、端到端无溢出） |
| 3 静音裁剪 | `TestAcceptance03Trim`（减约 0.98s、每侧 ≥10ms、全静音保护、阈值边界） |
| 4 淡入淡出 | `TestAcceptance04Fade`（恒定信号严格单调、淡出归零、时长边界） |
| 5 处理顺序 | `TestAcceptance05Order`（两种顺序分别等于手动链、顺序改变结果、order 校验） |
| 6 JSON 元数据 | `TestAcceptance06Metadata`（时长误差 <1ms、字段与 WAV 头一致） |
| 7 跳过无效素材 | `TestAcceptance07SkipInvalid`（txt/8bit/96k/损坏均列原因，有效素材全成功） |
| 8 默认值 | `TestAcceptance08Defaults`（空对象、部分字段、端到端两配置） |
| 9 非法配置零处理 | `TestAcceptance09BadConfig`（越界/语法错/未知键/类型错，均无输出） |
| 10 目录结构 | `TestAcceptance10Structure` + `TestCliAndReport`（结构保持、报告区分、退出码） |

### 6.2 对真实产出的独立数值验收（`verify_outputs.py`，全部 PASS）

```
[PASS] 验收1 WAV头一致: 采样率 44100Hz / 位深 16bit / 声道 1（输入 44100/16/1）
[PASS] 验收2 +6dB峰值≈2倍且不削波: 输入峰值 16383 -> 输出峰值 32688，倍数 1.9952（±5%内），最大值 32688 <= 32767
[PASS] 验收3 静音裁剪: 输入 2.000s -> 输出 1.020s，减少 0.9800s；头/尾静音各 441 样本(10.00ms)
[PASS] 验收4 100ms线性淡入: 后半段线性系数最大偏差 0.0700%（<0.5%），首样本=0
[PASS] 验收6 JSON元数据: JSON时长 1.02s vs 实际 1.019977s，误差 0.0227ms
[PASS] 验收10 目录结构保持+跳过素材无输出: 8 个预期文件全部就位；8bit WAV 与 txt 均无输出
全部验收通过 [OK]
```

### 6.3 非法配置拦截（5 种配置，全部退出码 2 且无输出目录）

| 配置 | 报错要点 | 输出目录 |
|------|----------|----------|
| `bad_gain.json`（+40dB） | `gain_db 取值 40.0dB 超出允许范围 [-20.0, 20.0]dB` | 未创建 |
| `bad_threshold.json`（-10dB） | `threshold_db 取值 -10.0dB 超出允许范围 [-60.0, -20.0]dB` | 未创建 |
| `bad_fade.json`（5ms） | `fade_ms 取值 5.0ms 超出允许范围 [10.0, 500.0]ms` | 未创建 |
| `bad_order.json`（含 boost） | `order 含未知步骤 'boost'` | 未创建 |
| `malformed.json`（语法错） | `不是合法 JSON（第 2 行第 1 列）` | 未创建 |

### 6.4 开发过程中通过测试发现并修复的真实缺陷

1. Python 3.10 的 `wave.open()` 不接受 `pathlib.Path`（3.12 才支持），统一改为传字符串路径。
2. 淡出系数索引写反，导致尾部未正确归零；已修正并由 `test_fade_out_descends_to_zero` 固化。
3. Windows 控制台管道/重定向默认 GBK 导致中文报告乱码，CLI 与脚本统一强制 UTF-8 输出。
