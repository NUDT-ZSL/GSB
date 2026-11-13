# 播客采访剪切与响度对齐工具

读取 JSON 剪切剧本，对单声道 16 位 PCM WAV 采访录音执行裁剪、RMS 响度对齐、
按 20 ms 静音间隔拼接，导出 WAV 并在同目录生成 JSON 处理报告。
仅依赖 Python 标准库（需 Python 3.10+），全部样本本地确定性构造。

## 文件

| 文件 | 说明 |
|---|---|
| `podcast_cutter.py` | 主工具（CLI） |
| `make_samples.py` | 确定性生成演示 WAV 与 7 个 JSON 剧本到 `samples/` |
| `verify.py` | 自动验证全部 11 条验收要求（15 项断言） |

## 快速开始

```powershell
python -I make_samples.py                          # 1. 生成确定性样本
python -I podcast_cutter.py samples\script_ok.json # 2. 处理一个剧本
python -I verify.py                                # 3. 跑全部验收断言
```

输出写入 `out/<场景>/episode.wav` 与 `out/<场景>/episode_report.json`。

## 剧本格式

```json
{
  "input": "interview.wav",
  "output": "../out/ok/episode.wav",
  "clips": [
    {"clip": "opening", "start": 0.0, "end": 1.0, "target_loudness": -20.0}
  ]
}
```

- 每片段必填 `clip`、`start`、`end`、`target_loudness`；缺失任一字段则整个剧本被拒绝，
  不生成输出音频，报告指明缺失字段名。
- `start`/`end` 单位为秒（毫秒精度），必须为非负数字；字符串或负值导致剧本被拒绝并列出非法值。
- 可选 `source` 字段让单个片段使用独立音频源（用于多录音/损坏文件场景）。
- 可选顶层 `report` 字段指定报告路径，默认为 `<output同名>_report.json`。

## 处理规则与验收对应

| # | 规则 | 实现 |
|---|---|---|
| 1 | 缺必填字段 → 不生成音频，报告指明字段名 | `validate_script` 整体拒绝，状态 `rejected` |
| 2 | start<end 且区间在时长内；无效片段标记原因，其余继续 | 逐片段校验，`skipped` + `reason` |
| 3 | 按剧本顺序拼接，片间 20 ms 静音，单声道 16 位，采样率同输入 | `SILENCE_MS=20`，`wave` 写出 |
| 4 | RMS(dBFS) 对齐目标，峰值 ≤ 0 dBFS，报告逐段增益 | `rms_dbfs`/`apply_gain`，报告含 `gain_db` |
| 5 | 目标不可达 → 不施加过载增益，报告原因 | 增益封顶到峰值 0 dBFS，状态 `capped` |
| 6 | 音频读取失败 → 记录异常继续，已成功片段仍导出 | 按源缓存加载，异常片段 `skipped` |
| 7 | 同目录 JSON 报告：状态/时长/增益 + 全局 errors | `<output同名>_report.json` |
| 8 | 区分 success / partial / failed，列出未处理索引 | `status` + `unprocessed_indices` |
| 9 | 输出已存在则覆盖并记录 | `overwritten: true` + `notes` |
| 10 | 所需增益超出 ±20 dB → 响度异常，不参与拼接 | `GAIN_LIMIT_DB=20` |
| 11 | 时间非数字或为负 → 拒绝并列出非法值 | `validate_script` |

退出码：`0` 成功/部分成功，`1` 全部片段失败，`2` 剧本被拒绝。

## 演示剧本一览（`make_samples.py` 生成）

| 剧本 | 演示内容 |
|---|---|
| `script_ok.json` | 全部有效 → `success` |
| `script_partial.json` | 越界、响度异常、目标不可达、损坏源 → `partial` |
| `script_missing_field.json` | 缺 `target_loudness` → `rejected`，无音频输出 |
| `script_bad_time.json` | 字符串/负时间 → `rejected`，列出非法值 |
| `script_all_invalid.json` | 全部无效 → `failed` |
| `script_corrupt.json` | 损坏 WAV + 正常片段混合 → 损坏跳过，其余导出 |

## 验证结果

`python -I verify.py` 实际运行输出：15 项断言全部 PASS，0 失败，覆盖上表 11 条要求
（含输出时长=片段和+(N-1)×20ms、各段 RMS 与目标差 ≤0.5 dB、峰值 ≤0 dBFS、
覆盖记录、损坏文件跳过等逐项核对）。
