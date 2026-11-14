# 过场动画多轨混音工具

按时间表 JSON 将对白、音效、环境等 WAV 分轨合成立体声母带，输出混音 WAV 与合成报告 JSON。
单条分轨损坏（缺失、格式不符、入点非法）只影响该分轨，其余分轨照常混音。
纯 Python 标准库实现，无第三方依赖，不使用容器。

## 环境要求

- Windows 10/11（已在 Windows 11 验证）
- Python 3.10 及以上（已在 Python 3.14.7 验证）

## 目录结构

```
src/timeline_mixer.py   混音器主程序（CLI）
tools/make_samples.py   确定性样例生成器（无随机数，重复运行逐字节一致）
samples/                业务样例：6 条 WAV 分轨 + timeline.json
tests/verify.py         十条核对项的自动化验证
out/                    混音产物（mix.wav + report.json）
```

## 本地启动步骤

```powershell
# 1. 生成业务样例（确定性，可重复重建）
python tools\make_samples.py

# 2. 按时间表合成混音母带与报告
python src\timeline_mixer.py --timeline samples\timeline.json --out out\mix.wav --report out\report.json

# 3. 运行十条核对项验证
python tests\verify.py
```

可选目标格式参数：`--sample-rate <8000-192000>`（默认 44100）、`--channels <1|2>`（默认 2）。
输出固定位为 16-bit PCM。参数非法时进程以非零退出码终止并打印可读错误。

## 时间表 JSON 格式

```json
{
  "total_duration_ms": 2000,
  "tracks": [
    {"id": "dialogue", "file": "dialogue.wav", "in_point_ms": 0}
  ]
}
```

- `total_duration_ms`：声明总时长（非负整数毫秒），非法则整体终止。
- `in_point_ms`：分轨入点（非负整数毫秒），非法条目单独记录于报告 `invalid_entries`，不影响其他条目。
- `file` 相对于时间表所在目录解析。

## 业务样例说明

`samples/timeline.json` 模拟一段 2000ms 的开场过场动画，9 条分轨条目覆盖全部边界情形：

| 条目 | 情形 | 结果 |
|---|---|---|
| dialogue / sfx / ambient | 正常立体声 44.1kHz 分轨 | 参与混音 |
| room_tone | 全零采样静音分轨 | 参与混音，列入 `silent_tracks` |
| narration_mono | 单声道 | 跳过，报告注明声道数不符 |
| music_48k | 48kHz 采样率 | 跳过，报告注明采样率不符 |
| missing_voice | 文件不存在 | 跳过，报告注明文件缺失 |
| bad_inpoint | 入点 -50ms | 列入 `invalid_entries` |
| late_track | 入点 5000ms 超过总时长 | 不混入，报告注明越界 |
| ambient | 入点 1000ms + 时长 1100ms | 尾部超出声明总时长 100ms，报告给出声明 2000 / 实际 2100 / 差值 100 |

## 合成报告字段

- `declared_total_ms` / `actual_total_ms` / `duration_diff_ms`：声明总时长、实际合成时长（最晚分轨末尾）、两者差值
- `mixed_track_count` / `mixed_tracks`：参与混音的分轨数与清单
- `unprocessed_tracks`：未处理分轨及原因（缺失 / 采样率不符 / 声道数不符 / 位深不符 / 入点越界）
- `invalid_entries`：入点等字段非法的原始条目
- `silent_tracks`：全静音分轨清单（供业务核查）

## 混音规则

多条分轨时间重叠时在 32 位累加器中按振幅相加，写出时削减（clip）到 int16 范围
[-32768, 32767]，不回绕，两条满幅分轨完全重叠也不会产生极性翻转失真。

## 验证记录

`python tests\verify.py` 最近一次运行结果（2026-11-14，Python 3.14.7，Windows 11）：

```
[PASS] 主流程退出码为 0
[PASS] 1. 负入点条目列入 invalid_entries 且其余分轨正常混音
[PASS] 2. 入点 5000ms 超过总时长 2000ms 的分轨未混入且有记录
[PASS] 3. 单声道与 48kHz 分轨被跳过并说明原因，输出保持立体声 44.1kHz
[PASS] 4. 缺失分轨列入报告，混音仍生成且含其余分轨
[PASS] 5. 满幅重叠按振幅相加后削减：峰值=32767 且无回绕负值
[PASS] 6. 报告字段齐全，mixed_track_count 与有效条目数一致
[PASS] 7. 损坏 JSON 与非法声道数均非零退出且输出可读错误
[PASS] 8. 声明/实际时长与差值分别给出 (2000 / 2100 / 100)
[PASS] 9. 全静音分轨参与混音且列入 silent_tracks
[PASS] 10. 运行前后源 WAV 与时间表哈希全部一致

共 11 项核对，通过 11，失败 0
```

验证在临时目录中进行，全程不修改 `samples/` 内任何源文件（核对项 10 以 SHA-256 比对确认）。
