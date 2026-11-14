# 实际检查记录

检查日期：2026-11-14
环境：Windows 11，Python 3.14.7，NumPy 2.5.2，pytest 9.2.0（未使用容器）

## 1. 静态检查

```text
$ ruff check loop_seams.py make_samples.py tests/test_loop_seams.py
All checks passed!

$ ruff format --check loop_seams.py make_samples.py tests/test_loop_seams.py
3 files already formatted
```

## 2. 自动化测试

```text
$ python -m pytest tests -q
.......................................                                  [100%]
39 passed in 11.75s
```

覆盖内容：清单字段/类型/取值校验、非对象条目、越界接缝点、接缝评分与选择、
0 ms 空循环、接缝点恰好等于音频末尾、截断的 WAV 数据、导出格式与帧数、
三类状态、失败隔离、共用源文件、共用候选点、非零退出码、输出目录创建、
重复运行稳定性、摘要计数一致性。

## 3. 样例批量运行

```text
$ python make_samples.py
已生成样例清单: ...\samples\manifest.json
条目数: 15

$ python loop_seams.py samples/manifest.json
=== 无缝循环导出摘要 ===
清单: samples\manifest.json
输出目录: samples\out
统计: 总条目 15 | 完整导出 5 | 未完成 5 | 无效 5
  [完整导出] loop_perfect.wav -> loop_perfect__loop_001.wav（接缝 750 ms，33075 帧）
  [未完成] loop_short.wav：音频时长 600.000 ms 不足预期循环长度 900 ms
  [未完成] missing.wav：音频文件不存在：samples\audio\missing.wav
  [未完成] silent.wav：整段音频为静音（全部样本为零），不能作为可循环音效导出
  [未完成] broken.wav：无法按 WAV 解码：file does not start with RIFF id
  [未完成] stereo.wav：要求 1 声道，实际为 2 声道
  [无效] out_of_range.wav：候选接缝点超出音频时长：2000 ms（音频时长 1000.000 ms）
  [完整导出] shared.wav -> shared__loop_008.wav（接缝 300 ms，13230 帧）
  [完整导出] shared.wav -> shared__loop_009.wav（接缝 700 ms，30870 帧）
  [完整导出] seam_a.wav -> seam_a__loop_010.wav（接缝 300 ms，13230 帧）
  [完整导出] seam_b.wav -> seam_b__loop_011.wav（接缝 420 ms，18522 帧）
  [无效] loop_perfect.wav：缺少预期循环长度字段（expected_loop_ms）；该条目不作为有效音效
  [无效] loop_perfect.wav：预期循环长度 'expected_loop_ms' 必须是正整数毫秒（当前类型：str）
  [无效] loop_perfect.wav：候选接缝点[1] 不能为负数：-20
  [无效] loop_perfect.wav：候选接缝点[0] 必须是整数毫秒（当前类型：float）
报告: samples\out\loop_report.json
EXIT=0
```

对应需求：

- 3：loop_perfect 的候选为 500/750/880 ms，750 ms 处首尾窗口完全相同，
  选中的正是 750 ms；另两点被构造成差异明显。
- 5：报告条目顺序与清单一致，`loop_perfect.wav` 的
  `expected_loop_ms=800`，`selected_seam_ms=750`，`exported_frames=33075`。
- 9：`loop_short.wav` 源时长 600 ms < 预期 900 ms，状态 `incomplete`，
  报告仍保留 `expected_loop_ms=900`。
- 10：`silent.wav` 标为 `incomplete`（`silent_audio`），未产生 WAV。
- 11：`shared.wav` 两条分别导出 13230 / 30870 帧，文件互不覆盖。
- 12：`seam_a` / `seam_b` 使用同一候选列表 [300, 420]，分别选中 300 / 420，
  报告值互相独立。
- 15：摘要 `完整导出 5 | 未完成 5 | 无效 5` 与报告 `summary` 一致。

## 4. 导出文件核验

用独立脚本读取导出 WAV：

```text
meta(rate,ch,width,frames)= (44100, 1, 2, 33075) expected_frames= 33075
frame_match= True
last,first= -5536 -5536 window_rms= 0.0
evals= [{'ms': 500, 'frame': 22050, 'difference': 30824.618699},
        {'ms': 750, 'frame': 33075, 'difference': 0.0},
        {'ms': 880, 'frame': 38808, 'difference': 30824.618699}]
wav_count= 5
```

结论：采样率 44100 Hz、单声道、16 位与输入一致；帧数 33075 等于
750 ms 对应样本数；导出文件首样本与末样本相同，首尾 64 样本 RMS 差为 0。

## 5. 致命清单错误（需求 13）

```text
$ python loop_seams.py samples/_verify/no_output.json
错误: 清单缺少输出目录字段（output_dir），无法运行
EXIT13=2
```

输出目录未被创建，无任何部分产物。另在测试中覆盖了空字符串、纯空白、
非字符串输出目录、`entries` 非数组、顶层为数组、非法 JSON 等情形，
均返回非零且不创建 `out/`。

## 6. 混合场景（需求 6/7/15）

清单含一条正常、一条文件缺失、一条越界接缝点，输出目录为不存在的
`nested/deep/out`：

```text
统计: 总条目 3 | 完整导出 1 | 未完成 1 | 无效 1
  [完整导出] loop_perfect.wav -> loop_perfect__loop_001.wav（接缝 750 ms，33075 帧）
  [未完成] missing.wav：音频文件不存在：...
  [无效] out_of_range.wav：候选接缝点超出音频时长：2000 ms（音频时长 1000.000 ms）
EXIT15=0
```

目录自动创建，仅写出成功条目的 WAV，越界条目不导出。

## 7. 非法格式不中断整批（需求 8）

```text
统计: 总条目 3 | 完整导出 1 | 未完成 2 | 无效 0
  [未完成] broken.wav：无法按 WAV 解码：file does not start with RIFF id
  [未完成] stereo.wav：要求 1 声道，实际为 2 声道
  [完整导出] seam_a.wav -> seam_a__loop_003.wav（接缝 300 ms，13230 帧）
EXIT8=0
```

程序正常结束，合法音效照常导出。

## 8. 重复运行（需求 14）

同一清单连续运行两次，第二次 `EXIT=0`，`samples/out` 中始终为：

```text
loop_perfect__loop_001.wav
seam_a__loop_010.wav
seam_b__loop_011.wav
shared__loop_008.wav
shared__loop_009.wav
```

文件名含清单序号，重复运行幂等，同源不同长度的条目互不覆盖。

## 已知边界与取舍

- 仅接受未压缩 PCM、44.1 kHz、16 位、单声道 WAV（按需求约定）；
  其余格式归入 `incomplete`，不中断批处理。
- 0 ms 接缝会产生空循环，不参与选择；若候选中只有 0 ms，该条目记为
  `incomplete`（`zero_length_loop`）。
- 越界接缝点按需求 2 优先判定为 `invalid`，优先于静音/时长不足检查。
- 报告中的接缝差异值为整数样本单位的组合分数，仅供比较，不代表物理声压。
