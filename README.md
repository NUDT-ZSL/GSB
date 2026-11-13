# wavmixer — 16-bit PCM 片段时间线混音器

把多个 16 位 PCM WAV 短片段按毫秒时间线叠加成一条混音 WAV，并输出 JSON 处理报告。
报告区分 **完整结果**（`complete`）、**部分片段未参与**（`partial`）和 **失败**（`failed` / `invalid`），
便于音效师判断哪些片段需要替换。

纯标准库实现（`wave`、`json`、`struct`），不需要任何第三方依赖，也不需要外部音频素材：
所有验证样本由 `tools/samples.py` 按固定公式确定性生成。

---

## 1. 环境与启动

要求 Python 3.10+（`wave` 与 `json` 为标准库，无需安装）。

```powershell
# 在仓库根目录 E:\Company\demo\projects\project24
python --version            # 应为 3.10 或更高

# 一条命令跑通全部演示场景（会自动生成样本、清单并调用 CLI）
python demo/run_demo.py
```

命令行的直接用法：

```powershell
python -m wavmixer --manifest demo\manifests\complete.json --out demo\out\complete\mix.wav
```

| 参数 | 说明 |
| --- | --- |
| `--manifest` | 片段清单 JSON 路径（必填） |
| `--out` | 输出混音 WAV 路径（必填） |
| `--report` | 可选，自定义报告路径；默认写到 `<out>.report.json` |
| `--quiet` | 不向 stdout 打印摘要（演示脚本使用） |

退出码：

| 码 | 含义 | 是否生成 WAV |
| --- | --- | --- |
| `0` | 成功，状态为 `complete` 或 `partial` | 是 |
| `2` | 清单字段校验失败（偏移/增益/结构） | 否 |
| `3` | 源格式不匹配，或没有任何可用片段 | 否 |
| `4` | 未预期内部错误 | 否 |

> 失败时报告仍会写到 `<out>.report.json`；若目标位置残留了上一次运行的 WAV，会被删除，
> 以免把旧混音误当成这次的结果。

---

## 2. 清单格式

```json
{
  "clips": [
    { "id": "ambience",   "source": "../samples/ambience.wav",   "offset_ms": 0,   "gain": 0.8 },
    { "id": "footstep",   "source": "../samples/footstep.wav",   "offset_ms": 40,  "gain": 1.0 },
    { "id": "loud_impact","source": "../samples/loud_impact.wav","offset_ms": 320, "gain": 2.0 }
  ]
}
```

- `source`：WAV 路径；相对路径**相对于清单文件所在目录**解析。
- `offset_ms`：非负整数（布尔值不算整数），单位毫秒。`-1` 会被拒绝。
- `gain`：数值，范围 `[0.0, 2.0]`。`3.0` 会被拒绝。
- `id`：可选字符串；缺省时自动命名为 `clip-01`、`clip-02`……

字段错误会**一次性全部收集**，每条都带精确的字段路径，例如：

```json
{
  "status": "invalid",
  "errors": [
    { "field": "clips[0].offset_ms", "message": "... must be a non-negative integer, got -1", "value": -1 },
    { "field": "clips[1].gain",      "message": "... must be a number in [0.0, 2.0], got 3.0",   "value": 3.0 }
  ]
}
```

---

## 3. 处理规则

1. **源格式**：所有参与片段必须能被 `wave` 读取，且为未压缩 **16 位整数 PCM**，采样率、声道数一致。
   任一条不满足时终止（退出码 3），并在 `format_mismatches` / `errors` 中列出不匹配文件名与各自参数。
2. **时间线**：每个片段的起始帧 = `offset_ms` 换帧（四舍五入到最近帧）。
   输出帧数取所有片段 `起始帧 + 片段帧数` 的最大值，再由该帧数换算毫秒得到 `total_duration_ms`
   （向上取整到整毫秒，保证不截断尾音）。因此 `total_duration_ms` 与真实最大结束时间
   `max(offset_ms + 片段毫秒时长)` 的误差不超过 1 ms；输出 WAV 的帧数与 `total_duration_ms`
   严格对应（`frames == round(total_duration_ms * sample_rate / 1000)`，
   因帧数必须为整数，个别采样率下相差 1 帧）。
