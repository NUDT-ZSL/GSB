# GSB 离线声学时延与相位对齐测量引擎

面向游戏配音素材库 / 虚拟录音棚链路的纯离线测量工具。工程自带固定种子的多轨 WAV 与任务 JSON 生成器，
不访问网络或外部服务；测量结果写入 JSON 报告和 SQLite 定点账本。

## 能力概览

- 解析任务 JSON、WAV 文件头和 16/24-bit PCM，校验采样率、位深、声道、参考轨和定点/容差参数。
- 将 `[sweep_start_ms, sweep_end_ms)` 精确换算为源 WAV 整数采样点，形成：
  - 静音段 `[0, sweep_start_sample)`
  - 激励段 `[sweep_start_sample, sweep_end_sample)`
  - 尾部段 `[sweep_end_sample, input_frames)`
- 用 FFT 互相关估计整数采样点时延，并用抛物线插值给出 `float64` 参考时延。
- 按任务频段做 FFT 相位差、相位展开和 OLS 线性回归，输出相位斜率时延与散点数目。
- SQLite 账本同时保存定点量子、`float64` 参考值和量化误差，支持按轨 ID / 频段查询。
- 有界线程池、内存预算准入、按轨 ID 排序等待和异常取消。
- 支持可处理终止信号的轨边界 / 频段边界退出，以及强杀后依据原子账本恢复。
- 报告和账本均“同目录临时文件 + `fsync` + `os.replace`”原子提交。
- 输入摘要、账本摘要和条目数异常时返回 / 记录 `E_DIGEST`，并从最近有效账本备份重建报告。

## 目录

```text
acoustic_alignment/
  canonical.py          # 规范化 JSON、SHA-256、定点量化
  cli.py                # run / verify / query / generate 命令
  dsp.py                # 重采样、互相关、抛物线插值、相位回归
  engine.py             # 校验、切分、并发调度、中断边界
  ledger.py             # SQLite 账本、摘要、原子替换和备份恢复
  memory.py             # 内存预算会计
  reporting.py          # 报告摘要核对与 E_DIGEST 恢复
  sample_generator.py   # 固定种子确定性样本生成器
  task_model.py         # JSON 任务模型和参数校验
  wav_io.py             # 16/24-bit WAV 头解析与 PCM 解码
tests/                  # pytest 验收与恢复测试
samples/                # 运行生成器后得到的本地样本
output/                 # 报告、SQLite 账本、中断标记和有效账本备份
```

## Windows / Python 3.11 环境

推荐在 Windows 10 及以上使用 Python 3.11：

```powershell
py -3.11 -m pip install numpy scipy pytest
py -3.11 -c "import numpy, scipy; print(numpy.__version__, scipy.__version__)"
```

SQLite 直接使用 Python 标准库 `sqlite3`，无需额外安装。

本工程使用 `pathlib`、`os.replace` 和标准 WAV / SQLite API；信号处理对 Windows 的
`SIGINT` / `SIGBREAK` 与 POSIX 的 `SIGINT` / `SIGTERM` / `SIGHUP` 均做了兼容注册。

## 快速验收

```bash
# 1. 生成固定种子样本和任务 JSON
python generate_samples.py --out samples --seed 380

# 2. 测量并原子提交 output/alignment_report.json 与 output/alignment_ledger.sqlite
python run_alignment.py run \
  --task samples/measurement_task.json \
  --output output

# 3. 按轨和频段查询账本
python run_alignment.py query \
  --output output \
  --track cand_phase_bad \
  --band high_2600_6400

# 4. 校验摘要；若报告/账本被篡改，会返回 E_DIGEST 并从有效备份重建
python run_alignment.py verify \
  --task samples/measurement_task.json \
  --output output

# 5. 完整自动化测试
python -m pytest -q
```

也可以使用模块入口：

```bash
python -m acoustic_alignment.sample_generator --out samples --seed 380
python -m acoustic_alignment.cli run -t samples/measurement_task.json -o output
```

## 任务 JSON 关键字段

完整示例见 `samples/measurement_task.json`。

