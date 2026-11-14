# 战斗音效素材核对工具

本地命令行工具：读取素材清单 JSON，逐条核对 WAV 的**实际时长**与**峰值电平**，
对峰值偏差超标的素材生成**等时长、等波形形状**的调整试听 WAV（纯线性增益，不削波），
并输出逐条核对报告 JSON，供音频人员定位返工范围。全部在本地完成，不依赖容器与网络。

## 环境要求

- Python 3.10+（本机验证：3.10.20）
- numpy（本机验证：2.2.6），安装：`pip install numpy`

## 启动说明

```powershell
# 1. 生成演示素材与清单（确定性，重复生成结果一致）
python make_samples.py

# 2. 运行核对（输出目录不存在时自动创建）
python audio_check.py --manifest samples/manifest.json `
    --audio-out out/run1/audio `
    --report out/run1/report.json

# 可选：调整峰值允许偏差（线性，默认 0.01）
python audio_check.py --manifest samples/manifest.json `
    --audio-out out/audio --report out/report.json --peak-tolerance 0.02
```

参数：

| 参数 | 说明 |
|---|---|
| `--manifest` | 素材清单 JSON 路径（清单内 WAV 相对路径按清单所在目录解析） |
| `--audio-out` | 试听 WAV 输出目录，不存在时自动创建（含多级目录） |
| `--report` | 核对报告 JSON 输出路径 |
| `--peak-tolerance` | 峰值允许偏差，线性值，默认 0.01 |

退出码：`0` 正常完成（含「需复核」条目）；`2` 清单级致命错误（不生成任何文件）。

## 清单格式

顶层为数组，每条必需字段：

```json
[
  {"id": "hit_quiet", "path": "hit_quiet.wav", "target_peak": 0.8,
   "duration_min_ms": 400, "duration_max_ms": 600}
]
```

- `target_peak`：目标峰值电平，线性值，合法区间 `(0, 1]`（1.0 为满量程）
- `duration_min_ms` / `duration_max_ms`：允许时长范围（毫秒），单条上限 30000ms

## 报告格式

顶部汇总 + 按清单顺序的逐条结果，`conclusion` 仅取 `可用` / `需复核`：

```json
{
  "summary": {"total": 3, "usable": 1, "needs_review": 2},
  "items": [
    {"id": "...", "path": "...", "actual_duration_ms": 500.0,
     "actual_peak": 0.200012, "target_peak": 0.8, "adjusted_peak": 0.799988,
     "conclusion": "需复核", "reason": "..."}
  ]
}
```

- `adjusted_peak`：可用条目等于实际峰值；已生成试听的条目为调整后实测峰值；不可核对条目为 `null`
- 报告为带 BOM 的 UTF-8，Windows 编辑器可直接打开；内容不含时间戳，重复运行字节一致

## 判定规则

1. 清单 JSON 解析失败 / 缺必需字段 / 字段类型错误 → 打印含出错位置（行、列、字段名、条目 id）的说明，退出码 2，**不生成任何文件**
2. 目标电平越界、时长下限为负、范围起止颠倒、范围上限超 30000ms → 该条「需复核」并注明不可核对原因
3. WAV 缺失 / 文件头损坏 → 该条「需复核」注明原因，**不阻断**其余素材
4. 实际时长超 30000ms 或不在允许范围 → 「需复核」
5. 清单内采样率/声道数不一致 → 报告说明具体差异（本条 vs 基准），**本次不生成任何试听文件**
6. 峰值偏差 ≤ 容差 → 「可用」，不生成试听文件
7. 峰值偏差 > 容差 → 「需复核」，按 `增益 = 目标/实际` 线性缩放生成 `{id}_adjusted.wav`：帧数不变（时长一致）、波形形状不变、采样绝对值不超过满量程（不削波）
8. 空清单 → 打印说明，不生成试听文件

## 业务样例与实际运行结果

`samples/` 下由 `make_samples.py` 确定性生成：5 个 WAV（48kHz 立体声 500ms 峰值 0.5 /
500ms 峰值 0.2 / 600ms 峰值 0.5 / 单声道 500ms / 8kHz 单声道 30500ms）、1 个损坏 WAV、
9 份场景清单。以下为本机实际运行输出。

### 主场景（对应需求 1、5、6、7、9、12、14、15）

```
python audio_check.py --manifest samples/manifest.json --audio-out out/run1/audio --report out/run1/report.json
```

```
  [可用] hit_normal: 峰值偏差 0.0 在允许范围 ±0.01 内，无需调整
  [需复核] hit_quiet: 峰值偏差超出允许范围（实际 0.200012，目标 0.8，容差 ±0.01）；已生成试听文件: hit_quiet_adjusted.wav
  [需复核] hit_long: 实际时长 600.0ms 不在允许范围 [400, 500]ms 内
