# 无缝循环接缝评估与导出工具

读取循环音效清单 JSON，在每条音效的候选接缝点比较首尾波形，
选出差异最小的位置作为无缝循环点，导出同采样率/位深/声道的 WAV，
并生成分类评估报告。面向游戏音频播放循环音效时的接缝“咔哒声”问题。

## 环境

- Python 3.10 及以上
- NumPy
- 无需外部素材，样例由脚本确定性生成

```powershell
python -m pip install -r requirements.txt
```

## 快速开始

```powershell
# 1. 生成确定性样例（samples/audio/*.wav 与 samples/manifest.json）
python make_samples.py

# 2. 运行导出
python loop_seams.py samples/manifest.json
```

运行后：

- `samples/out/` 下生成无缝 WAV（文件名形如 `<源文件名>__loop_<序号>.wav`）
- `samples/out/loop_report.json` 为评估报告
- 终端输出人类可读摘要

可选参数 `--report PATH` 指定报告位置：

```powershell
python loop_seams.py samples/manifest.json --report build/report.json
```

退出码：`0` 表示清单整体合法并已完成处理（个别条目可能未完成/无效）；
`2` 表示清单整体结构非法（无任何部分结果产出）。

## 清单格式

```json
{
  "output_dir": "out",
  "audio_dir": "audio",
  "entries": [
    {
      "filename": "loop_perfect.wav",
      "expected_loop_ms": 800,
      "candidate_seams_ms": [500, 750, 880]
    }
  ]
}
```

- `output_dir`：必填字符串，相对路径按清单所在目录解析；不存在时自动创建。
- `audio_dir`：可选，音源目录；缺省时按清单所在目录解析 `filename`。
- `entries`：必填数组。
- 每个条目必填：`filename`（字符串）、`expected_loop_ms`（正整数毫秒）、
  `candidate_seams_ms`（非负整数毫秒数组）。
- 为便于接入，解析器也接受常见别名（如 `expected_loop_length_ms`、
  `seam_candidates_ms`、`sounds` 等），但同一字段只取第一个别名。
- 约定音源为 44.1 kHz、16 位、单声道 PCM WAV。

清单整体非法（缺少/空输出目录、顶层非对象、`entries` 非数组、JSON 语法错误等）
时不会创建输出目录，也不会写出报告。

## 处理规则与状态

| 状态 | 含义 | 典型原因 |
| --- | --- | --- |
| `exported`（完整导出） | 已导出无缝 WAV | — |
| `incomplete`（未完成） | 条目有效但本次无法导出 | 文件缺失、非 WAV/采样率/声道/位深不符、音频短于预期循环长度、整段静音、导出失败 |
| `invalid`（无效） | 条目不满足清单约定，不参与导出 | 字段缺失或类型错误、循环长度非正整数、接缝点为负/非整数、接缝点超出音频时长 |

- 接缝评分：`2 * |首样本 - 尾样本| + RMS(首窗 - 尾窗)`，越小越平滑；
  比较窗口为 64 个样本。`0 ms` 接缝会产生空循环，不参与选择。
- 导出帧数严格等于所选接缝点换算的样本数（`round(ms * 44100 / 1000)`）。
- 未完成/无效条目的 `selected_seam_ms`、`exported_frames` 等缺失值一律为 `null`，
  不以 `0` 冒充有效结果。
- 单条失败不会中断整批；文件名带清单序号，重复运行或同一源文件被多条引用时不会互相覆盖。

## 报告结构

```json
{
  "tool": "loop_seams.py",
  "manifest": "...",
  "output_dir": "...",
  "summary": {
    "total": 15,
    "exported": 5,
    "incomplete": 5,
    "invalid": 5,
    "labels": {"exported": "完整导出", "incomplete": "未完成", "invalid": "无效"}
  },
  "entries": [
    {
      "index": 0,
      "filename": "loop_perfect.wav",
      "expected_loop_ms": 800,
      "candidate_seams_ms": [500, 750, 880],
      "selected_seam_ms": 750,
      "exported_frames": 33075,
      "exported_file": "loop_perfect__loop_001.wav",
      "status": "exported",
      "status_zh": "完整导出",
      "reason": null,
      "message": "已导出无缝循环",
      "seam_difference": 0.0,
      "candidate_evaluations": [
        {"ms": 500, "frame": 22050, "difference": 40000.5},
        {"ms": 750, "frame": 33075, "difference": 0.0},
        {"ms": 880, "frame": 38808, "difference": 40321.0}
      ],
      "audio_duration_ms": 1000.0,
      "source_file": ".../samples/audio/loop_perfect.wav",
      "validation_errors": []
    }
  ]
}
```

`entries` 顺序与清单一致，便于逐条核对。

## 样例覆盖场景

`make_samples.py` 生成的 15 条清单同时包含：

- 完整导出：最佳接缝、同源不同长度、同候选不同源
- 未完成：文件缺失、非 WAV、立体声、时长不足、整段静音
- 无效：缺少预期长度、长度类型错误、负接缝点、非整数接缝点、接缝点越界

## 测试

```powershell
python -m pytest tests -q
```

39 个测试覆盖清单校验、接缝选择、导出格式与帧数、三类状态、
失败隔离、重复运行、退出码与摘要一致性。

## 文件

- `loop_seams.py` — 主程序
- `make_samples.py` — 确定性样例生成器
- `samples/` — 生成的清单与音频
- `tests/test_loop_seams.py` — 端到端与单元测试
- `VERIFICATION.md` — 实际检查记录
