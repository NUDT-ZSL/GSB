# ClipForge — PCM WAV 清单裁剪与顺序拼接

按 JSON 清单把单声道 / 双声道 PCM WAV 长录音裁成多个片段，并按**清单顺序**拼接为成片；
同时产出 `report.json`，逐项区分「已写入成片的片段」与「未处理的清单项」及原因。

仅使用 Python 3.10+ 标准库，无第三方依赖、无容器。

## 1. 环境与启动

```powershell
# 仓库根目录：E:\Company\demo\projects\project7
python --version                 # 需要 3.10+
python -m unittest discover -s tests -v   # 28 项验收/边界测试
python demo.py                   # 业务演示（生成 demo-output/ 下全部素材与报告）
```

单次业务调用：

```powershell
python -m clipforge `
  --source  D:\audio\recording.wav `
  --manifest D:\audio\clips.json `
  --output  D:\audio\final.wav `
  --report  D:\audio\report.json
```

`--report` 可省略，默认写到成片同目录的 `report.json`。

### 退出码

| 退出码 | 含义 |
| --- | --- |
| 0 | 成片已生成（可能含被跳过的非法清单项，见报告） |
| 2 | 输入错误：源缺失/非 PCM、清单缺失/非法 JSON/结构错误、输出路径冲突 |
| 3 | 清单中没有任何合法片段，未生成成片 |
| 4 | 写输出失败或写后自检失败（已删除半成品） |

## 2. 清单格式

顶层为数组，按数组顺序处理（不按时间排序）：

```json
[
  {"start": 0.0, "end": 0.75},
  {"start": 1.25, "end": 1.75, "gain": 0.5},
  {"start": 2.5, "end": 3.0, "gain": 1.5}
]
```

- `start` / `end`：秒（浮点），按源采样率换算为帧边界，采用「0.5 远离 0」取整；
- `gain`：可选，默认 `1.0`，合法范围 `[0.0, 4.0]`（含端点）；
- 片段可相邻，也可重叠；重叠不会合并，各段完整保留后顺序拼接。

单条清单项非法**不会**中止任务，只跳过该项并写入报告；以下情况整单失败：
清单文件缺失/非法 JSON/顶层不是数组，或没有任何合法项。

## 3. 报告结构（report.json）

```json
{
  "schema_version": "1.0",
  "status": "success",
  "summary": {
    "total": 6, "processed": 3, "unprocessed": 3,
    "processed_indices": [0, 2, 5], "unprocessed_indices": [1, 3, 4]
  },
  "output": {
    "path": "...\\edit-mixed.wav", "created": true, "complete": false,
    "frames": 66150, "duration_seconds": 1.5,
    "sample_rate": 44100, "channels": 2, "sample_width": 2, "sha256": "..."
  },
  "verification": {"wave_readback": "ok", "riff_structure": "ok",
                   "params_match_source": true, "readable_frames": 66150},
  "clips": [
    {"manifest_index": 0, "label": "#1", "status": "processed",
     "start_seconds": 0.0, "end_seconds": 0.5, "gain": 1.0,
     "start_frame": 0, "end_frame": 22050, "frames": 22050,
     "output_offset_frame": 0, "reasons": []},
    {"manifest_index": 1, "label": "#2", "status": "unprocessed",
     "start_seconds": 0.8, "end_seconds": 1.3, "gain": 5.0,
     "reason": "invalid_gain",
     "reasons": [{"code": "invalid_gain",
                  "message": "gain 必须是 [0.0, 4.0] 内的数值，实际为 5.0"}]}
  ],
  "errors": []
}
```

- `clips` 与清单**同序同长**，`status` 只有 `processed` / `unprocessed` 两种；
- `output.complete`：清单项全部合法时为 `true`，有跳过项时为 `false`；
- 失败时 `status=failed`，`errors[]` 给出原因，`output.created=false`；
  若输出路径原本已有旧文件，`output.pre_existing=true` 表明它是历史遗留、本次未写入。

### 未处理原因码

| 原因码 | 说明 |
| --- | --- |
| `out_of_bounds` | 区间超出源时长 |
| `start_after_end` | `start >= end` |
| `negative_start` | `start < 0` |
| `invalid_gain` | gain 非数值或超出 `[0, 4]` |
| `invalid_entry` | 条目不是对象 / 缺 start、end / 类型非法 |
| `zero_length` | 换算后不足 1 帧 |
| `run_failed` | 条目本身合法，但整次任务失败故未写入 |

## 4. 实际验证结果

环境：Python 3.10.20（Windows 11）。以下为实际运行输出。

### 4.1 自动化测试（28 项全部通过）

```text
$ python -m unittest discover -s tests -v
...
test_req1_three_valid_entries_reported ... ok
test_req2_two_half_second_clips_make_one_second ... ok
test_req3_output_format_preserved_and_fully_readable ... ok
test_req4_stereo_channels_kept_independent ... ok
test_req5_gain_out_of_range_rejected ... ok
test_req6_out_of_bounds_item_skipped_with_reason ... ok
test_req7_missing_source_nonzero_exit_and_no_output ... ok
test_req8_output_equals_valid_only_concatenation ... ok
...
----------------------------------------------------------------------
Ran 28 tests in 1.923s

OK
```

验收点与测试的对应：

