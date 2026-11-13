# 播客单集拼接工具（podcast_concat）

按清单 JSON 顺序把多段 **单声道 / 16bit / 44100Hz PCM WAV** 录音拼接成一集，
相邻片段间插入 0.5 秒静音，标注响度明显偏低的片段，备份成功片段并输出 JSON 报告。
仅使用 Python 标准库，要求 Python 3.10+，无需网络与第三方依赖。

## 文件说明

| 文件 | 作用 |
|---|---|
| `podcast_concat.py` | 主程序：校验清单 → 逐段校验/读取 → 拼接 → RMS 标注 → 备份 → 报告 |
| `make_samples.py` | 确定性生成演示样本（`samples/`）与清单 `samples/manifest.json` |
| `verify_acceptance.py` | 8 条验收标准的自动化实测（临时目录中运行，共 13 项断言） |

## 本地启动方式（可复现）

```bash
# 1. 生成确定性样本与清单
python make_samples.py

# 2. 执行拼接（输出拼接音频、JSON 报告、备份目录）
python podcast_concat.py samples/manifest.json -o output/episode.wav -r output/report.json -b backup

# 3. 运行验收测试
python verify_acceptance.py
```

退出码：`0` 正常完成（含 partial）；`2` 清单格式错误（输出含“清单格式错误”）。

## 清单格式

```json
{
  "segments": [
    {"path": "intro.wav", "title": "开场白"},
    {"path": "main.wav", "title": "正片", "offset": 1.5}
  ]
}
```

- `path`、`title` 必填；`offset` 可选（秒，跳过片段开头，非负数字）。
- 相对路径相对于**清单文件所在目录**解析。
- 清单缺少 `segments` 数组、片段缺少 `path`/`title`、`offset` 非法等，
  均输出“清单格式错误：…”并以退出码 2 终止。

## 业务规则

1. **缺失**：路径不存在 → 报告记“文件缺失”并跳过，其余片段继续。
2. **格式不符**：非单声道 / 非 16bit / 非 44100Hz / 非未压缩 PCM → 报告记录具体原因并跳过。
3. **文件损坏**：无法解析（如空文件、非 WAV 内容）或数据长度与声明不符 → 记“文件损坏”并跳过。
4. **拼接**：按清单顺序拼接有效片段，相邻片段间插入 0.5 秒（22050 帧）零值静音；
   报告中每个片段的 `start_frame` 等于此前片段长度与静音长度之和。
5. **音量过低**：计算各有效片段 RMS，当某片段 RMS 低于**其余有效片段 RMS 均值的 50%** 时，
   在报告该片段 `warnings` 中标注“音量过低”，不修改音频。
   （该判定与验收用例一致：RMS 1000 与 400 → 标注 400；1000 与 600 → 不标注；
   仅一个有效片段时不标注。）
6. **备份**：成功拼接的原始文件按原文件名复制到 `backup/`（目录不存在时创建），
   被跳过的片段不复制。同名文件以清单中靠后的为准（覆盖）。
7. **报告**：`status` 无跳过为 `complete`、有跳过为 `partial`；`success` 含标题/路径/RMS/
   起始帧/时长/警告；`skipped` 含路径与原因；另含输出路径、总帧数、总时长等。

## 业务操作样例

`samples/manifest.json` 故意包含 7 个片段覆盖全部情形：2 个正常、1 个路径缺失、
1 个 48000Hz、1 个立体声、1 个空文件、1 个低音量（RMS≈400）。实际运行结果：

```
[1/7] 开场白 (intro.wav) ... OK（44100 帧，RMS=999.9）
[2/7] 丢失的访谈 (missing.wav) ... 跳过（文件缺失：路径不存在）
[3/7] 正片 (main.wav) ... OK（44100 帧，RMS=999.8）
[4/7] 采样率错误的广告 (wrong_rate.wav) ... 跳过（格式不符：采样率为 48000Hz（要求 44100Hz））
[5/7] 立体声插曲 (stereo.wav) ... 跳过（格式不符：声道数为 2（要求单声道））
[6/7] 损坏的片尾 (corrupt.wav) ... 跳过（文件损坏：无法解析 WAV（不是有效的 WAV 文件或内容为空））
[7/7] 低音量口播 (quiet_ad.wav) ... OK（44100 帧，RMS=400.2）
提示：片段「低音量口播」RMS=400.23，低于其余有效片段均值的 50%，标注为音量过低
拼接输出：output\episode.wav（176400 帧，4.00 秒）   # 3×1s + 2×0.5s 静音
备份完成：backup（3 个文件）                            # 仅 intro/main/quiet_ad
状态：partial（成功 3，跳过 4）
```

## 实际验证结果

`python verify_acceptance.py` 实测输出（Python 3.10.20，Windows 11）：

```
PASS  1a 缺少 segments → 含“清单格式错误”且退出码非0  —— exit=2
PASS  1b 片段缺少 title → 含“清单格式错误”且退出码非0  —— exit=2
PASS  2 缺失路径被记录并跳过，其余继续
PASS  3 48000Hz 记为格式不符并跳过
PASS  4 空 WAV 记为文件损坏并跳过
PASS  7 backup/ 仅含有效片段副本  —— ['a.wav', 'b.wav']
PASS  8 有跳过时 status=partial，success/skipped/输出路径齐全
PASS  8b 无跳过时 status=complete
PASS  5 两个1s片段→2.5s，中间0.5s全为0，波形位置正确  —— frames=110250
PASS  5b 报告起始采样 = 前序片段长度+静音长度之和  —— starts=[0, 66150]
PASS  6a RMS 1000 与 400 → 标注较低者
PASS  6b RMS 1000 与 600 → 不标注
PASS  6c 标注不修改音频
共 13 项，通过 13，失败 0 —— 全部验收通过。
```