核对完成：共 3 条，可用 1 条，需复核 2 条。
```

报告三条字段齐全、按清单顺序排列、顶部汇总 `usable=1 / needs_review=2` 与逐条结论一致；
`hit_normal` 可用且 `out/run1/audio/` 下无其试听文件。

试听文件实测（需求 7）：`hit_quiet_adjusted.wav` 帧数 24000 与原文件一致（时长 500.0ms 不变），
峰值 0.799988 ≈ 目标 0.8，逐采样增益比值恒定 ≈ 4.0（波形形状不变）。

### 文件缺失 / 损坏（需求 2）

```
python audio_check.py --manifest samples/manifest_missing.json --audio-out out/missing/audio --report out/missing/report.json
```

```
  [可用] hit_normal: ...
  [需复核] ghost: WAV 文件缺失: ...\samples\ghost_deleted.wav
  [需复核] corrupt: WAV 文件头损坏或无法解析: ...\samples\corrupt.wav（file does not start with RIFF id）
  [需复核] hit_quiet: ...已生成试听文件
```

问题素材 id（ghost、corrupt）列入报告，其余两条正常核对。

### 采样率/声道数不一致（需求 3）

```
python audio_check.py --manifest samples/manifest_mono.json --audio-out out/mono/audio --report out/mono/report.json
```

```
[说明] 清单内素材格式不一致（基准 48000Hz/2声道），本次不生成试听文件。
  [需复核] hit_mono: 采样率/声道数与清单基准不一致（本条 48000Hz/1声道，基准 48000Hz/2声道）
```

`out/mono/audio/` 为空，未产生新音频文件。

### 非法取值（需求 4）

```
python audio_check.py --manifest samples/manifest_invalid.json --audio-out out/invalid/audio --report out/invalid/report.json
```

```
  [需复核] bad_target: 目标峰值电平越界（-0.5），须在 (0, 1] 区间内，该条不可核对
  [需复核] bad_range: 允许时长范围起止颠倒（下限 500ms 大于上限 400ms），该条不可核对
```

### 满量程目标不削波（需求 8）

```
python audio_check.py --manifest samples/manifest_fullscale.json --audio-out out/fullscale/audio --report out/fullscale/report.json
```

目标峰值 1.0 时输出试听实测：最大采样绝对值 = 32767，不超过满量程，峰值 0.999969。

### 空清单 / 超长素材（需求 10）

```
python audio_check.py --manifest samples/manifest_empty.json ...        # [说明] 清单条数为 0，无素材需要核对
python audio_check.py --manifest samples/manifest_toolong.json ...      # 允许时长上限 40000ms 超过 30000ms 核对范围
python audio_check.py --manifest samples/manifest_toolong_actual.json ... # 实际时长 30500.0ms 超过 30000ms 核对范围
```

三种情况均给出明确说明，且不生成任何 WAV。

### 重复运行确定性（需求 11）

同一清单连续运行两次，`report.json` 与试听 WAV 的 SHA-256 哈希分别完全相同
（报告不含时间戳，增益为纯确定性浮点运算）。

### 清单解析失败 / 缺字段（需求 13）

```
python audio_check.py --manifest samples/manifest_missing_field.json ...
# [错误] 清单第 1 条缺少必需字段 'target_peak'（id=no_target），退出码 2
python audio_check.py --manifest samples/manifest_broken.json ...
# [错误] 清单 JSON 解析失败: 第 2 行第 20 列: Expecting property name ...，退出码 2
```

失败时不创建输出目录、不生成任何半截文件。

### 输出目录自动创建（需求 14）

```
python audio_check.py --manifest samples/manifest.json --audio-out out/deep/nested/dir/audio --report out/deep/nested/dir/report.json
```

多级不存在目录自动创建，报告与试听文件落于指定目录内。

## 文件说明

| 文件 | 说明 |
|---|---|
| `audio_check.py` | 核对工具本体 |
| `make_samples.py` | 确定性样例生成脚本（无随机源） |
| `samples/` | 生成的 WAV 素材与 9 份场景清单 |
| `out/` | 各场景运行输出（报告与试听文件） |