| 验收要求 | 测试 |
| --- | --- |
| 1. 3 条合法项逐项列记录 | `test_req1_three_valid_entries_reported` |
| 2. 两段 0.5s 拼成 1.0s 且顺序一致 | `test_req2_two_half_second_clips_make_one_second` |
| 3. 采样率/声道/位宽保留、全部帧可读 | `test_req3_output_format_preserved_and_fully_readable` |
| 4. 双声道独立裁剪不混音 | `test_req4_stereo_channels_kept_independent`、`..._stereo_gain_...` |
| 5. gain 缺省=1.0、越界非法 | `test_req5_default_gain_equals_explicit_one`、`test_req5_gain_out_of_range_rejected`、`test_req5_gain_boundaries_accepted`、`test_req5_gain_half_scales_samples`、`test_req5_gain_saturates_instead_of_wrapping` |
| 6. 越界 / start>=end 逐项原因 | `test_req6_out_of_bounds_item_skipped_with_reason`、`test_req6_inverted_and_negative_ranges_rejected` |
| 7. 源缺失 / 非 PCM 非零退出、无成片 | `test_req7_missing_source_...`、`test_req7_non_pcm_source_rejected`、`test_req7_garbage_file_rejected` |
| 8. 合法拼接不受非法项影响 | `test_req8_output_equals_valid_only_concatenation` |

额外覆盖：空清单、坏 JSON、清单非数组、坏条目、重叠区间、位宽 8/16/24/32、
流式分块边界、源数据截断、输出路径冲突、报告与成片的确定性。

### 4.2 业务演示（`python demo.py`，退出码 0）

样本：2 声道 / 16 bit / 44100 Hz / 3.000 秒，确定性生成。

**场景 A：3 条合法项（gain 缺省 / 0.5 / 1.5）**

```text
成片已生成：...\demo-output\edit-valid.wav (77175 帧 / 1.750 秒)
片段：共 3 项，已处理 3 项，未处理 0 项

  status            : success
      #1 processed    帧 [0, 33075) 输出偏移 0 gain=1.0
      #2 processed    帧 [55125, 77175) 输出偏移 33075 gain=0.5
      #3 processed    帧 [110250, 132300) 输出偏移 55125 gain=1.5

  成片 edit-valid.wav: 77175 帧 / 1.750 秒, 2 声道, 16 bit, 44100 Hz
```

**场景 B：3 合法 + 3 非法混排**

```text
成片已生成：...\demo-output\edit-mixed.wav (66150 帧 / 1.500 秒)
片段：共 6 项，已处理 3 项，未处理 3 项

      #1 processed    帧 [0, 22050) 输出偏移 0 gain=1.0
      #2 unprocessed  gain 必须是 [0.0, 4.0] 内的数值，实际为 5.0
      #3 processed    帧 [66150, 88200) 输出偏移 22050 gain=2.0
      #4 unprocessed  片段 [3.2, 4.0] 秒超出源时长 3.000000 秒（132300 帧）
      #5 unprocessed  start 必须小于 end，实际 start=2.6，end=2.4
      #6 processed    帧 [88200, 110250) 输出偏移 44100 gain=1.0

  成片与仅合法清单结果逐字节一致：True
```

即：把非法项删掉后单独跑一遍，两份 WAV 的字节完全相同，证明合法数据未被污染。

**场景 C：源文件缺失**

```text
任务失败（退出码 2）
  source_missing: 源文件不存在：...\demo-output\no-such-source.wav
  未处理片段：3 项 (索引 [0, 1, 2])

  退出码 2，成片存在：False，报告存在：True
```

### 4.3 手工核对示例

```powershell
# 用 wave 模块核对成片参数与源一致
python -c "import wave; w=wave.open(r'demo-output\edit-mixed.wav'); print(w.getnchannels(), w.getsampwidth()*8, w.getframerate(), w.getnframes())"
# 输出：2 16 44100 66150
```

## 5. 实现要点

- **帧边界换算**：`seconds * sample_rate` 后「0.5 远离 0」取整，不使用 Python
  `round()` 的银行家舍入，保证跨平台确定性。
- **流式处理**：片段按 65536 帧分块读取、增益、写出，长录音不会整段驻留内存。
- **增益**：`gain == 1.0` 直接透传原始字节；其余按「取整 + 饱和裁剪」处理，
  不会发生 16 bit 回绕。8 bit 按无符号中点 128 处理。
- **原子写出**：先写同目录临时文件，自检通过后 `os.replace` 到目标；任何失败都
  删除临时文件，不会留下半成品或破坏同名的旧成片。
- **写后自检**：用标准 `wave` 回读参数与全部可读字节，并独立解析 RIFF 块长度，
  两者都写入 `report.json` 的 `verification`。
- **体积**：仅 `clipforge/` 包 + 测试，无第三方依赖。

## 6. 目录结构

```text
clipforge/
  __init__.py
  __main__.py      # python -m clipforge
  cli.py           # 参数解析、退出码、控制台摘要
  manifest.py      # 清单解析与逐项校验（原因码）
  processor.py     # 主流程、report.json、写后自检
  wavio.py         # PCM WAV 读取、增益、原子写出、RIFF 校验
tests/
  helpers.py       # 确定性样本构造与解码
  test_clipforge.py
demo.py            # 业务演示脚本（会重建 demo-output/）
demo-output/       # 演示产物（可随时删除重建）
```