| 字段 | 说明 |
| --- | --- |
| `reference_track` | 唯一参考轨 ID；也可用轨内 `"role": "reference"`。 |
| `tracks[].track_id` | 轨标识，业务码和账本查询均使用该 ID。 |
| `tracks[].path` | WAV 路径；相对路径相对于任务 JSON 所在目录解析。 |
| `tracks[].enabled` | 禁用轨返回 `E_DISABLED` 并跳过；参考轨禁用会导致 `E_REF`。 |
| `tracks[].expected_start_ms` | 必须是非负整数；严格拒绝负数、小数和布尔值。 |
| `tracks[].sample_rate/bit_depth/channels` | 声明值，必须与 WAV 文件头一致。 |
| `sweep_start_ms/sweep_end_ms` | 全局扫频边界；轨内可用同名字段覆盖。 |
| `bands[]` | `band_id`、`min_hz`、`max_hz` 和可选 `band_index`。 |
| `search_window_ms` 或 `search_window_samples` | 互相关搜索窗；毫秒按分析采样率换算。 |
| `phase_unwrap_limit` | 相位展开修正幅度上限（rad），必须为有限正数。 |
| `fixed_point_scale` | 正整数标度，示例为 `1000000`。 |
| `delay_tolerance_samples` | 整数时延与抛物线 float64 时延的定点容差。 |
| `phase_tolerance_samples` | 相位斜率时延与整数互相关时延的定点容差。 |
| `noise_gate_factor` / `correlation_threshold` | 本底噪声门限和相关峰有效阈值。 |
| `concurrency` | 有界线程池最大轨并发。 |
| `memory_budget_bytes` | 在途解码 / 互相关 / FFT 工作集的保守预算。 |

允许采样率为 `16000`、`44100`、`48000`；允许位深为 `16`、`24`。

### 混合采样率说明

源段边界始终使用各轨原生采样率换算并原样写入报告，不会因重采样改变。互相关和相位分析前，
候选激励段会确定性重采样到参考轨采样率；因此 `delay_samples` 是参考分析网格上的整数采样数，
报告同时给出 `analysis_sample_rate`、`analysis_samples` 和各轨原生 `sample_rate`。

## 报告与账本

报告路径：`output/alignment_report.json`。每条轨包含：

- `input_frames`、`sample_rate`、`channels`、`bit_depth`
- `silence_start_sample/end_sample`
- `excitation_start_sample/end_sample`
- `tail_end_sample`
- `noise_rms`、`excitation_peak`、`correlation_peak`
- `delay_samples`、`delay_float_samples`、`delay_float_delta_samples`
- `integer_delay_q`、`float_delay_q`、`delay_delta_q`、`delay_tolerance_q`
- 逐频段 `point_count`、`slope_rad_per_hz`、`slope_q`、`phase_delay_samples/q`
- `phase_error_q`、`phase_tolerance_q`、`passed`、`code`
- `business_codes` 和最终 `alignment_pass`

账本路径：`output/alignment_ledger.sqlite`。

- `tracks` 表：每轨一行，含完整 `result_json` 和可查询标量列。
- `bands` 表：每轨每频段一行，建有 `(input_digest, track_id)` 和 `(input_digest, band_id)` 索引。
- `meta` 表：保存 schema、条目数和账本摘要。
- `output/ledger_versions/latest_valid.sqlite`：最近一份摘要有效的账本备份。

判定只比较定点量子：

```text
delay_pass = |float_delay_q - integer_delay_q| <= delay_tolerance_q
band_pass  = |phase_delay_q - integer_delay_q| <= phase_tolerance_q
```

float64 值和 `*_quantization_error` 仅用于诊断。

## 业务码

