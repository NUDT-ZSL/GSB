# Podcast WAV 拼接工具（wav_splicer）

按毫秒剪辑清单从一期 **16 位单声道 PCM WAV** 中截取多个片段，按清单顺序拼接为成片，并输出一份供制片人核对的 JSON 报告。纯标准库实现，Python 3.10+，无需第三方依赖、无需 Docker。

- 采样直接复制源文件的 PCM 字节：**不重采样、不做增益、不做量化**。
- 所有校验在写文件之前完成；失败时不生成成片，只写出 JSON 报告（供核对原因）。
- 成片与报告均以「临时文件 + 原子替换」写入，中途失败不会留下半成品，也不会破坏已存在的旧成片（除非显式 `--force`）。

---

## 1. 环境与启动

```powershell
# 需要 Python 3.10 或更高（本机验证：Python 3.10.20）
python --version

# 仓库根目录即可直接运行，无安装步骤
python wav_splicer.py --help
```

`--help` 实际输出（已核验五个选项齐全）：

```
usage: wav_splicer [-h] --input PATH --plan PATH --output PATH --report PATH
                   [--force]

Cut segments from a 16-bit mono PCM WAV according to a millisecond edit plan,
concatenate them in plan order, and write a JSON report.

options:
  -h, --help     show this help message and exit
  --input PATH   source WAV file (default: None)
  --plan PATH    JSON edit plan (default: None)
  --output PATH  output WAV path (default: None)
  --report PATH  JSON report path (default: None)
  --force        replace the output WAV if it already exists (the report is
                 always rewritten) (default: False)
```

## 2. 业务操作样例

### 2.1 一键可复现演示

```powershell
python demo.py --force
```

它会确定性生成 48 kHz / 10 秒素材与 3 段（含 200 ms 重叠）剪辑清单，运行拼接并逐采样核对：

```
source : ...\demo_out\episode_raw.wav  (10000 ms, 48000 Hz, 16-bit mono)
plan   : ...\demo_out\plan.json  (3 segments)
$ python wav_splicer.py --input ...\demo_out\episode_raw.wav --plan ...\demo_out\plan.json --output ...\demo_out\episode_cut.wav --report ...\demo_out\report.json
wrote ...\demo_out\episode_cut.wav (237600 frames, 4950.000 ms); report: ...\demo_out\report.json

output channels=1 sampwidth=2 rate=48000
output frames=237600 expected=237600 -> OK
output duration=4950.000 ms
byte-identical to source slices: OK
report: {"total_segments": 3, "valid": true, "errors": []}
```

（500–2500、2300–4000、7000–8250 ms 三段，总时长 2000+1700+1250 = 4950 ms，与输出一致。）

### 2.2 手工演示

```powershell
python wav_splicer.py `
  --input  demo_out\episode_raw.wav `
  --plan   demo_out\plan.json `
  --output demo_out\episode_cut.wav `
  --report demo_out\report.json
```

### 2.3 剪辑清单格式

```json
[
  {"start_ms": 500, "end_ms": 2500},
  {"start_ms": 2300, "end_ms": 4000},
  {"start_ms": 7000, "end_ms": 8250}
]
```

也接受 `{"segments": [...]}` 形式。`start_ms` / `end_ms` 必须为整数（布尔值、字符串均拒绝）。
片段按清单顺序拼接，**不要求按时间升序**，也允许互相重叠（每段独立截取后拼接）。

### 2.4 报告格式

```json
{
  "total_segments": 3,
  "valid": true,
  "errors": []
}
```

失败示例（第 1、3 段非法，全部列出并升序）：

```json
{
  "total_segments": 4,
  "valid": false,
  "errors": [
    {"segment_index": 1, "reason": "start_ms must be less than end_ms (got start_ms=900, end_ms=800)"},
    {"segment_index": 2, "reason": "start_ms=1200 is beyond the audio duration of 1000.000 ms; end_ms=1300 is beyond the audio duration of 1000.000 ms"}
  ]
}
```

说明：`segment_index` 为清单中的**从 0 开始**下标；音源/清单级别的错误（文件损坏、格式不支持、覆盖拒绝、计划无法解析）使用 `segment_index: -1`，并排在段级错误之前。每个 error 项只含 `segment_index` 与 `reason` 两个字段。

## 3. 命令行为约定

