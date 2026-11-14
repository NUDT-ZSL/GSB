# 口型 / 起音偏差离线核对工具

对每一句对白，用帧表里的口型时间点减去该句 WAV 的实际起音点，得到带符号的
偏差量（正值 = 口型晚于起音，负值 = 口型早于起音），并输出可复核的逐句结论与
整体概览，供配音验收参考。

全部在本地完成：Windows + Python 3.10+ + numpy，不联网、不依赖容器或外部账号。

---

## 1. 启动说明

```powershell
# 1) 安装依赖（离线环境可从本地 wheel 安装）
python -m pip install -r requirements.txt

# 2) 生成可复现的业务样例（帧表 + 对白清单 + 7 个 WAV，约 0.7 MB）
python run.py --help
python tools/make_demo.py --out sample

# 3) 运行核对，默认容差 8 ms
python run.py --frames sample/frames.json --dialogues sample/dialogues.json --tolerance-ms 8 --out-dir out

# 4) 运行测试（41 项）
python -m pytest -q
```

`python -m lip_sync ...` 与 `python lip_sync_check.py ...` 两种入口等价。
退出码：`0` 正常结束（含"无有效语句"的情况）、`2` 参数缺失/非法、`3` 输入
文档不可读或不是合法 JSON、`4` 结论文件写出失败。

### 命令行参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `--frames` | 是 | 帧表 JSON 路径 |
| `--dialogues` | 是 | 对白清单 JSON 路径 |
| `--tolerance-ms` | 是 | 超差容差（毫秒，`>= 0`） |
| `--out-dir` | 是 | 输出目录，不存在时自动创建 |
| `--report-name` | 否 | 输出文件名，默认 `lip_sync_report.json` |
| `--quiet` | 否 | 不在终端打印逐句表格 |

---

## 2. 输入格式

两个文件都接受「顶层数组」或「顶层对象 + 下列任一数组字段」两种形态。
字段名兼容若干常见导出别名，便于直接对接现有导出脚本。

### 帧表 `frames.json`

```json
{
  "frames": [
    {"line_id": "L01", "mouth_time_ms": 310},
    {"line_id": "L08", "mouth_time_ms": 400},
    {"line_id": "L08", "mouth_time_ms": 320},
    {"line_id": "L08", "mouth_time_ms": 250}
  ]
}
```

- 数组字段名：`frames` / `rows` / `entries` / `lines`
- 语句标识：`line_id` / `id`（字符串或整数，空串/布尔视为非法）
- 毫秒时间：`mouth_time_ms` / `time_ms` / `mouth_ms` / `t_ms` / `time`
  （必须是有限、非负的数值）
- 同一句出现多个时间点时取**最早**那个作为对齐依据；重复的相同时间点自动去重。

### 对白清单 `dialogues.json`

```json
{
  "dialogues": [
    {"line_id": "L01", "wav_path": "wav/l01_10ms_late.wav", "text": "第一句"},
    {"line_id": "L05", "text": "第五句，路径被删除"}
  ]
}
```

- 数组字段名：`dialogues` / `lines` / `entries`
- WAV 路径：`wav_path` / `audio_path` / `wav` / `wav_file`；
  相对路径以**对白清单所在目录**为基准解析。
- 非法条目只记录到 `input_issues`，不影响其余语句。

---

## 3. 判定规则

| 状态 `status` | 含义 | 是否计入偏移统计 |
|---|---|---|
| `ok` | 偏移绝对值 **不超过** 容差 | 是 |
| `out_of_tolerance` | 偏移绝对值 **超过** 容差 | 是（并计入超差句数） |
| `silent` | 整句峰值低于静音底 `1e-4`（全零样本属于此类） | 否，单独列出 |
| `unverifiable` | WAV 缺失 / 无法解码 / 采样率非 44100 / 声道数非 1 / 有能量但无持续起音段 | 否，单独列出 |
| `pending` | 帧表缺该句口型时间，或对白清单缺该句 WAV 路径 | 否，列为待处理问题 |

起音检测（`lip_sync/audio.py`）：

1. 以 1 ms 窗口计算整段信号的滑动 RMS 能量包络（累加和实现，O(n)）。
2. 阈值取 `max(2e-4, 内部峰值 × 2%)`。**内部峰值**剔除首尾各 10 ms，
   因此首尾淡入淡出与末尾低能量拖尾不会改变阈值。
3. 包络必须连续 4 ms 高于阈值才判为起音，孤立单点脉冲不会被误判。
4. 起音点 = 该持续段起点对应的样本位置换算成的毫秒数。

同一路径的 WAV 每次运行只解码一次（`AudioProbeCache`），各句结论仍独立计算。

---

## 4. 输出

终端打印逐句表格与整体概览；同时写出结论 JSON：

```json
{
  "generated_at": "2026-11-14T15:40:21+00:00",
  "tolerance_ms": 8.0,
  "summary": {
    "total_lines": 8,
    "checked_lines": 5,
    "out_of_tolerance_lines": 3,
    "mean_offset_ms": 2.6,
    "max_offset_ms": -12.0,
    "max_abs_offset_ms": 12.0,
    "silent_lines": 1,
    "unverifiable_lines": 1,
    "pending_lines": 1,
    "no_valid_lines": false
  },
  "lines": [
    {
      "line_id": "L06",
      "mouth_time_ms": 188.0,
      "onset_ms": 200.0,
      "offset_ms": -12.0,
      "status": "out_of_tolerance"
    }
  ],
  "wav_files_decoded": 6
}
```

