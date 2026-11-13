# WAV 静音区间标记工具

音频后期批处理工具：按同一份静音判定配置，扫描目录中的单声道 16 位 PCM WAV
录音，标出每段录音中长度达到门槛的连续静音区间，供剪辑人员定位删减点。
损坏或格式不符的文件跳过并单独记录，不影响其他文件。

- 仅依赖 Python 3.10+ 标准库（`wave` / `array` / `json` / `math` 等），无需第三方库、无需网络
- 全部输出为 UTF-8 编码的合法 JSON
- 不使用 Docker 或任何容器环境

## 1. 文件说明

| 文件 | 作用 |
|---|---|
| `silence_marker.py` | 主工具：配置读取、WAV 解析、静音检测、标记文件与汇总报告输出，含 CLI |
| `make_samples.py` | 生成确定性本地样本（正常 WAV、文本改名的坏 WAV、立体声 WAV、各类配置） |
| `verify.py` | 端到端验收脚本，36 条断言逐条覆盖任务 8 项验收标准 |

## 2. 快速开始（可复现演示）

Windows PowerShell（本机已验证，Python 3.10.20）：

```powershell
# 建议让控制台按 UTF-8 输出，避免 GBK 代码页显示乱码（不影响 JSON 输出文件）
$env:PYTHONIOENCODING='utf-8'

# 步骤 1：生成确定性样本到 demo_data\
python -I make_samples.py demo_data

# 步骤 2：按 config.json 处理，输出到 demo_output\
python -I silence_marker.py demo_data -c demo_data\config.json -o demo_output
```

`-I` 为 Python 隔离模式，忽略环境变量与当前目录隐式导入，防止样本目录中
被植入同名模块（如 `json.py`）干扰解释器加载。

### 命令行参数

```
python -I silence_marker.py <输入目录> [-c 配置.json] [-o 输出目录]
```

- 输入目录：只扫描该目录本身的 `*.wav`（大小写后缀均可），**不递归子目录**，按**文件名字典序**处理
- `-c` 可选；不传、文件不存在、JSON 损坏或字段非法时自动回退默认值并在报告 `config.notes` 注明
- `-o` 可选；默认标记文件与 `report.json` 写在输入目录旁（即输入目录内）

### 配置文件格式

```json
{
  "threshold_db": -40,
  "min_silence_ms": 500
}
```

- `threshold_db`：静音阈值（分贝，相对 16 位满量程 32768），默认 **-40 dB**
- `min_silence_ms`：最短静音时长（毫秒，非负数），默认 **500 ms**
- 支持带 UTF-8 BOM 的配置文件（Windows 记事本 / PowerShell 5.1 常见产物）

## 3. 判定规则与输出格式

### 静音判定

1. 分贝阈值换算线性幅度：`amplitude = 32768 × 10^(threshold_db/20)`，结果裁剪到 `[0, 32768]`
2. 逐采样比较：`|sample| < amplitude` 即静音采样（严格小于，零采样恒为静音）
3. 连续静音采样数达到 `ceil(min_silence_ms × sample_rate / 1000)` 才形成一个区间
4. 区间端点转毫秒并**向下取整**：`ms = sample_index × 1000 // sample_rate`

极端阈值不会崩溃：0 dB 时幅度为 32768，整文件判为静音；-96 dB 时幅度约
0.51，只有纯零采样算静音，仍能正常输出。

### 标记文件 `<文件名>.marks.json`（仅成功文件生成）

```json
{
  "file": "a.wav",
  "sample_rate": 8000,
  "frames": 24000,
  "threshold_db": -40.0,
  "min_silence_ms": 500.0,
  "marks": [
    {"start_ms": 0, "end_ms": 1000},
    {"start_ms": 2000, "end_ms": 3000}
  ]
}
```

### 汇总报告 `report.json`