3. **增益与限幅**：每个样本先乘增益，再四舍五入并限幅到 `[-32768, 32767]`；
   发生限幅时该片段 `clipped` 记为 `true`。
4. **淡入淡出**：每个片段首尾各 5 ms 线性淡入/淡出；片段短于 10 ms 时两端各用**片段长度的一半**。
   淡入第 `i` 帧系数为 `i / N`，淡出末帧系数为 `0`，因此首尾样本均为 0。
   该包络只作用于本片段，不影响其他片段。
5. **叠加**：各片段处理后的样本逐点相加，再限幅到 16 位区间；
   只要有一个叠加值越界，`overall_clipped` 记为 `true`。
6. **输出格式**：采样率、位深（16）、声道数与源文件一致，可被 `wave` 重新读取；
   帧数与 `total_duration_ms` 对应。

> 说明：单条片段即使因增益被限幅（`clipped=true`），只要叠加后总和没有越界，
> `overall_clipped` 仍为 `false`——两个标志分别回答“这个片段本身是否被削”和“混音总线是否被削”。

---

## 4. 报告字段

```json
{
  "tool": "wavmixer",
  "status": "partial",
  "reason": null,
  "manifest": "...\\demo\\manifests\\partial.json",
  "output": "...\\demo\\out\\partial\\mix.wav",
  "total_duration_ms": 300,
  "overall_clipped": false,
  "format": { "sample_rate": 44100, "channels": 1, "bits_per_sample": 16, "frames": 13230 },
  "summary": { "requested": 3, "mixed": 2, "excluded": 1 },
  "clips": [
    { "index": 0, "id": "ambience", "source": "../samples/ambience.wav", "offset_ms": 0,
      "gain": 0.8, "duration_ms": 300, "clipped": false, "status": "mixed", "peak_abs_sample": 6554 },
    { "index": 1, "id": "footstep", "source": "../samples/footstep.wav", "offset_ms": 40,
      "gain": 1.0, "duration_ms": 120, "clipped": false, "status": "mixed", "peak_abs_sample": 19660 },
    { "index": 2, "id": "sword_hit", "source": "../samples/removed_sword_hit.wav", "offset_ms": 150,
      "gain": 1.0, "duration_ms": null, "clipped": null, "status": "excluded",
      "excluded_reason": "unreadable", "detail": "../samples/removed_sword_hit.wav: file not found" }
  ],
  "excluded_sources": [
    { "index": 2, "id": "sword_hit", "source": "../samples/removed_sword_hit.wav",
      "reason": "unreadable", "detail": "../samples/removed_sword_hit.wav: file not found" }
  ],
  "format_mismatches": [],
  "errors": []
}
```

关键约定：

- `status`：`complete`（全部参与）/ `partial`（WAV 已生成但有片段缺失）/ `invalid`（清单非法）/
  `failed`（格式不匹配或无可用户片段，未生成 WAV）。
- `clips` 始终按 `offset_ms` **升序**排列（同偏移保持清单顺序），`index` 保留原始清单下标。
- 参与混音的片段：`clipped` 为布尔值、`status` 为 `mixed`；
  未参与的片段：`clipped` 为 `null`、`status` 为 `excluded`，`detail` 说明原因。
- 顶层 `overall_clipped` 为布尔值（失败时为 `null`）。
- `total_duration_ms` 为整数毫秒。

---

## 5. 演示场景与实测结果

`python demo/run_demo.py` 会重建 `demo/samples` 与 `demo/out`，依次执行四个场景：

