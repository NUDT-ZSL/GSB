# 现场录音素材拼接器（project54）

纯 Python 标准库实现（3.10+，无第三方依赖、无容器）。按剪辑清单 JSON 拼接多段单声道
16 位 44.1kHz PCM WAV：逐段支持裁剪、去首尾静音、增益（钳位）、倒放，拼接后整体归一化
到满量程 90%（峰值 29491），导出成品 WAV 与逐段状态报告 JSON。

## 文件

| 文件 | 说明 |
|---|---|
| `splicer.py` | 核心库 + CLI（读取重试、字段校验、处理流水线、报告生成） |
| `make_samples.py` | 确定性生成小规模 WAV 样本与三份演示清单 |
| `test_splicer.py` | 14 项验收测试（unittest） |

## 快速开始（可复现）

```bash
python make_samples.py workspace          # 1. 生成样本与清单
python splicer.py workspace/manifest_demo.json          # 2. 混合清单：3 成功 5 跳过
python splicer.py workspace/manifest_all_invalid.json   # 3. 全部无效：不生成成品，退出码 1
python splicer.py workspace/manifest_bad_field.json     # 4. 非法字段：报错并指出字段路径，退出码 2
python -m unittest test_splicer -v        # 5. 运行验收测试
```

产物：`workspace/final.wav`（44100Hz/单声道/16 位）、`workspace/report.json`（UTF-8）。

## 清单格式

```json
{
  "output": "workspace/final.wav",
  "report": "workspace/report.json",
  "segments": [
    {"file": "samples/ramp.wav", "start_ms": 0.0, "end_ms": 500.0,
     "reverse": true, "silence_threshold": 100, "gain": 2.0}
  ]
}
```

- `start_ms`/`end_ms` 缺省为整段；越界或倒置的段被跳过并记录中文原因。
- `silence_threshold`：绝对值 ≤ 阈值的样本视为静音，仅去首尾。
- `gain`：乘性增益，结果钳位到 [-32768, 32767]。
- `reverse` 必须为布尔、`silence_threshold`/`gain` 必须为数值，否则整体阻止处理，
  错误信息含字段路径（如 `segments[1].reverse`）。

## 处理规则

- 每段流水线：裁剪 → 去首尾静音 → 增益钳位 → 倒放；按清单顺序拼接。
- 跳过并注明原因：文件缺失、非单声道、非 16 位、采样率非 44100、时间越界/倒置、读取失败。
- 读取遇暂时性 I/O 故障自动重试（3 次）；持续故障跳过该段，不影响其他段。
- 拼接后整体归一化使最大绝对样本 = 29491（满量程 90%）；全零成品保持全零。
- 全部片段无效时不生成成品 WAV，报告 `success=false` 并列出全部原因。
- 报告逐段含 `index/status/reason/original_duration_ms/processed_duration_ms`，
  汇总含总数、成功数、跳过数、输出路径、成品总时长（毫秒浮点），与逐段记录一致。

## 验证结果（本次实际运行）

- `python -m unittest test_splicer`：14 项测试全部 OK（覆盖任务 14 条验收点）。
- 演示清单：8 段中 3 段处理、5 段跳过（缺失/立体声/48k/越界/倒置），成品 658.7ms，
  文件头 44100/1/16，峰值 29491；全无效清单退出码 1 且无成品文件；
  非法字段清单退出码 2，错误信息为 `字段 segments[0].reverse 必须为布尔值`。
