# 游戏本地化台词 WAV 批量处理工具

按 CSV 清单批量裁剪台词 WAV、按目标 RMS 做增益对齐，输出新 WAV 并生成中文 JSON 报告。
**仅依赖 Python 3.10+ 标准库（`wave`/`struct`/`csv`/`json`/`argparse` 等），无需 pip 安装任何第三方库，不使用容器。**

## 目录结构

```
wav_batch/
├── cli.py                 # 命令行入口（--manifest / --output-dir / --report）
├── processor.py           # 清单解析、逐行校验、批量处理与 JSON 报告
├── wav_tools.py           # WAV 读写、格式校验、裁剪、RMS 增益对齐（含削波回退）
├── make_samples.py        # 确定性生成示例 WAV 与 CSV 清单（无随机数）
├── verify_acceptance.py   # 14 条验收项的独立验证脚本（18 个检查点）
├── samples/               # 生成的样本与清单（运行 make_samples.py 后出现）
└── demo_output/           # 演示输出（WAV + 报告，无临时文件）
```

## 一、本地启动方式（可复现）

要求 Python 3.10 或以上，在 PowerShell 中：

```powershell
cd E:\Company\demo\projects\project35\wav_batch

# 1. 生成确定性样本（4 个 WAV + 3 份清单）
python -I make_samples.py

# 2. 执行批量处理
python -I cli.py `
  --manifest samples\manifest.csv `
  --output-dir demo_output\wav `
  --report demo_output\report.json

# 3. 一键跑全部验收（会自动重建样本与输出目录）
python -I verify_acceptance.py
```

`-I` 为隔离模式（忽略环境变量、用户站点包、当前目录注入），保证只加载标准库与本目录代码。

退出码约定：

| 退出码 | 含义 |
|---|---|
| 0 | 清单可用且全部条目成功；或 `--help` |
| 1 | 全局失败：清单缺失/不可读/无有效数据行 |
| 2 | 清单可用但存在条目级失败（其他成功条目不受影响） |

## 二、清单 CSV 格式

UTF-8（可带 BOM），表头固定，时间以秒为单位、支持三位小数：

```csv
id,source,start,end,target_dbfs,output
A_normal_44100,src/line_a_44100.wav,1.234,2.345,-20,A_normal.wav
```

- `source` 相对路径相对**清单所在目录**解析；`output` 相对路径相对 `--output-dir` 解析；也支持绝对路径。
- `target_dbfs` 合法范围 `-40` 至 `0` dBFS。

## 三、业务规则与处理顺序

每一行独立处理，失败只记录该行、不影响其他行。校验顺序：

1. 字段完整性与数值可解析性；
2. 时间合法性（非负、结束 > 起始）、目标响度范围；
3. 输出路径与源路径相同 → 拒绝（防止覆盖源文件）；
4. 源文件存在性（错误含「文件不存在」，与格式错误区分）；
5. 格式校验：**仅接受 16 位 PCM、双声道、采样率 8000~48000Hz**，不符错误含「格式不支持」并列出具体原因；
6. 时间越界：结束时间不得超过源文件时长（秒级 + 帧级双重校验）；
7. 按帧裁剪（帧号四舍五入），输出时长与区间误差 ≤ 1ms；
8. RMS 增益对齐到目标 dBFS，处理后 RMS 误差 ≤ ±0.5dB；
9. 峰值保护：若目标增益会使任一样本超出 16 位整数域，增益回退到不削波的最大值，并在 `warnings` 记录中文警告，输出峰值 ≤ 32767（即不超过 0dBFS）；
10. 直接写目标 WAV（不产生临时文件），保持源片段采样率/位深/声道数不变。

全静音片段 RMS 为 0、无法计算增益，按条目失败处理并说明原因。

## 四、JSON 报告结构

全局失败时 `global_status` 为 `失败`、`entries` 为空、`error` 给出全局原因；
正常处理时为 `成功`（全部通过）或 `部分成功`（含条目失败）。条目严格按清单行顺序排列：

```json
{
  "global_status": "部分成功",
  "manifest": "...manifest.csv",
  "output_dir": "...demo_output\\wav",
  "error": null,
  "total": 9, "succeeded": 4, "failed": 5,
  "entries": [
    {
      "id": "A_normal_44100",
      "status": "成功",
      "output_path": "...\\A_normal.wav",
      "error": null,
      "warnings": [],
      "measured_rms_dbfs": -20.0,
      "duration_seconds": 1.11102
    },
    {
      "id": "D_missing_file",
      "status": "失败",
      "output_path": null,
      "error": "文件不存在：...not_exist.wav",
      "warnings": []
    }
  ]
}
```

失败条目固定为 `output_path=null`、`error` 非空；成功条目 `error=null`、`output_path` 非空；
`warnings` 恒为列表。成功条目额外附带实测 `measured_rms_dbfs` 与 `duration_seconds` 便于核对。

## 五、业务操作样例（演示清单覆盖的场景）

| 条目 | 场景 | 预期结果 |
|---|---|---|
| A_normal_44100 | 44100Hz 双声道16位，裁 1.234–2.345s，目标 -20dBFS | 成功，实测 RMS -20.000 dBFS |
| B_clip_guard | 近满幅源（幅度0.97），目标 0dBFS | 成功但带削波回退警告，峰值=32767 不超满幅 |
| C_keep_8000 | 8000Hz 源 | 成功，输出仍为 8000Hz 双声道16位 |
| D_missing_file | 源文件不存在 | 失败，错误含「文件不存在」 |
| E_bad_format | 单声道 8 位 WAV | 失败，错误含「格式不支持」并列出位深/声道原因 |
| F_time_overflow | 结束 10s > 源时长 5s | 失败，错误含「时间越界」 |
| G_bad_interval | 起始=结束=2s | 失败，结束须大于起始 |
| H_target_range | 目标 -45dBFS | 失败，超出 -40~0 范围 |
| J_duration_check | 裁 1.000–2.500s | 成功，实测时长 1.500000s（误差 0ms） |

另有 `manifest_empty.csv`（仅表头 → 全局失败「清单为空」）、
`manifest_same_path.csv`（配合 `--output-dir samples` 使用 → 「输出与输入相同」拒绝）。

## 六、实际验证结果

`python -I verify_acceptance.py` 最近一次运行结果：**18/18 通过，退出码 0**，关键实测：

- 时长：1.500000s（66150 帧 @44100Hz），落在 1.499–1.501s 内；
- RMS：目标 -20dBFS 的三条输出实测均为 -20.000 dBFS；
- 削波保护：B 条目增益由 1.4580 回退至 1.0309，输出峰值 32767（满幅 32767，不超 0dBFS），报告含中文警告；
- 格式保持：22050Hz 双声道 16 位源 → 输出 22050Hz / 2 声道 / 16 位；
- 输出目录仅含 4 个预期 WAV 与报告 JSON，无任何临时文件；
- `--help` 退出码 0，包含三个参数的中文说明。
