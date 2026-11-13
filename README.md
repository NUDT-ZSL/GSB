# WAV 时间片段变速变调工具（wavedit）

对本地 PCM WAV 语音按 JSON 配置做时间片段的变速变调，并拼回原时间轴；未编辑区域与原文件逐字节一致。纯 Python 标准库实现（≥3.10），无第三方依赖，Windows 下验证通过。

## 快速开始

```powershell
# 1. 生成演示样本与配置（samples/ 目录）
python gen_samples.py

# 2. 执行业务操作：源WAV + 配置JSON + 输出路径
python wavedit.py samples\voice_44k_stereo16.wav samples\config_single.json out.wav

# 3. 运行全部 15 项验收测试
python run_tests.py
```

## 命令行接口

```
python wavedit.py <源WAV> <配置JSON> <输出WAV>
```

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 源文件缺失 / 无法解析 / 不支持的WAV格式（stderr 含“不支持的WAV格式”） |
| 2 | 配置错误（缺字段、字段越界、JSON 无法解析，stderr 指出具体字段） |
| 3 | 输出路径不可写 |

成功时终端打印摘要，例如：`编辑操作：1 个，总时长：1.5 秒，输出字节：264644 字节`。

## 配置格式

```json
{"edit": [
  {"start": 0.5, "end": 1.5, "speed": 2.0, "pitch": 2.0},
  {"start": 1.0, "end": 3.0, "speed": 0.5, "pitch": -1.0}
]}
```

- `edit` 为编辑操作列表，每项必须含 `start`/`end`（秒）、`speed`（>0）、`pitch`（半音，|pitch|≤48）。
- 片段允许重叠：按定义顺序施加，后定义操作作用于前一操作的结果。
- 起止超出音频时长时截断到有效范围；起止按毫秒换算样本位置并四舍五入。
- `edit` 为空列表时把源文件原样复制到输出路径并提示“无编辑操作”。

## 处理规则

- 仅接受 PCM（format=1）、采样率 8000–48000Hz、位深 8/16/24/32、声道 1 或 2 的 WAV。
- 每片段先做**不变速音调调整**（重采样变调 + OLA 时长还原），再做**变速**（线性插值重采样），变速后样本数按速度比例四舍五入：`round(len/speed)`。
- 浮点计算后按位深饱和舍入；8 位无符号，16/24/32 位有符号。
- 多声道施加相同参数、逐声道同步处理，保持声道对齐与相对波形关系。
- 输出 WAV 的采样率、位深、声道数与源一致；未编辑区间采样与原文件逐字节一致。
- 输出先写临时文件再原子替换：处理失败时输出路径无残留，源文件不受影响。
- 处理全程确定性（无随机数、无时间戳），同一配置多次运行输出 MD5 相同。

## 业务操作样例

```powershell
# 片段 0.5~1.5 秒提速 2 倍并升 2 个半音
python wavedit.py samples\voice_44k_stereo16.wav samples\config_single.json out.wav
# -> 编辑操作：1 个，总时长：1.5 秒，输出字节：264644 字节

# 重叠 + 越界（end=3.0 超出 2 秒时长，自动截断）
python wavedit.py samples\voice_44k_stereo16.wav samples\config_overlap.json out2.wav
# -> 编辑操作：2 个，总时长：2.0 秒，输出字节：352844 字节

# 含中文与空格的输出路径
python wavedit.py samples\voice_44k_stereo16.wav samples\config_single.json "测试 输出\演示 结果.wav"
```

## 实际验证结果

`python run_tests.py` 输出（15/15 通过）：

```
[PASS] 1 命令行正常处理退出码0且输出为可解析PCM WAV
[PASS] 2 非PCM输入stderr含'不支持的WAV格式'且退出码1
[PASS] 3 缺speed字段时报出该字段且退出码2
[PASS] 4 重叠且越界配置总时长与截断结果一致（期望88200帧 实际88200帧）
[PASS] 5 1秒片段速度2.0输出约0.5秒（误差0.000ms）
[PASS] 6 未编辑区间采样与原文件逐字节一致
[PASS] 7 输出参数与源一致且数据块可完整读出
[PASS] 8 8/16/24/32位极值样本编辑后均在合法范围
[PASS] 9 立体声样本数相等且声道波形关系保持
[PASS] 10 空edit列表输出与原文件逐字节一致且有提示
[PASS] 11 连续两次运行输出MD5相同
[PASS] 12 源缺失退出码1；输出不可写退出码3
[PASS] 13 含中文与空格的路径下输出正常生成
[PASS] 14 失败后输出路径无残留且源文件哈希未变
[PASS] 15 终端摘要含编辑操作数/总时长/输出字节
```

## 文件说明

- `wavedit.py` — 主程序（CLI、WAV 解析/构造、PCM 编解码、变速变调 DSP）
- `gen_samples.py` — 生成确定性测试样本与配置
- `run_tests.py` — 15 项验收自动化测试
- `samples/` — 生成的样本与配置（含非PCM、各位深极值样本）
