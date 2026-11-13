# 播客片段拼接与响度统一工具

把多段 16 位 PCM 单声道 WAV 按描述文件顺序拼接，按基准响度统一各片段 RMS，
并对缺失文件、无效区间、采样率不匹配等失败片段做**隔离处理**——成功片段照常产出，
全部失败时仍生成可被 `wave` 打开的空 WAV。仅依赖 Python 标准库。

## 环境要求

- Python 3.10+（已在 Python 3.10.20 / Windows 11 验证）
- 标准库 `wave`、`audioop` 可导入（Python 3.13 移除 `audioop` 时自动回退内置纯 Python 实现）
- 不使用 Docker 或其他容器；测试样本由脚本确定性生成，不访问网络

## 目录结构

```
project34/
├── src/
│   ├── podcast_joiner.py   # 主程序：拼接、响度统一、报告生成
│   ├── make_test_data.py   # 确定性测试 WAV 与描述文件生成器
│   └── run_acceptance.py   # 端到端验收测试（覆盖 11 项验收，44 条断言）
├── runtime/                # 运行产物（已 gitignore）
│   ├── test_workspace/     # 测试 WAV、描述文件、输出 WAV 与报告
│   └── acceptance_result.md# 最近一次验收 UTF-8 留档
└── README.md
```

## 快速开始（可复现）

PowerShell 下在项目根目录执行：

```powershell
# 1) 生成确定性测试数据（正弦波 WAV + 11 个场景的描述文件 JSON）
python -I src/make_test_data.py runtime/test_workspace

# 2) 运行主程序（指定输出目录）
python -I src/podcast_joiner.py runtime/test_workspace/desc_valid.json `
    --output-dir runtime/test_workspace/out

# 3) 一键跑全部验收（自动重建工作区，44 条断言）
python -I src/run_acceptance.py
```

Bash 下将反引号续行改为 `\` 即可。建议带 `-I`（隔离模式）运行，避免当前目录
被植入的同名模块干扰 `import`。

## 命令行用法

```
python -I src/podcast_joiner.py <描述文件.json> [--output-dir 目录]
```

- 省略 `--output-dir` 时，WAV 与报告输出到**当前工作目录**。
- 输出文件名取自描述文件 `output_file`；目标已存在则覆盖，报告 `overwritten` 记录是否发生覆盖。
- 报告文件名由输出名派生：`<输出名去扩展名>_report.json`，与 WAV 同目录。
- 退出码：`0` 成功（含片段级失败被隔离）；`2` 描述文件级错误（如基准响度非法），此时不生成任何文件。

## 描述文件格式

片段 `path` 相对描述文件所在目录解析（也支持绝对路径）。

```json
{
  "output_file": "episode01_output.wav",
  "sample_rate": 48000,
  "target_loudness_db": -20.0,
  "segments": [
    {"path": "wav/seg_a.wav", "start_sec": 0.0, "end_sec": 2.0},
    {"path": "wav/seg_b.wav", "start_sec": 0.5, "end_sec": 3.5}
  ]
}
```

字段约束：

| 字段 | 规则 |
| --- | --- |
| `sample_rate` | 正整数；片段实际采样率必须与之相等，否则标记「采样率不匹配」并跳过 |
| `target_loudness_db` | 必须在 **[-60, 0] dB** 内，否则报错、非零退出、不产出文件 |
| `start_sec`/`end_sec` | 不可为负、`end` 必须晚于 `start`、不得超出文件时长，否则标记「无效区间」并跳过 |
| 片段格式 | 仅支持 16-bit PCM 单声道；文件不存在/不可读标记「缺失」并跳过 |

## 响度统一与防削波

1. 对有效区间计算线性 RMS，换算 dBFS：`20*log10(rms/32768)`。
2. 目标增益 `gain_db = 基准 - 原始RMS`，线性系数 `factor = 10^(gain_db/20)`。
3. 防溢出限幅：若 `峰值 * factor > 32767`，则把系数压到 `32767/峰值`，保证调整后样本不削波。
4. 静音片段（RMS=0）增益记 0 dB。
5. 单片段有效时长 > 5 分钟时报告 `warning`，但状态仍为「成功」并完整参与拼接。
6. 拼接总时长超过 10 分钟时报错终止（任务约束）。

## 处理报告格式

`segments[].status` 仅取四个允许值：`成功` / `缺失` / `无效区间` / `采样率不匹配`。

```json
{
  "output_wav": "desc_quiet_output.wav",
  "sample_rate": 48000,
  "target_loudness_db": -20.0,
  "overwritten": false,
  "segments": [
    {
      "index": 1,
      "path": "wav/seg_minus10.wav",
      "start_sec": 0.0,
      "end_sec": 3.0,
      "status": "成功",
      "reason": null,
      "original_rms_db": -29.042,
      "applied_gain_db": 9.042,
      "effective_duration_sec": 3.0,
      "warning": null
    }
  ],
  "summary": {
    "total_segments": 1, "succeeded": 1, "failed": 0,
    "missing": 0, "invalid_range": 0, "sample_rate_mismatch": 1,
    "warnings": 0,
    "total_output_frames": 144000,
    "total_output_duration_sec": 3.0,
    "overwritten": false
  }
}
```

失败片段的 `original_rms_db` / `applied_gain_db` / `effective_duration_sec` 为 `null`，
`reason` 给出具体原因；全部失败时 WAV 为 0 数据帧空音频（44 字节头），仍可被 `wave.open` 打开。

## 验收标准与实测结果对照

最近一次运行：**44/44 断言通过**（留档 `runtime/acceptance_result.md`）。

| # | 验收点 | 实测结果 |
| --- | --- | --- |
| 1 | 全有效时总时长≈区间之和（<0.1s） | 10.500s = 2+3+1.5+4，误差 0 |
| 2 | 低 10dB 片段统一后偏差 <1dB 且无削波 | 处理后 RMS=-19.994dBFS（基准 -20），peak=4640 |
| 3 | 缺失片段隔离，其余继续 | 状态 `[缺失, 成功]`，输出仅 2.0s |
| 4 | 负起止/结束≤开始/超时长标无效区间 | 4 条均「无效区间」，有效片段照常产出 |
| 5 | 全部失败仍产出可打开空 WAV | 0 帧、48kHz/16bit/单声道，报告逐片段说明 |
| 6 | JSON 报告字段完整、状态合法、摘要含成功数 | 10 个字段齐全，状态均在允许集合内 |
| 7 | 基准 -80/+10 非零退出且无 WAV | 退出码 2，stderr 含「基准响度无效」，目录无新增文件 |
| 8 | 44100Hz vs 指定 48000Hz 标采样率不匹配 | 状态 `[采样率不匹配, 成功]`，输出不含该片段 |
| 9 | 6 分钟片段警告但成功且完整 | 警告含「超过 300.0s」，输出 360.000s |
| 10 | 覆盖已存在输出并记录标志 | 覆盖时 `overwritten=true`；新目录首跑为 `false` |
| 11 | `--output-dir` 与默认当前目录 | 两文件分别落在指定目录 / 当前工作目录 |

## 业务操作样例（自定义素材）

1. 把自己的 WAV 放到某目录，例如 `D:/show/wav/`。
2. 在 `D:/show/episode.json` 写描述文件（格式见上），`path` 用相对该 JSON 的路径。
3. 执行：
   ```powershell
   python -I src/podcast_joiner.py D:/show/episode.json --output-dir D:/show/dist
   ```
4. 在 `D:/show/dist/` 取得拼接 WAV 与 `*_report.json`，先看报告确认各片段状态再发布。