| 场景 | 退出码 | 成片 | 报告 |
|---|---|---|---|
| 全部合法 | 0 | 写出 | `valid:true, errors:[]` |
| 任意段非法 / 音源格式或损坏 / 清单不可解析 | 1 | **不写出** | `valid:false`，列出全部错误 |
| 成片已存在且未给 `--force` | 1 | 保持原样 | `valid:false`，说明需 `--force` |
| 报告路径已存在 | — | — | 报告始终重写（报告是核对产物，不视为成片） |
| 缺少必需参数（argparse） | 2 | 不写出 | **不写出** |
| 输出路径与输入/清单/报告路径冲突 | 2 | 不写出 | 不写出 |
| 输出目录不存在 / 父路径是文件 / 目标只读 | 1 | 不写出，无临时文件残留 | `valid:false` 并说明原因 |

> 需求要求「未给覆盖选项时退出非 0 且原文件不变」——已满足；`--force` 只影响成片，报告每次都会重建，避免制片人读到上一轮过期结论。

## 4. 校验规则

1. **范围**：`0 <= start_ms < end_ms <= 音频总毫秒时长`。时长比较用整数运算（`end_ms * sample_rate <= total_frames * 1000`），避免浮点边界误判。
2. **格式**：整数 PCM（format 1）、单声道、16 位，采样率 `8000 <= rate <= 48000`（含端点）。同时校验 `block_align`、`byte_rate` 与 `data` 块长度是否自洽。
3. **损坏**：RIFF 声明长度大于实际、块声明长度超出文件、尾部多余字节、`data` 块长度为帧长的非整数倍、缺 `fmt `/`data` 块等，均判为损坏。
4. **采样不变**：成片每个采样来自 `source_bytes[frame_start*2 : frame_end*2]` 的原样切片，测试逐字节比对。
5. **毫秒→帧**：`round(ms * rate / 1000)`；对 8 kHz（1000 可被 8000 整除关系成立的常见剪辑点）与 48 kHz（48 帧/ms）均为精确值。

## 5. 自动化验证（实际运行结果）

```powershell
python -m unittest -v test_wav_splicer.py
```

实际输出：

```
Ran 25 tests in 5.338s

OK
```

25 个用例全部通过，覆盖全部 11 条验收点：

| 需求 | 对应用例 |
|---|---|
| 1 三段合法、时长与采样顺序 | `test_valid_three_segments_concatenates_exactly_in_plan_order`、`test_plan_order_is_respected_for_non_chronological_segments` |
| 2 越界/起点≥终点、报索引、无成片 | `test_invalid_ranges_are_reported_per_segment_and_no_wav_written` |
| 3 重叠片段独立截取 | `test_overlapping_segments_preserve_samples_and_durations` |
| 4 立体声 / 7999 Hz / 非 16 位拒绝；8000、48000 通过 | `test_stereo_input_is_rejected`、`test_sample_rate_below_minimum_is_rejected`、`test_non_16_bit_input_is_rejected`、`test_boundary_sample_rates_8000_and_48000_are_accepted` |
| 5 截断 / 声明长度不符 | `test_declared_length_larger_than_actual_data_is_reported`、`test_physically_truncated_file_is_reported`、`test_non_aligned_data_chunk_is_reported_as_corrupt` |
| 6 已存在默认拒绝、`--force` 替换 | `test_existing_output_requires_explicit_force` |
| 7 逐采样不变 | 需求 1/3 用例中的逐字节断言 + `demo.py` 的 `byte-identical` 检查 |
| 8 报告字段 | `test_success_report_schema` |
| 9 `--help` 五选项；缺 `--output` 不写任何文件 | `test_help_lists_all_five_options`、`test_missing_output_writes_neither_report_nor_wav`、`test_missing_input_writes_nothing` |
| 10 失败不留半成品、旧成片不变 | `test_corrupt_input_with_force_leaves_old_production_untouched`、`test_unwritable_output_location_leaves_no_partial_wav`、`test_output_parent_is_a_file_fails_without_partial_wav`、`test_read_only_existing_output_is_not_replaced_even_with_force`、`test_path_conflicts_are_rejected_before_writing` |
| 11 多非法段升序全列 | `test_multiple_invalid_segments_are_all_listed_in_ascending_order`、`test_malformed_plan_entry_is_reported_with_index` |

所有样本由测试代码用 `wave` 模块确定性生成（无网络、无外部素材）。

## 6. 文件说明

```
wav_splicer.py        # 主程序（WAV 解析/校验、计划校验、拼接、原子写入、CLI）
test_wav_splicer.py   # 25 个端到端用例（子进程调用真实 CLI）
demo.py               # 确定性演示：生成素材→拼接→逐采样核对
demo_out/             # demo.py 输出（已加入 .gitignore）
```