逐句对象固定包含 `line_id` / `mouth_time_ms` / `onset_ms` / `offset_ms` / `status`
五个键（不可核对时两个数值为 `null`），并按需附带 `reason` / `detail` / `wav_path`。
数值统一保留 3 位小数。

排序：可核对语句（`ok` + `out_of_tolerance`）在前，按 `|offset|` 降序，
同值按 `line_id` 字典序升序；不可核对语句随后按 `line_id` 排列，保证重复运行
输出顺序完全一致。

---

## 5. 实际运行结果（可复现）

```
> python tools/make_demo.py --out sample
样例已生成: D:\demo_GSB\demo\projects\project77\sample
WAV 文件数: 7, 数据总量: 727.4 KiB

> python run.py --frames sample/frames.json --dialogues sample/dialogues.json --tolerance-ms 8 --out-dir out
语句标识          状态            口型(ms)   起音(ms)   偏移(ms)  说明
------------------------------------------------------------------------------
L06               超差             188.0    200.0    -12.0
L01               超差             310.0    300.0     10.0
L08               超差             250.0    240.0     10.0
L02               正常             505.0    500.0      5.0
L07               正常             200.0    200.0      0.0
L03               无声             420.0        —        —  silent_audio
L04               无法核对         210.0        —        —  channels_not_mono
L05               待处理问题       360.0        —        —  missing_wav_path

参与统计 5 句，超差 3 句，平均偏移 2.6 ms，最大偏移 -12.0 ms
无声语句: L03
无法核对: L04
待处理问题: L05

结论已写入: out\lip_sync_report.json
```

样例中每句的构造意图：

| 语句 | 构造 | 期望结论 |
|---|---|---|
| L01 | 300 ms 静音前缀，口型时间 310 ms | 偏移 +10 ms，超差 |
| L02 | 500 ms 静音前缀，口型时间 505 ms | 偏移 +5 ms，正常 |
| L03 | 全零样本 | 无声，不计入统计 |
| L04 | 立体声 WAV | 无法核对（`channels_not_mono`） |
| L05 | 对白清单删掉 `wav_path` | 待处理问题，其余句不受影响 |
| L06 | 与 L07 共用同一 WAV，口型时间 188 ms | 偏移 -12 ms，超差 |
| L07 | 共用 WAV，口型时间 200 ms | 偏移 0，正常 |
| L08 | 三个口型时间点 400/320/250 | 取 250，偏移 +10 ms，超差 |

运行耗时参考（本机 Python 3.14 + numpy 2.5）：上述 8 句、7 个 WAV 完整一次运行
（含解释器启动）0.34 s，进程内核对阶段约 21 ms；
L06/L07 共用录音时 `wav_files_decoded` 为 6 而语句结论为 8 条。

### 容差可配置的实际效果

```
> python run.py --frames sample/frames.json --dialogues sample/dialogues.json --tolerance-ms 4 --out-dir out2 --report-name report_4ms.json
```

| 容差 | 参与统计 | 超差句数 | 新增超差 |
|---|---|---|---|
| 8 ms | 5 | 3 | — |
| 4 ms | 5 | 4 | L02（+5 ms） |

### 参数缺失的实际效果

```
> python run.py --frames sample/frames.json --dialogues sample/dialogues.json --out-dir out
usage: lip-sync-check [-h] --frames FRAMES --dialogues DIALOGUES
                      --tolerance-ms TOLERANCE_MS --out-dir OUT_DIR ...
lip-sync-check: error: the following arguments are required: --tolerance-ms
（退出码 2）
```

---

## 6. 测试覆盖

`python -m pytest -q` → **41 passed**。逐条对应需求：

| 需求 | 测试 |
|---|---|
| 1 非法语句不影响其余 | `test_missing_dialogue_path_isolated`、`test_invalid_frame_entries_isolated` |
| 2 起音点误差 ≤ 5 ms | `test_onset_matches_known_silence_prefix`（参数化 0/5/120/300/777.5 ms） |
| 3 带符号偏移 | `test_signed_offset_positive` / `test_signed_offset_negative` |
| 4 容差判定 | `test_tolerance_boundary`、`test_tolerance_exactly_equal_is_ok` |
| 5 无声语句 | `test_silent_line_listed_and_excluded` |
| 6 同 WAV 只解码一次 | `test_shared_wav_calls_decoder_once`（计数解码调用） |
| 7 缺失/立体声/采样率异常 | `test_stereo_wav_is_unverifiable_others_unchanged`、`test_wrong_sample_rate_is_unverifiable`、`test_missing_wav_file_is_unverifiable` |
| 8 输出 JSON 固定键名 | `test_output_json_schema` |
| 9 排序稳定 | `test_ordering_and_determinism`、`test_ordering_ties_are_lexicographic` |
| 10 整体概览 | `test_summary_statistics` |
| 11 取最早口型时间 | `test_earliest_mouth_time_used` |
| 12 命令行参数校验 | `test_missing_tolerance_exits_nonzero`、`test_nonexistent_frames_path_exits_nonzero`、`test_invalid_json_exits_with_code_3` |
| 13 无有效语句仍出 JSON | `test_empty_dialogue_list_writes_report`、`test_all_lines_unverifiable_still_reports` |
| 14 淡入淡出 / 拖尾稳定 | `test_trailing_low_energy_tail_does_not_move_onset`、`test_fade_in_and_out_are_stable` |

---

## 7. 目录结构

```
lip_sync/          核对工具（audio 起音检测 / inputs 校验 / verify 结论 / cli 入口）
tools/make_demo.py 业务样例生成器（可重复执行，结果一致）
sample/            生成的样例数据（gitignore）
tests/             pytest 测试与共享 helper
run.py             工作区启动入口
```
