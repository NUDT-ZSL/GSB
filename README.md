# wav_trim — 播客 WAV 修剪与音量包络工具

命令行工具:输入 16 位 PCM 双声道 WAV,按可选剪切区间(秒)修剪片头片尾、
按可选音量包络列表调整指定时间段增益,输出保持原采样率/声道数/位深的 WAV,
并在输出同目录生成 `report.json` 处理报告。仅依赖 Python 3.10+ 标准库
(`wave` / `json` / `argparse` / `array`)。

## 本地启动方式(可复现)

```powershell
# 1. 生成确定性测试样本(tone5s / loud09 / silence / mono8bit / big≈10.6MB)
python make_samples.py samples

# 2. 业务操作样例:剪掉 5 秒录音的片头 0.5s 与片尾 0.5s,
#    开头 0.5–1.5s 淡出到 30% 音量,结尾 3.5–4.5s 提升 1.8 倍
python wav_trim.py samples/tone5s.wav demo/episode.wav --trim 0.5 4.5 --envelope 0.5,1.5,0.3 --envelope 3.5,4.5,1.8

# 3. 运行全部 9 项验收测试
python test_acceptance.py
```

## 命令行参数

```
python wav_trim.py INPUT.wav OUTPUT.wav [--trim START END] [--envelope START,END,GAIN] ...
```

- `--trim START END`:剪切起止秒;超出音频时长按边界钳制,钳制后 起点≥终点 报错(退出码 1)。
- `--envelope START,END,GAIN`:音量包络三元组,可重复多次。
  - 时间基于**输入(原始)时间轴**,区间 `[start, end)` 内应用恒定增益,区间外不变;
  - 包络可重叠,重叠处增益**相乘**;
  - 增益钳位到 `[0.0, 2.0]`;
  - 每样本乘增益后**饱和截断**到 int16 `[-32768, 32767]`,正负方向均不回绕。

## 行为与退出码

| 情形 | 行为 | 退出码 |
|---|---|---|
| 成功 | 写出 OUTPUT.wav + 同目录 `report.json` | 0 |
| 非 16bit PCM 立体声(位深/声道/编码不符) | 打印 `unsupported format`,不产生输出 | 1 |
| 剪切起点 ≥ 终点(钳制后) | 报错,不产生输出 | 1 |
| 输入全零静音 | 提示 `输入音频为静音`,不产生输出 | 1 |
| 缺参数 / 包络三元组不完整 / 增益非数字 | 打印 usage,无任何输出文件 | 2 |

`report.json` 字段:输入/输出绝对路径、采样率、声道数、位深、实际剪切起止(秒)、
处理后时长与帧数、实际应用的包络列表(增益为钳位后的值)。

## 实现要点

- 仅接受 `comptype=NONE`、`sampwidth=2`、`channels=2` 的 WAV,校验先于任何输出文件创建。
- 两遍处理:第一遍分块扫描选中区域是否全零(静音检查),第二遍按"恒定增益段"
  (由包络边界切分,重叠段增益预乘)分块读写,增益为 1.0 的段原样拷贝,
  内存占用与输入规模无关(块大小 262144 帧),10.6MB 输入实测约 0.6s 完成。
- 时间→帧换算带 `1e-9` 浮点保护,保证 `[start, end)` 区间边界语义精确到帧。

## 文件

- `wav_trim.py` — 工具本体
- `make_samples.py` — 确定性样本生成器(无随机源)
- `test_acceptance.py` — 9 项验收自动化测试
