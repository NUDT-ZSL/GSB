# 游戏技能声画同步校准工具

本地离线工具：按校准清单逐个技能比对**音效实际起音**与**关键帧标称起音**，输出同步偏差报告、预览帧 PNG 与总报告。单技能失败不影响其他技能。

## 环境要求

- Windows，Python 3.10+（无需音频设备、无需网络）
- 依赖：`pip install Pillow numpy`

## 快速开始

```powershell
# 1. 确定性生成本地样例（samples/ 与 manifest.json）
python generate_samples.py

# 2. 运行校准（输出目录由清单指定，不存在时自动创建，无需预先创建）
python sync_calibrator.py manifest.json
```

## 输入格式

**校准清单 manifest.json**

```json
{
  "standard_sample_rate": 48000,
  "output_dir": "output",
  "skills": [
    {
      "name": "fireball",
      "wav": "samples/fireball.wav",
      "timing": "samples/fireball_timing.json",
      "nominal_onset_ms": 50
    }
  ]
}
```

**关键帧时序 JSON**

```json
{
  "nominal_onset_ms": 50,
  "fps": 60,
  "keyframes": [
    {"frame": 0, "x": 40, "y": 90},
    {"frame": 1, "x": 160, "y": 60}
  ]
}
```

## 输出（均位于清单指定的输出目录下）

| 文件 | 说明 |
|---|---|
| `<技能名>_<帧序号>.png` | 320x180 预览帧，叠加该帧对应音效时间位置（毫秒），帧数 = 最大帧序号 + 1 |
| `sync_deviation_report.json` | 同步偏差报告：标称/实际起音、偏差（整数毫秒，四舍五入）、采样率与重采样提示 |
| `master_report.json` | 总报告：按清单顺序列出各技能状态、问题记录与已生成预览帧路径 |

## 技能状态

- **完整**：起音可测且全部关键帧均生成预览帧
- **部分完成**：部分关键帧坐标越界被跳过，已生成的合法帧保留
- **不可用**：音效无法读取、全静音或采样率不合法（报告中注明原因），不生成预览帧
- **时序缺失**：时序 JSON 缺失或格式错误，仍记录音效起音，不生成预览帧
- **清单条目无效**：清单条目缺少必要字段，记录后继续处理后续技能

## 判定规则要点

- 实际起音 = 音效中第一个非静音采样点位置（按原始采样率计算，不受声明采样率影响）
- 偏差 = 标称起音 − 实际起音，四舍五入为整数毫秒；绝对值小于 1ms 报告为 0
- 实际采样率与清单标准采样率不一致时记录实际采样率并注明"需重采样"
- WAV 文件头声明长度与实际文件大小不符时，以实际可读采样数为准，不崩溃
- 输出目录创建失败（如路径被同名文件占用）时终止处理，控制台报错，不生成任何报告

## 文件说明

- `sync_calibrator.py` — 校准工具主程序
- `generate_samples.py` — 确定性样例生成器（覆盖全部异常场景）
- `CHECK_RESULTS.md` — 实际运行检查结果