每个文件条目均含 `file` / `status`（`ok`|`error`）/ `error`（成功为 `null`）/
`marks`（错误条目为空数组，错误数据不混入）；顶层含 `total_marks`（仅统计
成功文件的区间总数）及成功/失败计数；`config.notes` 记录默认值回退原因。

## 4. 演示样本说明

`make_samples.py` 生成的样本均为确定性内容（8 kHz 采样率）：

| 文件 | 内容 | 预期结果（-40 dB / 500 ms） |
|---|---|---|
| `a.wav` | 3 秒：静音 1s → 440Hz 0.46s → 静音 80ms → 440Hz 0.46s → 静音 1s | 2 个区间 `[0,1000]`、`[2000,3000]`；80ms 间隙被门槛过滤 |
| `b.wav` | 2 秒：前 1s 静音 + 后 1s 330Hz 正弦 | 1 个区间 `[0,1000]` |
| `bad.wav` | 纯文本改名为 `.wav` | `status=error`，记录 RIFF 解析失败，不生成标记文件 |
| `stereo_sample.wav` | 立体声 16 位 PCM | `status=error`，记录声道数不符 |
| `config_missing_threshold.json` | 缺少 `threshold_db` | 回退 -40 dB 并在报告注明 |
| `config_empty.json` | 空对象 `{}` | 两个字段均回退默认 |

### 业务操作样例：不同阈值对比

```powershell
# 缺字段配置：报告 notes 中注明"采用默认阈值 -40dB"
python -I silence_marker.py demo_data -c demo_data\config_missing_threshold.json -o demo_output_missing

# 极端阈值 0dB：a.wav 整段静音，得到 [0, 3000] 单区间
'{"threshold_db": 0, "min_silence_ms": 500}' | Out-File -Encoding utf8 demo_data\config_0db.json
python -I silence_marker.py demo_data -c demo_data\config_0db.json -o demo_output_0db

# 极端阈值 -96dB：仅纯零采样命中，a.wav 仍为首尾两段
'{"threshold_db": -96, "min_silence_ms": 500}' | Out-File -Encoding utf8 demo_data\config_96db.json
python -I silence_marker.py demo_data -c demo_data\config_96db.json -o demo_output_96db
```

## 5. 验收方式与实际验证结果

```powershell
$env:PYTHONIOENCODING='utf-8'
python -I verify.py
```

本机实际运行结果：**全部 36 项断言通过 [ALL PASS]，进程退出码 0**。覆盖：

1. 字典序处理、不递归（含 a.wav 在 b.wav 前的顺序断言）
2. 配置字段缺失/非法/损坏 JSON/带 BOM/文件不存在 共 5 类回退场景，报告均注明默认值来源
3. b.wav 前 1 秒静音 → 出现 `start_ms=0` 区间；a.wav 80ms 短间隙被 500ms 门槛过滤
4. 0 dB 与 -96 dB 均输出标记且正常退出；另验证幅度换算有限非负
5. `a.wav.marks.json` 存在、含 `marks` 数组、端点均为 `int` 毫秒；非整除采样率下验证向下取整（333 而非 334）
6. `report.json` 含 `total_marks`，每条目 `marks`/`status`/`error` 齐全
7. 文本改名的 `bad.wav` 记录解析失败、立体声文件记录格式不符，均不产生标记文件，合法文件结果不受影响
8. 所有输出以 UTF-8 重新读取并 `json.load` 成功，标记文件均含 `marks` 数组

退出码约定：全部文件处理完毕（含部分文件失败）返回 `0`；输入目录不存在等
致命错误返回 `2`。

## 6. 在真实录音目录上使用

```powershell
# 不传 -o 时，标记文件与 report.json 直接写到录音目录
python -I silence_marker.py D:\recordings\batch01 -c D:\recordings\silence.json
```

输出目录中的 `report.json` 可直接交给后续流程：剪辑人员按每个
`*.marks.json` 的 `start_ms/end_ms` 定位删减点，失败文件清单从报告
`status=error` 条目中单独提取返工。
