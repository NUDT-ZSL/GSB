# 航线广播录音裁剪工具（audio_clip）

将单声道 16 位 PCM 广播录音按秒区间裁剪，首尾加线性音量渐变，输出裁剪后 WAV 与 `report.json` 处理报告。仅使用 Python 标准库（Python 3.10+），无第三方依赖、无网络访问。

## 启动方式

```powershell
# 1. 生成确定性测试样本（单声道/立体声/8位/损坏文件，位于 samples\）
python make_samples.py

# 2. 业务操作样例：截取 1.0s–3.5s，淡入淡出各 0.5s
mkdir out
python audio_clip.py --input samples\mono_16bit.wav --output-dir out `
    --start 1.0 --end 3.5 --fade-in 0.5 --fade-out 0.5

# 3. 一键验收（11 项检查）
python verify.py
```

## 参数

| 参数 | 含义 |
|---|---|
| `--input` | 输入 WAV（仅单声道 16 位 PCM） |
| `--output-dir` | 输出目录，须已存在且可写（不会自动创建） |
| `--start` / `--end` | 裁剪起止秒，起始须早于结束且落在音频时长内 |
| `--fade-in` / `--fade-out` | 淡入/淡出秒数，各自及两者之和均不得超片段长度 |
| `--output-name` | 可选，输出 WAV 文件名（默认 `<输入名>_clip.wav`） |

无参数运行打印用法并以退出码 2 退出；`--help` 列出全部参数说明。

## 输出与报告

- 成功：输出 WAV 保持输入采样率与位深，帧数 = 区间秒数 × 采样率，淡入首帧与淡出末帧样本值为 0；`report.json` 含输入/输出文件名、原始与裁剪后时长（秒）、淡入淡出时长、`status: "success"`。
- 失败（格式/区间/渐变/损坏输入）：不生成输出 WAV；输出目录可写时写出同结构 `report.json`（`status: "failed"`，`error` 为可读原因），否则明确提示报告未能写出。
- 输出目录不存在或不可写：提示无法写入目标，不创建目录、不生成任何文件，退出码非零。
- 输出 WAV 先写临时文件再原子替换，异常时不留半成品。

## 业务操作样例（异常路径演示）

```powershell
python audio_clip.py --input samples\stereo_16bit.wav --output-dir out --start 1 --end 3 --fade-in 0.5 --fade-out 0.5
# -> 处理失败：仅支持单声道WAV，当前输入为 2 声道；out\report.json 记录失败

python audio_clip.py --input samples\mono_16bit.wav --output-dir out --start 2 --end 1 --fade-in 0.1 --fade-out 0.1
# -> 处理失败：裁剪区间无效：起始秒（2.0）必须早于结束秒（1.0）

python audio_clip.py --input samples\mono_16bit.wav --output-dir out --start 1 --end 2 --fade-in 0.8 --fade-out 0.8
# -> 处理失败：渐变总长（淡入 0.8 + 淡出 0.8 = 1.6 秒）超过片段长度（1.000 秒）

python audio_clip.py --input samples\corrupt.txt --output-dir out --start 0 --end 1 --fade-in 0.1 --fade-out 0.1
# -> 处理失败：无法读取WAV文件：文件损坏或不是有效的WAV（无堆栈信息）
```

## 验证结果

`python verify.py` 实跑结果：共 11 项，通过 11 项（覆盖任务验收 1–9，含成功路径帧数 20000/20000、首末帧样本值均为 0、报告时长误差 ≤1ms）。
