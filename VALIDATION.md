# Validation Record

## Environment

```text
Python 3.10.12
numpy 1.26.4
SQLite 3.37.2
WSL/Linux local filesystem, no network
```

代码只使用跨平台标准库与 `numpy`，目标运行环境为 Windows 10/11 的
Python 3.11+。本容器未提供 Windows 图形会话，因此实际命令在本地
WSL/Linux 文件系统执行；路径处理、信号处理、SQLite、`os.replace` 和
`fsync` 均使用跨平台 API。

## Automated tests

Command:

```text
python -m unittest discover -s tests -t . -v
```

Result:

```text
Ran 13 tests
OK
```

Covered:

- v1 相对秒 + 逻辑原点换算为绝对毫秒
- v2 代数/生效时刻裁决与 6 条来源可定位冲突
- 新代墓碑压制旧记录
- 损坏 v2 分片产生 `E_DIGEST`，有效来源继续
- 滑窗采样点对齐与 `zero_pad` / `shorten` / `drop`
- 声道权重归一化
- 双预算调度峰值不超限，结果按声明顺序
- 非法位深、采样率声明、权重分别产生可区分业务码
- 三条异常注入全部定位
- 全静音 `E_SILENT` 且无非法 JSON 浮点
- 建议规则变更时测量修订号不变、3/3 测量复用
- 16 位与 24 位输出、目标采样率与理论帧数
- 中断标记、部分报告、SIGTERM 退出 `143`、恢复完成
- 损坏测量分块摘要回退并重做
- 报告替换失败时音频与报告均保持旧版本
- 运行期首错返回 `E_WAV` 且不替换旧交付物

## Fixed-seed demonstration

```text
python -m audio_pipeline demo --workspace demo --force
```

The command returns `3` by design because the fixture contains injected
anomalies.

| input | status | code | input frames | windows | gated | LUFS | DR | verdict | action | output frames |
|---|---|---:|---:|---:|---:|---:|---:|---|---|---:|
| promo | measured | - | 124800 | 37 | 27 | -11.272904 | 19.104681 | above_max | limit, -11.727096 dB | 124800 |
| voice | measured | - | 88200 | 22 | 22 | -28.093055 | 4.461452 | within | gain_up, +4.093055 dB | 96000 |
| bad_depth | rejected | E_BIT_DEPTH | 44100 | - | - | - | - | - | - | - |
| silent | measured | E_SILENT | 57600 | 23 | 0 | null | null | unavailable | none | 57600 |
| wrong_rate | rejected | E_SR | 57600 | - | - | - | - | - | - | - |

Summary:

```text
declared_inputs: 5
measured_inputs: 3
rejected_inputs: 2
criteria_conflicts: 6
anomalies: 3
anomalies_detected: 3
peak_decode_bytes: 241664 / 1048576
peak_sample_bytes: 8119840 / 16777216
```

Output WAV checks:

```text
promo.wav   48000 Hz, 24 bit, 124800 frames
voice.wav   48000 Hz, 24 bit,  96000 frames
silent.wav  48000 Hz, 24 bit,  57600 frames
```

All output frame counts equal the rational-rate theoretical values.

## Interrupt and resume

```text
run --simulate-interrupt-after-chunks 2
exit 143
state/interrupted.json = {signal: 15, boundary: window-chunk, next_window: 4}
output/report.json absent

run --resume
exit 3
measured 3, rejected 2, interrupted.json removed, report/audio committed
```

The interrupted chunk prefix is retained; only digest-valid persisted chunks
are consumed. Corrupting one chunk produced `E_DIGEST`, discarded that chunk
and all successors, then recomputed them successfully.

## Decision-only rerun

Changing only the v2 promotion compression suggestion:

```text
measurement_revision: unchanged
decision_revision:    changed
measurement_reused_inputs: 3
promotion limiter: false after disabling the rule
```

This confirms measurement reuse and decision-only recomputation.

## Atomic commit rollback

With existing outputs and `AUDIO_PIPELINE_FAIL_COMMIT=report`:

```text
exit 4
state/partial_report.json written
all old WAV bytes unchanged
old report bytes unchanged
no temporary or backup files left in output/
```
