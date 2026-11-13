# 游戏音频片段拼接与响度统一工具

按输入顺序拼接多个 16 位 PCM WAV 片段，逐段裁剪、按 RMS 统一响度、限制峰值，
并输出拼接后的 WAV 与 JSON 报告。只用 Python 标准库，无 Docker、无网络访问。

## 环境

- Python 3.10+（已在 3.10.20 验证）
- 无第三方依赖

```
python --version
```

## 快速开始（可复现）

```powershell
# 1) 生成确定性测试样本（13 个 WAV，写入 demo/samples）
python make_samples.py

# 2) 跑 11 项验收场景 + 1 项峰值限幅附加场景
python run_demo.py

# 3) 跑单元测试（23 个用例）
python -m unittest discover -s tests -v
```

`run_demo.py` 会用 `wave` 模块重新读取产物做独立校验，所有场景输出在
`demo/out/<场景号>/` 下，汇总写入 `demo/out/summary.json`。

## 命令行用法

```powershell
python wav_stitcher.py `
  --inputs demo/samples/tone_a.wav demo/samples/tone_b.wav `
  --crops full 0.2:0.6 `
  --out-wav demo/out/mix.wav `
  --report demo/out/mix.json `
  --target-loudness -20
```

或从列表文件读取（每行一个路径，`#` 开头为注释，相对路径相对当前目录）：

```powershell
python wav_stitcher.py --input-list clips.txt --out-wav out/mix.wav --report out/mix.json
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `--inputs WAV [WAV ...]` | 二选一 | 输入片段，顺序即拼接顺序 |
| `--input-list FILE` | 二选一 | 每行一个输入路径的文本文件 |
| `--crops START:END [...]` | 否 | 与输入按位对齐的裁剪窗口（秒，支持小数）；`full` 表示不裁剪；缺省尾部为整段 |
| `--out-wav PATH` | 是 | 输出 WAV；所有片段都不可用时不会创建该文件 |
| `--report PATH` | 是 | 输出 JSON 报告；无论成功与否都会生成 |
| `--target-loudness DBFS` | 否 | 目标 RMS 响度，默认 -20；超出 [-60, 0] 时回落 -20 并向 stderr 警告 |

退出码：`0` 成功写出 WAV；`1` 无可用片段（仅报告）；`2` 参数/路径错误；`3` 处理完成但写 WAV 失败。

## 处理规则

1. **校验**：必须为未压缩 16 位 PCM，且采样率、声道数与会话基准一致
   （基准取列表中第一个可解析的片段）。不合格项记入报告并跳过，其余继续。
2. **裁剪**：`START:END` 秒，越界截断到 `[0, 片段时长]`；实际区间按帧量化后在报告中记录。
3. **响度**：按裁剪后 RMS 求 `目标 - 输入RMS` 增益，限制在 ±20 dB；
   若应用到峰值超过 0 dBFS，则整体缩放使峰值不超过 0 dBFS（不削波）。
4. **拼接**：仅拼接成功片段，保持输入顺序与基准采样率/声道数。
5. **报告**：每个输入都有独立记录，数值保留两位小数，并附 `unit` 与 `formatted` 字段。
6. **失败隔离**：单片段解析失败（缺失、损坏、截断）只影响自身；成功片段数据不变。

## 报告结构（节选）

```json
{
  "target_loudness": {
    "requested": {"value": -20.0, "formatted": "-20.00 dBFS", "unit": "dBFS"},
    "applied": {"value": -20.0, "formatted": "-20.00 dBFS", "unit": "dBFS"},
    "fallback_to_default": false
  },
  "input_count": {"total": 2, "stitched": 2, "skipped": 0, "failed": 0},
  "stitched_files": ["tone_a.wav", "tone_b.wav"],
  "excluded_files": [],
  "output": {
    "wav_path": "demo/out/mix.wav", "written": true,
    "sample_rate_hz": 44100, "channels": 1, "bit_depth": 16,
    "frames": 88200,
    "duration": {"value": 2.0, "formatted": "2.00 s", "unit": "s"}
  },
  "segments": [
    {
      "index": 0, "file_name": "tone_a.wav", "status": "ok", "participated": true,
      "reason": null,
      "crop": {
        "requested_start": {"value": 0.0, "formatted": "0.00 s", "unit": "s"},
        "actual_start": {"value": 0.0, "formatted": "0.00 s", "unit": "s"},
        "actual_end": {"value": 1.0, "formatted": "1.00 s", "unit": "s"},
        "duration": {"value": 1.0, "formatted": "1.00 s", "unit": "s"},
        "actual_formatted": "0.00 - 1.00 s",
        "truncated_to_clip_bounds": false
      },
      "input_rms": {"value": -10.0, "formatted": "-10.00 dBFS", "unit": "dBFS"},
      "gain": {"value": -10.0, "formatted": "-10.00 dB", "unit": "dB",
               "range_limited": false, "peak_limited": false},
      "final_rms": {"value": -20.0, "formatted": "-20.00 dBFS", "unit": "dBFS"},
      "final_peak": {"value": -16.99, "formatted": "-16.99 dBFS", "unit": "dBFS"}
    }
  ]
}
```

`status` 取值：`ok`（已拼接）/ `skipped`（格式没问题但因时长为 0 未参与）/
`failed`（校验或解析失败）。

## 文件

- `wav_stitcher.py` — 核心处理与 CLI
- `wav_samples.py` — 确定性样本生成（正弦、尖峰、损坏、截断等）
- `make_samples.py` — 样本生成入口
- `run_demo.py` — 11 项验收场景的实际运行与独立校验
- `tests/test_stitcher.py` — 23 个单元/集成测试

## 实际验证结果（2026-11-14，本地 Windows 11 / Python 3.10.20）

```
$ python -m unittest discover -s tests
Ran 23 tests ... OK

$ python run_demo.py
Scenarios passed: 12/12
```

关键实测数据（均为 `wave` 模块独立读取产物所得，非仅读报告）：

| 场景 | 实测 |
| --- | --- |
| 采样率不一致 | `sr_22050.wav` 失败，原因 "sample rate 22050 Hz does not match session sample rate 44100 Hz"；tone_a/tone_b 拼接得 88200 帧、2.00 s |
| 裁剪 0.2–0.6 s | 输出 17640 帧 = 0.40 s；报告 actual 0.20–0.60 s |
| 响度 | 输入 -10.00 dBFS → 增益 -10.00 dB → 输出 RMS -20.00 dBFS，峰值 -16.99 dBFS |
| 拼接 | 两段 1 s → 88200 帧，报告与 `wave` 一致 |
| 全部无效 | 退出码 1，输出目录无 WAV，5 条失败原因齐全 |
| 起止反向 | `skipped`，原因 "insufficient duration"，同批其他片段照常输出 44100 帧 |
| 目标 -100 | stderr 输出 WARNING，按 -20.00 dBFS 处理，输出 RMS -20.00 dBFS |
| 截断音频 | 报告 "truncated audio data: header declares 44100 frames (88200 bytes), but only 80200 bytes were readable"；有效片段拼接字节与干净运行完全相同 |
| 峰值限幅 | 请求 0 dBFS 时标记 `peak_limited`，实测峰值 -0.00 dBFS |
