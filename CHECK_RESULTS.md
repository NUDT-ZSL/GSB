# 实际检查结果记录

检查日期：2026-11-14
环境：Windows 11，Python 3.14.7，Pillow 12.3.0，numpy 2.5.2（本地离线，无容器）
执行命令：`python generate_samples.py && python sync_calibrator.py manifest.json`

## 控制台输出

```
[完整] fireball
[不可用] frostbolt
[时序缺失] slash
[完整] thunder
[部分完成] quake
[不可用] zerorate
[完整] truncated
[完整] tinydev
[清单条目无效] broken_entry
```

## 逐条验收对照

| # | 要求 | 检查方式 | 结果 |
|---|---|---|---|
| 1 | 条目缺字段不中止，缺失条目有记录 | 清单含缺 `wav` 字段的 `broken_entry` | ✅ 状态"清单条目无效"，后续技能不受影响，总报告有记录 |
| 2 | 第一个非静音采样点为实际起音 | fireball：100ms 静音 + 标称 50ms | ✅ 报告实际起音 100.021ms（正弦首采样过零，第 4801 个采样），偏差 -50 |
| 3 | 全静音 → 不可用，无预览帧 | frostbolt 全静音 WAV | ✅ 状态"不可用"，原因"全部采样均为静音"，无 frostbolt_*.png |
| 4 | 时序缺失仍记录起音、无帧 | slash 时序 JSON 不存在 | ✅ 状态"时序缺失"，实际起音 30.021ms 已记录，无 slash_*.png |
| 5 | 采样率不一致 → 记录实际值并注明需重采样 | thunder 实际 44.1kHz / 声明 48kHz | ✅ `sample_rate: 44100`，notes 含"需重采样"，起音按 44.1kHz 计算（80.023ms） |
| 6 | 偏差四舍五入为整数毫秒 | fireball 标称 50 / 实测 100.021 | ✅ `deviation_ms: -50`（整数） |
| 7 | 帧数 = 最大帧序号 + 1，叠加毫秒 | fireball 关键帧 0/1/2 | ✅ 输出 3 张 PNG；fireball_0.png 显示 t=0.0ms，fireball_2.png 显示 t=33.3ms（60fps） |
| 8 | 坐标越界跳过并记录 | quake 帧 1 (400,100)、帧 3 (-5,60) | ✅ issues 含 2 条"坐标越界"，仅生成 3 张合法帧 |
| 9 | 总报告按清单顺序列状态与帧路径 | 清单 9 条 | ✅ master_report.json 顺序与清单一致，含 frames 路径列表 |
| 10 | 部分完成保留合法帧 | quake 5 帧中 2 帧越界 | ✅ output/ 存在 quake_0/2/4.png 共 3 张，总报告列出 3 个路径 |
| 11 | 采样率 0 → 不可用并说明原因 | zerorate.wav 采样率 0 | ✅ 状态"不可用"，原因含"采样率不合法: 0" |
| 12 | 文件头长度与实际不符不崩溃 | truncated.wav 头部声明完整长度、实际截断 | ✅ 正常读取实际采样，起音 60.021ms，状态"完整"，无异常 |
| 13 | 帧文件名含技能名与帧序号 | fireball 帧 0 | ✅ output/fireball_0.png 存在 |
| 14 | 偏差绝对值 <1ms 报告为 0 | tinydev 实测 0.417ms / 标称 0 | ✅ `deviation_ms: 0`（整数） |
| 15 | 输出目录创建失败 → 报错且无报告 | output_dir 指向已存在同名文件下的路径 | ✅ 控制台输出"错误: 无法创建输出目录 ..."，退出码 1，未生成任何报告文件 |

## 输出产物（output/）

- 预览帧 PNG 共 10 张：fireball_0/1/2、thunder_0/1、quake_0/2/4、truncated_0、tinydev_0
- `sync_deviation_report.json`（同步偏差报告）
- `master_report.json`（总报告，汇总：完整 4 / 部分完成 1 / 不可用 2 / 时序缺失 1 / 清单条目无效 1）

## 备注

- 正弦样例首个采样为过零点（幅值 0），故实测起音比静音长度多 1 个采样周期（如 100.021ms），属真实测量行为，偏差取整后不受影响。
- 单技能处理包在异常捕获中，任一技能抛错仅标记该技能为"不可用"并记录原因，整体流程继续。
