# 16 位单声道 PCM WAV 对白片段处理流水线

仅依赖 **Python 3.10+ 标准库**（`wave` / `json` / `argparse` / `logging` / `array` /
`math` / `struct`），无需安装任何第三方库，不使用容器。

## 1. 文件说明

| 文件 | 作用 |
|---|---|
| `process.py` | 主处理程序：校验 → 裁剪 → 重采样 → 响度归一化 → 拼接 → 报告/日志 |
| `make_samples.py` | 确定性生成测试 WAV 样本（含非 PCM、8 位、立体声、缺失引用）与两份编辑清单 |
| `verify.py` | 端到端自动化验收脚本，覆盖任务书全部 8 个验收点（共 29 项断言） |
| `run_demo.ps1` / `run_demo.sh` | 一键演示脚本（PowerShell / Bash） |

## 2. 本地启动方式（可复现）

```powershell
# 前置：python --version 确认 3.10+（实测 3.10.20）
# 方式 A：一键演示（生成样本 + 两次处理 + 29 项验收）
./run_demo.ps1

# 方式 B：分步执行
python -I make_samples.py --workdir work
python -I process.py --manifest work/samples/edit_list.json `
                      --output   work/output/dialogue_main.wav `
                      --report   work/output/report_main.json `
                      --log      work/output/run_main.log
python -I verify.py
```

Bash 环境把 `python` 换成 `python3`，或直接 `bash run_demo.sh`。
`-I` 为隔离模式，忽略环境变量与当前目录中的同名模块（防止 planted `json.py` 等）。

## 3. 编辑清单格式

```json
{
  "target_sample_rate": 44100,
  "segments": [
    {
      "file": "good_2s_22050.wav",
      "start_ms": 0,
      "end_ms": 2000,
      "target_dbfs": -18.0,
      "silence_after_ms": 100,
      "target_sample_rate": 44100
    }
  ]
}
```

- `file`：WAV 路径，**相对清单文件所在目录**解析（也支持绝对路径）。
- `start_ms` / `end_ms`：裁剪区间毫秒。越界自动截断到素材有效范围；
  非数值/负数/起始大于结束/裁剪后为空 → 状态 `跳过`。
- `target_dbfs`：片段目标响度（dBFS，必须为负值）。处理后实测 RMS 误差 0.00 dB。
- `silence_after_ms`：该成功片段之后、下一成功片段之前插入的静音毫秒；
  最后一条之后不插静音。跳过/错误片段不占时间、不留空洞。
- `target_sample_rate`：可在顶层统一指定，也可在条目内单独覆盖。

## 4. 处理规则与状态语义

| 状态 | 触发条件 | 是否进入输出 WAV |
|---|---|---|
| `成功` | 通过全部校验并完成裁剪/重采样/归一化 | 是 |
| `跳过` | 裁剪区间非法或裁剪/重采样/归一化后为空（含全静音片段） | 否，不占时间 |
| `错误` | 文件缺失、非 PCM（如 IEEE float）、非 16 位、非单声道、WAV 损坏、清单字段缺失/类型错误 | 否，不占时间 |

关键实现：

- **格式校验**：`wave.open` 打开失败（`unknown format: 3` 等）或位深/声道不符即记错误，
  后续片段继续处理。
- **重采样**：自实现线性插值（`resample_linear`），22050 Hz × 2.0 s → 44100 Hz 输出 88200 帧，
  输出 WAV 头 `nSamplesRate=44100、nChannels=1、wBitsPerSample=16`。
- **响度归一化**：`gain = 目标RMS / 当前RMS` 整体缩放后裁剪到 [-32768, 32767]，
  RMS 按满刻度 32768 换算 dBFS。
- **拼接**：成功片段按清单顺序写入，中间插零帧静音；无成功片段时输出 0 帧合法空 WAV。

## 5. 报告与日志

报告 JSON（`--report`）：

```json
{
  "output_file": ".../dialogue_main.wav",
  "sample_rate": 44100,
  "channels": 1,
  "sample_width_bits": 16,
  "total_duration_seconds": 3.2,
  "total_frames": 141120,
  "summary": {"total": 8, "success": 3, "skipped": 1, "error": 4},
  "segments": [
    {"file": "good_2s_22050.wav", "status": "成功", "reason": null, "duration_seconds": 2.0}
  ]
}
```

日志（`--log`）每行格式：`YYYY-MM-DD HH:MM:SS.mmm INFO|ERROR 消息`，
每个错误片段均有对应 `ERROR` 行，跳过与成功为 `INFO` 行。

## 6. 命令行契约

```text
python process.py --manifest <清单.json> --output <输出.wav> --report <报告.json> --log <日志.log>
```

四个参数均为必需。缺参时用法说明打印到**标准输出**并以退出码 **2** 结束
（`verify.py` 验收点 8 实测确认）。

## 7. 实测验证结果

`python -I verify.py` 实测 **29/29 全部通过**（Windows 11 + Python 3.10.20），包括：

1. 缺失文件、IEEE float 非 PCM、8 位位深、立体声 → 均为 `错误` 且带中文原因，后续片段正常成功；
2. 2 s 素材请求 1.5–3 s → 截断输出 0.5 s（22050 帧）；起始大于结束 → `跳过`；
3. 22050 Hz 输入输出头为 44100 Hz/单声道/16 位，帧数按目标采样率换算（±1 帧）；
4. 三个成功片段实测 RMS 分别 -18.00 / -20.00 / -16.00 dBFS，与目标误差 0.00 dB（要求 ±0.5 dB）；
5. 间隔清单输出 52920 帧 = 22050 + 8820(200ms 静音) + 22050，总时长 1.2 s，静音区全零；
6. 报告字段齐全、状态枚举正确、总时长/总帧数与 WAV 实测完全一致；
7. 日志每行符合时间戳+级别+消息格式，4 个错误片段各有 ERROR 行；
8. 无参数运行退出码 2，stdout 含 usage 用法。

另测边界：起止相同 → `跳过（裁剪后为空）`；全部失败 → 0 帧合法空 WAV；
带 UTF-8 BOM 的清单可正常解析。

## 8. 目录产物

```text
work/
├─ samples/           # 确定性 WAV 样本 + edit_list.json / gap_list.json
└─ output/            # dialogue_*.wav、report_*.json、run_*.log
```