| 场景 | 清单 | 退出码 | status | 结果 |
| --- | --- | --- | --- | --- |
| 完整混音，含一条高增益片段 | `manifests/complete.json` | `0` | `complete` | 5/5 参与；`loud_impact` 片段 `clipped=true`，`overall_clipped=true`；总时长 380 ms |
| 引用文件被删除 | `manifests/partial.json` | `0` | `partial` | 2 条参与、1 条 excluded；WAV 正常生成 |
| 偏移 -1、增益 3.0 | `manifests/invalid.json` | `2` | `invalid` | 报告含 `clips[0].offset_ms` 与 `clips[1].gain`；无 WAV |
| 所有源文件缺失 | `manifests/unusable.json` | `3` | `failed` | `reason=no_usable_clips`；无 WAV |

实际输出（节选，2026-11-14 在本机 Python 3.10.20 运行）：

```
SCENARIO: complete mix (one clip clips on gain)
  exit code ............ 0
  report status ........ complete
  output format ........ 44100 Hz, 16-bit, 1 ch, 16758 frames
  total duration ....... 380 ms
  overall clipped ...... True
  clips mixed/excluded . 5 / 0
       0 ms  gain 0.8  ambience     clipped=False
      40 ms  gain 1.0  footstep     clipped=False
     120 ms  gain 1.2  door_creak   clipped=False
     260 ms  gain 1.5  sword_hit    clipped=False
     320 ms  gain 2.0  loud_impact  clipped=True
  wave re-read ......... OK (16758 frames @ 44100 Hz = 380 ms)

SCENARIO: invalid manifest (offset -1, gain 3.0)
  exit code ............ 2
  report status ........ invalid
  error: clips[0].offset_ms: clips[0].offset_ms must be a non-negative integer, got -1
  error: clips[1].gain: clips[1].gain must be a number in [0.0, 2.0], got 3.0
  wav written .......... False

SCENARIO: all sources unavailable
  exit code ............ 3
  report status ........ failed
  reason ............... no_usable_clips
  wav written .......... False
```

---

## 6. 测试

```powershell
python -m unittest discover -s tests -v
```

20 个用例覆盖验收条目：

| 验收项 | 用例 |
| --- | --- |
| 1 偏移/增益校验、非零退出、字段名 | `TestManifestValidation`（含多字段同时报告、布尔值当整数被拒、失败时清理旧 WAV） |
| 2 采样率/位深不一致、列出文件 | `TestSourceFormatValidation` |
| 3 总时长 = 最大结束时间 | `test_total_duration_is_max_end_time`、`test_total_duration_covers_every_clip_end`、`test_output_format_and_report_fields` |
| 4 增益 2.0 触发片段削波、样本不越界 | `test_gain_two_clips_clip_is_flagged` |
| 5 满幅重叠触发整体削波 | `test_overlapping_full_scale_clips_flag_overall_clip` |
| 6 5 ms 淡入淡出与手算对比（±1）、短片段半长、重叠边界 | `test_fade_shape_matches_manual_calculation`、`test_overlap_boundary_matches_manual_calculation`、`test_short_clip_uses_half_length_fades` |
| 7 输出参数与源一致、可被 `wave` 重读 | `TestOutputAndReport` |
| 8 报告字段、布尔状态、升序 | `test_output_format_and_report_fields` |
| 9 缺失文件仍完成混音并区分 | `test_missing_source_is_excluded_but_mix_completes`、`test_corrupt_source_is_reported_as_excluded` |
| 10 全无效不生成 WAV、非零退出 | `test_all_sources_missing_writes_no_wav` |

淡入淡出的手算基准使用 1 kHz 采样率（5 ms 恰为 5 帧），便于逐样本比对。

---

## 7. 目录结构

```
wavmixer/            # 库与 CLI
  manifest.py        # 清单读取与字段校验
  audio.py           # WAV 读写、帧/毫秒换算
  mix.py             # 增益、淡入淡出、限幅、叠加引擎
  report.py          # 报告结构与 JSON 序列化
  pipeline.py        # 端到端流程与退出码
  cli.py / __main__.py
tools/samples.py     # 确定性样本与清单生成
tests/test_wavmixer.py
demo/run_demo.py     # 四个场景的可演示脚本
```

生成物（`demo/samples`、`demo/out`）可随时删除，`run_demo.py` 会重建。
