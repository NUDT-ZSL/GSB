# 实际验证记录

验证日期：2026-11-25（Asia/Shanghai）。

## 验证环境

题目目标环境为 Windows 10+ / Python 3.11；当前交付容器实际使用：

```text
Python 3.10.12
NumPy 1.26.4
SciPy 1.13.1
pytest 8.2.0
SQLite via Python stdlib sqlite3
```

代码使用 Python 3.10+ 兼容语法，未使用 Docker、网络或外部服务。

## 1. 生成确定性样本

命令：

```bash
python generate_samples.py --out samples --seed 380
```

结果：

```text
samples/measurement_task.json
samples/wav/ref_main_24_mono.wav
samples/wav/cand_delay3_16_stereo.wav
samples/wav/cand_44100_24_stereo.wav
samples/wav/cand_16k_16_mono.wav
samples/wav/cand_fractional_delay.wav
samples/wav/cand_phase_bad.wav
samples/wav/cand_peak_silent.wav
samples/wav/bad_rate_32k.wav
samples/wav/bad_bits_8bit.wav
samples/wav/bad_channels_actual_stereo.wav
```

任务共声明 12 条轨，覆盖 16k/44.1k/48k、单声道/立体声、16/24-bit，以及缺失文件、非法起始毫秒、
非法采样率、非法位深和声道声明不一致。

## 2. 一次跑完测量、账本和报告

命令：

```bash
python run_alignment.py run \
  --task samples/measurement_task.json \
  --output output
```

实际摘要：

```json
{
  "input_digest": "51982a5e48f00cf55538b2aacb89e32a383e5054c2c49965172064aa970d2efc",
  "ledger_digest": "42221157c4a86cd86412c4e60d368f36db6e91ae7136b3cc263a35f84ea4d8fd",
  "summary": {
    "business_errors": 8,
    "measured": 6,
    "passed": 4,
    "total": 12
  },
  "track_count": 12
}
```

轨级实际结论：

| 轨 ID | 状态 | 通过 | 业务码 | 整数时延 | float64 时延 |
| --- | --- | ---: | --- | ---: | ---: |
| `ref_main` | measured | 是 |  | 0 | 0.000 |
| `cand_delay3` | measured | 是 |  | 3 | 3.000 |
| `cand_44100` | measured | 是 |  | 4 | 4.000 |
| `cand_16k` | measured | 是 |  | 6 | 6.000 |
| `cand_fractional` | measured | 否 | `E_DELAY` | 4 | 4.396 |
| `cand_phase_bad` | measured | 否 | `E_PHASE` | 5 | 5.222 |
| `cand_peak_silent` | error | 否 | `E_PEAK` | - | - |
| `bad_rate` | rejected | 否 | `E_RATE` | - | - |
| `bad_bits` | rejected | 否 | `E_BITS` | - | - |
| `bad_channels` | rejected | 否 | `E_CHANNELS` | - | - |
| `missing_file` | rejected | 否 | `E_FILE` | - | - |
| `bad_start` | rejected | 否 | `E_START` | - | - |

`cand_phase_bad` 的高频段定位结果：

```text
band_id=high_2600_6400
point_count=930
phase_delay_samples=8.056433494321887
phase_delay_q=8056433
integer_delay_q=5000000
phase_error_q=3056433
phase_tolerance_q=750000
passed=false
code=E_PHASE
```

顶层业务码清单：

```text
E_BITS, E_CHANNELS, E_DELAY, E_FILE, E_PEAK, E_PHASE, E_RATE, E_START
```

`--strict` 模式按约定返回退出码 `4`；普通模式只要报告和账本成功提交即返回 `0`，轨级异常由
`business_codes` 表达。

## 3. 重复运行确定性

连续执行同一 run 命令后：

```text
output/alignment_report.json: OK
output/alignment_ledger.sqlite: OK
```

即报告字节和 SQLite 数据库字节在当前环境重复运行后均未变化；测试还逐字段比较了账本规范化记录。

## 4. 自动化测试

命令：

```bash
python -m pytest -q
```

实际结果：

```text
.............                                                            [100%]
13 passed in 8.70s
```

测试分布：

- `tests/test_validation_and_segments.py`：3 项，业务码隔离和采样级边界。
- `tests/test_measurement_and_ledger.py`：4 项，时延 / 相位 / 峰值、定点双路记录、查询和确定性。
- `tests/test_recovery_and_concurrency.py`：6 项，频段 / 轨边界中断、强杀子进程恢复、内存顺序准入、
  异常取消和 `E_DIGEST` 恢复。

## 5. 强杀与恢复一致性

测试和手工验收均使用隐藏故障注入参数模拟强杀：

```bash
python -m acoustic_alignment.cli run \
  -t /tmp/acc_samples/measurement_task.json \
  -o /tmp/acc_interrupt \
  --fault-exit-after-commit 2
# 进程在第二次账本提交后以 os._exit(9) 退出

python -m acoustic_alignment.cli run \
  -t /tmp/acc_samples/measurement_task.json \
  -o /tmp/acc_interrupt

python -m acoustic_alignment.cli run \
  -t /tmp/acc_samples/measurement_task.json \
  -o /tmp/acc_oneshot --force
```

实际校验结论：

```text
digests ledger True
keys equal True 12 12
report core equal True
```

这表示恢复运行与不中断一次跑完的账本摘要、全部账本字段和报告核心结论一致。

## 6. 摘要损坏恢复

手工篡改报告 `track_count=999`，并把 SQLite `meta.ledger_digest` 改为 `bad` 后执行：

```bash
python -m acoustic_alignment.cli verify \
  -t /tmp/acc_samples/measurement_task.json \
  -o /tmp/digest_test
```

实际返回的恢复信息包含：

```text
code=E_DIGEST
problems=[
  ledger_restored_from_valid_backup,
  report_track_count_mismatch,
  report_track_entries_mismatch
]
restored_valid_ledger_backup=true
track_count=12
```

恢复后报告条目数回到 12，账本摘要恢复为有效备份中的摘要。

## 7. 仍建议在 Windows / Python 3.11 目标机执行的复核

当前 Linux 容器已验证算法、SQLite、原子替换和子进程恢复；交付到 Windows 目标机后建议补跑：

```powershell
py -3.11 -m pip install numpy scipy pytest
py -3.11 -m pytest -q
py -3.11 generate_samples.py --out samples --seed 380
py -3.11 run_alignment.py run -t samples\measurement_task.json -o output
```

Windows 上可另开终端执行 `taskkill /PID <pid> /F` 模拟强制终止，或发送 Ctrl+C 验证协作退出码 `130`
与 `output\interruption.json`。