| 业务码 | 含义 |
| --- | --- |
| `E_FILE` / `E_WAV` | 文件不存在或 WAV 头 / PCM 解码失败。 |
| `E_RATE` | 采样率不在允许集合或与声明不一致。 |
| `E_BITS` | 位深不是 16/24 或与声明不一致。 |
| `E_CHANNELS` | 声道数与声明不一致。 |
| `E_START` | `expected_start_ms` 不是非负整数。 |
| `E_REF` | 参考轨缺失、重复、禁用或不可测量。 |
| `E_DUPLICATE` / `E_DISABLED` | 轨 ID 重复或轨被禁用。 |
| `E_WINDOW` / `E_UNWRAP` / `E_SCALE` / `E_TOLERANCE` | 任务测量参数非法。 |
| `E_SEGMENT` | 扫频边界越界或激励段不足以加窗 / 相关。 |
| `E_PEAK` | 无有效相关峰、相关峰过低或激励峰值低于本底噪声门限。 |
| `E_DELAY` | 整数 / float64 时延差超过定点时延容差。 |
| `E_PHASE` | 频段相位斜率时延与整数时延差超过容差；详情定位频段和散点数。 |
| `E_MEMORY` / `E_MEMORY_BUDGET` | 单轨工作集超预算或内存调度无法推进。 |
| `E_DIGEST` / `E_LEDGER` | 输入摘要、账本摘要或条目数不一致。 |
| `E_INTERNAL` | 未预期异常；调度器会取消其余待处理轨并保留已提交账本。 |

单轨业务错误会写入该轨结果并继续其他轨；未预期异常才进入并发取消路径。

## 中断、退出码与恢复

| 退出码 | 含义 |
| ---: | --- |
| `0` | 测量流程和报告提交完成；报告内仍可能有 `E_*` 轨级结论。 |
| `1` | 未预期运行错误；已成功轨的账本快照保留。 |
| `2` | 命令行或任务 JSON schema 错误。 |
| `3` | 账本 / 摘要损坏且无有效备份可恢复。 |
| `4` | 仅 `--strict` 时，存在未通过轨或业务码。 |
| `130` | 收到可处理终止信号并在轨 / 频段边界协作退出。 |

收到 `SIGINT`、`SIGTERM` 或 Windows `SIGBREAK` 后，处理器只设置停止事件。引擎在下一轨开始前或
下一频段开始前检查；中断时原子写出 `output/interruption.json`，退出码为 `130`。未完成轨不会提交
`status="measured"` 的完整账本记录。

强杀 / 断电时来不及写标记也没关系：每条完整轨在测量完成后都会用临时 SQLite 文件原子替换账本。
重启同一命令时，引擎依据账本中的 `(input_digest, track_id)` 完整记录跳过已完成轨，仅补测剩余轨。
自动化测试用 `--fault-exit-after-commit 2` 后直接 `os._exit(9)` 模拟强杀，并验证恢复后的账本摘要、
规范化记录和报告核心字段与 `--force` 一次跑完完全一致。

## 并发与内存

- `ThreadPoolExecutor(max_workers=task.concurrency)` 保证轨级有界并发。
- 提交前根据 WAV 头帧数 / 声道数计算保守工作集上限。
- 若当前已预留字节 + 下一轨估算值超过 `memory_budget_bytes`，调度器等待已有 future 完成。
- 待提交轨按 `track_id` 字典序排列；预算不足时队首轨先等待，不会通过缩小搜索窗、减少 FFT 频点或
  降低频段精度来“让出内存”。

## 摘要与原子提交

`input_digest = SHA256(canonical_json({task: <任务 JSON>, audio: [{track_id, sha256(WAV 文件)}]}))`。

`ledger_digest = SHA256(canonical_json({tracks: [...], bands: [...]}))`。

提交报告前，`reconcile_existing_report()` 读取旧报告的 `input_digest`、`ledger_digest`、
`track_count`、`declared_track_count` 和实际账本行数。若不一致：

1. 标记 `E_DIGEST`；
2. 若账本摘要损坏，从 `output/ledger_versions/latest_valid.sqlite` 恢复；
3. 以摘要有效的账本记录重建报告；
4. 通过同目录临时文件和 `os.replace` 原子提交。

## 实际验证

详细命令和结果见 `docs/VERIFICATION.md`。当前容器实际使用 Python 3.10.12、NumPy 1.26.4、
SciPy 1.13.1、pytest 8.2.0；代码语法兼容 Python 3.10+，生产目标为题目要求的 Windows + Python 3.11。

最近一次完整测试结果：

```text
13 passed in 8.70s
```

确定性样本集的验收摘要：

```text
12 declared tracks; 6 measurable tracks; 4 fully aligned tracks;
E_DELAY / E_PHASE / E_PEAK / E_RATE / E_BITS / E_CHANNELS / E_FILE / E_START all exercised.
```
