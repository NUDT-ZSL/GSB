# 播客语音响度归一化工具

把单声道 16 位 44.1kHz PCM WAV 长对话中音量不一致的语音段调整到统一响度，
静音段保持原样，输出处理后的 WAV 与 JSON 报告。**仅使用 Python 标准库，
Windows cmd 下可直接运行，无需安装任何第三方包。**

## 环境要求

- Python 3.10 或更高版本（开发验证版本：Python 3.10.20，Windows 11）
- 无第三方依赖（仅用 `wave` / `array` / `json` / `statistics` / `math` /
  `os` / `argparse` / `sys` / `time`）
- 不使用 Docker 或其他容器

## 文件说明

| 文件 | 作用 |
|---|---|
| `loudness_normalizer.py` | 主程序：分段、RMS 计算、增益规划、截断、输出 |
| `make_samples.py` | 确定性生成 6 个测试 WAV 样本（无随机数，可重复生成） |
| `tests/run_tests.py` | 覆盖任务书 15 条验收的自动化测试（19 项检查） |

## 本地启动方式

```cmd
cd /d E:\Company\demo\projects\project53

REM 1. 生成确定性样本到 samples\
python make_samples.py

REM 2. 执行响度归一化（输出目录不存在会自动创建）
python loudness_normalizer.py samples\sample_44k_two_speech.wav demo_out\two_speech

REM 3. 运行全部验收测试
python tests\run_tests.py
```

命令行参数：

```
python loudness_normalizer.py <input.wav> <output_dir>
```

- 输出 WAV：`<output_dir>\<输入名>_normalized.wav`
- JSON 报告：`<output_dir>\<输入名>_report.json`
- 退出码：`0` 成功（含无语音情形）；`2` 命令行参数错误；
  `3` 处理或写入错误（报告含 `error` 字段，且不生成输出 WAV）

## 业务操作样例（均已在本机 cmd 实测）

### 样例 1：正常处理两段不同响度语音

```cmd
python loudness_normalizer.py samples\sample_44k_two_speech.wav demo_out\two_speech
```

控制台输出：

```
message: ok
target_rms: 0.149997
adjusted_segments: 2
average_gain: 1.125
elapsed_time: 0.289 s
output WAV: demo_out\two_speech\sample_44k_two_speech_normalized.wav
report: demo_out\two_speech\sample_44k_two_speech_report.json
```

两段语音 RMS 约为 0.1 与 0.2，中位数目标 `target_rms ≈ 0.15`；
两段增益分别约 1.5 与 0.75，平均增益 1.125。

### 样例 2：格式不符（8kHz 样本）

```cmd
python loudness_normalizer.py samples\sample_8k.wav demo_out\bad_format
echo %ERRORLEVEL%
```

退出码 `3`，目录中只有报告、没有 WAV：

```json
{
  "error": "sample rate must be 44100 Hz, got 8000 Hz"
}
```

### 样例 3：全静音样本

```cmd
python loudness_normalizer.py samples\sample_44k_all_silence.wav demo_out\silence
```

退出码 `0`，输出 WAV 与输入**逐字节相同**，报告：

```json
{
  "message": "no speech detected",
  "target_rms": null,
  "adjusted_segments": 0,
  "average_gain": null,
  "elapsed_time": 0.052,
  "segments": [
    { "start": 0.0, "end": 6.0, "type": "silence",
      "original_rms": 0.0, "target_rms": null,
      "gain": 1.0, "adjusted": false }
  ]
}
```

### 样例 4：增益上限与溢出截断

`sample_44k_gain_clamp.wav` 第一段 RMS≈0.1（含 5 个 32000 尖峰采样），
第二段 RMS≈0.5，中位数目标≈0.3。第一段理想增益约 3 倍，被截断为 **2.0**，
最终 RMS≈0.2；尖峰 `32000×2=64000` 被限制到 **32767**，输出不含越界值。

### 样例 5：输出目录不可写

把输出路径指向一个已存在的文件时，进程不崩溃、退出码 `3`，
错误报告回退写到输入文件同目录：

```cmd
echo occupied > blocked.txt
python loudness_normalizer.py samples\sample_44k_two_speech.wav blocked.txt
```

## JSON 报告字段

顶层字段：

| 字段 | 含义 |
|---|---|
| `message` | `ok` / `no speech detected`；错误时为 `error` 字段 |
| `target_rms` | 所有语音段 RMS 的中位数（满量程归一化）；无语音时为 `null` |
| `adjusted_segments` | 实际施加增益的段数 |
| `average_gain` | 实际调整段的增益平均值；无调整段时为 `null` |
| `elapsed_time` | 处理耗时（秒，3 位小数） |
| `segments` | 按 `start` 升序的片段列表（同 start 稳定排序） |

每个片段含任务要求的全部字段：`start`、`end`（秒，3 位小数）、
`original_rms`、`target_rms`、`gain`，并附带 `type`（speech/silence）
与 `adjusted` 布尔值。静音段与不足 0.1 秒的短语音段 `gain` 恒为 `1.0`。

## 算法说明

1. **分段**：10ms 无重叠分析帧，逐帧计算满量程归一化 RMS；
   RMS ≤ `0.01` 判为静音帧，相邻同标签帧合并为片段。
2. **目标响度**：所有语音段（含过短不调整的段）RMS 的中位数。
3. **增益规划**：理想增益 = 目标 RMS ÷ 段 RMS，截断到 `[0.5, 2.0]`；
   时长 < 0.1 秒的语音段与静音段不调整（增益 1.0）。
4. **应用与防溢出**：逐采样乘增益，四舍五入后限制在
   `[-32768, 32767]`；输出帧数与输入完全一致。
5. **无语音**：直接复制输入文件字节，保证输出与输入逐字节相同。

## 已验证的验收结果

`python tests\run_tests.py` 实测结果：**19 checks, 19 passed, 0 failed**，
对应任务书 T1–T15（T10/T14 各含一个附加边界检查）。
