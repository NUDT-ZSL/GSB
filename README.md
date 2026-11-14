# subaudit — 离线字幕 vs 人声发声区间逐句核查

配音更换后，字幕可能整体领先人声或在句尾拖尾。本工具在**完全离线**条件下：

1. 读取 44.1kHz 单声道对白 WAV 与字幕清单 JSON；
2. 用确定性能量 VAD 检测每句人声的实际发声起止（毫秒）；
3. 逐句比较字幕/人声起止偏差，判断字幕是否覆盖人声、偏差是否超出容忍；
4. 输出结构化 JSON 报告，无效条目、返工句子、单句失败分别单列。

无网络调用、无随机数、无容器；同一份输入任意次运行结果逐毫秒一致。

## 1. 本地启动步骤

环境要求：Python 3.10+（开发验证于 Python 3.14）、Windows / macOS / Linux。

```powershell
# 1) 安装公开依赖（numpy、soundfile）并以可编辑方式注册 subaudit 命令
python -m pip install -r requirements.txt
python -m pip install -e .

# 2) 生成确定性样本（WAV + 字幕清单，4 个场景）
python -m subaudit.cli samples --scenario all --out-dir ./samples

# 3) 执行核查
python -m subaudit.cli audit .\samples\normal.wav .\samples\normal.json -o report.json

# 4) 跑全部验收测试（10 项，对应需求 1-8）
python -m pytest -q
```

也可不安装直接用模块方式运行：`python -m subaudit.cli ...`（依赖 `src/` 在
PYTHONPATH 中，pytest 已在 pyproject.toml 配置好）。

## 2. 输入格式

字幕清单为 JSON 数组，每条一个对象：

```json
[
  {"id": "S1", "text": "第一句对白", "start_ms": 800, "end_ms": 2200}
]
```

| 字段 | 含义 |
| --- | --- |
| `id` | 句号（字符串或数字，不可为空、不可重复） |
| `text` | 字幕文本（可选） |
| `start_ms` / `end_ms` | 字幕起点/终点毫秒，必须为有限数字且 `start < end` |

## 3. 输出报告说明

```json
{
  "status": "complete",
  "parameters": { "sample_rate": 44100, "tolerance_ms": 200.0, "...": "..." },
  "results": [
    {
      "order": 1,
      "id": "S1",
      "subtitle_start_ms": 800.0,
      "subtitle_end_ms": 2200.0,
      "voice_start_ms": 1000,
      "voice_end_ms": 2000,
      "start_deviation_ms": -200.0,
      "end_deviation_ms": 200.0,
      "conclusion": "ok",
      "issues": []
    }
  ],
  "invalid_entries": [],
  "flagged": [],
  "failures": [],
  "errors": []
}
```

* **偏差符号约定**：`偏差 = 字幕时间 − 人声时间`。负值 = 字幕领先人声；正值 = 字幕拖尾。
  例：人声 1000–2000ms、字幕 800–2200ms → 起点偏差 −200、终点偏差 +200。
* `conclusion` 取值：
  - `ok`：字幕覆盖人声且偏差在容忍范围内（默认 ±200ms，`--tolerance-ms` 可调）；
  - `out_of_tolerance`：未覆盖人声（`voice_leading_subtitle` /
    `voice_trailing_subtitle`）或覆盖超出容忍（`subtitle_starts_too_early` /
    `subtitle_ends_too_late`），句号同时进入顶层 `flagged`；
  - `no_voice_detected`：该句时间窗内未检出语音，`voice_start_ms/end_ms` 与偏差均为
    `null`，不会被当作零偏差。
* `invalid_entries`：句号重复、起止倒置、非数字、缺字段等无法核查的条目，附原因原文，
  不影响其余句子出结论。
* `failures` + `status: "incomplete"`：单句处理异常被隔离，已完成句子结果保持可用。

CLI 退出码：`0` 完整；`2` 存在单句失败（报告仍已落盘）；`1` 输入文件/格式错误。

## 4. 确定性业务样例

样本由 `src/subaudit/samples.py` 以固定参数合成（44.1kHz、PCM_16 WAV、固定相位正弦波、
区间外纯零静音；PCM_16 仅含标准 fmt+data 块，规避 libsndfile PEAK 块的非确定字节），
无随机源，重复生成 SHA256 一致：

| 场景 | WAV 内容 | 清单特点 | 预期报告 |
| --- | --- | --- | --- |
| `normal` | 1–2s / 3–4s / 5–6s 三句 | 字幕均完整覆盖 | 3 句 `ok`，偏差 −200/+200 等 |
| `early_end` | 同上 | S2 字幕 3.5s 提前结束 | S2 进入 `flagged`，标签 `voice_trailing_subtitle`，终点偏差 −500 |
| `invalid` | 同上 | S2 起止倒置（4100→2900） | S2 进入 `invalid_entries`，S1/S3 结论完好 |
| `silent` | 4s 纯静音 | 两句清单 | 两句均 `no_voice_detected`，语音起止为 `null` |

单句失败演练（需求 8）：

```powershell
$env:SUBAUDIT_FORCE_FAIL_SENTENCE = "S2"
python -m subaudit.cli audit .\samples\normal.wav .\samples\normal.json -o fail.json
# status=incomplete，failures 含 S2，S1/S3 结果完整；进程退出码 2
Remove-Item Env:\SUBAUDIT_FORCE_FAIL_SENTENCE
```

## 5. VAD 工作原理（`src/subaudit/vad.py`）

1. 转单声道 float64，整数 PCM 按位深归一化到 [-1, 1]；
2. 20ms 帧 / 10ms 跳移计算 RMS 能量，阈值取「噪声底 20 分位 × 4」与绝对下限 0.01 的较大值；
3. 帧掩码聚合成候选段，在粗边界 ±100ms 内做样本级幅度精修，边界精确到单个采样点；
4. 桥接 ≤120ms 的短间隙（塞音/停顿），丢弃 <60ms 的碎片；
5. 每句字幕按「重叠最大优先、否则最近邻」匹配发声段，并防止同段被相邻句重复认领。

## 6. 目录结构

```
src/subaudit/
  vad.py       # 离线确定性能量 VAD
  audit.py     # 清单校验、句-段匹配、偏差与结论、单句隔离
  samples.py   # 确定性样本 WAV/JSON 构造
  cli.py       # audit / samples 两个子命令
tests/test_audit.py  # 需求 1-8 的 10 项验收测试
```
