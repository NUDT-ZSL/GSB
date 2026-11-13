# WAV 时间线拼接工具（纯 Python 标准库）

把多段 **16-bit / 44.1kHz / 单声道 PCM WAV** 按 JSON 时间线拼成一条音轨，
相邻片段重叠区做平滑线性交叉淡化，并产出可播放 WAV 与面向业务人员的 JSON 报告。

- Python 3.10+，**无第三方依赖**（仅用 `wave / json / array / argparse` 等标准库）
- 样本由 `make_samples.py` 以正弦公式确定性构造，无需任何外部素材
- 不使用 Docker / 容器，全部为本地文件操作

## 目录结构

```
audio_joiner/
├── joiner.py          # 主程序：校验 -> 逐段读取/跳过 -> 混音淡化 -> WAV + 报告
├── make_samples.py    # 确定性生成测试 WAV 与全部场景 JSON
├── verify.py          # 14 条验收点的自动化验证（样本级断言）
├── demo_workspace/    # 运行示例后生成（已 gitignore）
└── README.md
```

## 一、本地启动方式（可复现）

```powershell
# 1) 生成确定性样本与场景（默认生成到 ./demo_workspace）
python -I make_samples.py demo_workspace

# 2) 进入工作区，不带参数运行（使用默认 edit.json / output.wav / report.json）
cd demo_workspace
python -I ..\joiner.py

# 3) 也可显式指定三个参数：JSON 路径、输出 WAV 路径、报告路径
python -I ..\joiner.py edit_mixed.json mixed.wav mixed_report.json
```

> `-I` 为隔离模式（隔离模式，忽略环境变量与当前目录注入模块，防止同名 `json.py` 之类的植入风险）。
> JSON 内片段路径为相对路径时，相对 **JSON 所在目录** 解析；绝对路径按原样使用。

### 命令行参数

| 位置 | 含义 | 缺省值 |
|---|---|---|
| 第 1 个 | 时间线 JSON 路径 | `./edit.json` |
| 第 2 个 | 输出 WAV 路径 | `./output.wav` |
| 第 3 个 | 输出报告 JSON 路径 | `./report.json` |

### 时间线 JSON 格式

```json
{
  "clips": [
    {"file": "wav/clip_a.wav", "start": 0.0, "fade": 0.0},
    {"file": "wav/clip_b.wav", "start": 0.8, "fade": 0.2}
  ]
}
```

- `file`：片段 WAV 路径（相对 JSON 目录或绝对路径）
- `start`：起始秒，非负数，且各片段**严格递增**
- `fade`：请求淡化秒，非负数；仅在与前一有效片段实际重叠时生效

## 二、业务操作样例

`make_samples.py` 一次生成以下场景，业务人员可直接演示：

| 场景文件 | 演示内容 |
|---|---|
| `edit.json` | 两段 1 秒片段、淡化 0.2 秒 → 1.800 秒成品（默认场景） |
| `edit_fade_too_big.json` | 请求淡化 0.8 秒但重叠只有 0.2 秒 → 该处不淡化，报告 `note` 告警 |
| `edit_bad_format.json` | 中间是 48kHz 立体声 WAV → 跳过并写明“格式不匹配”，其余正常 |
| `edit_missing.json` / `edit_middle_missing.json` | 片段路径不存在 → 跳过，剩余片段按原起始秒定位，报告列路径与原因 |
| `edit_all_missing.json` | 全部缺失 → 不生成 WAV，报告 `no_usable_clip=true` |
| `edit_bad_timeline.json` | 起点 1.0 后又出现 0.5 → 报错退出码 2，**不生成任何文件** |
| `edit_missing_field.json` / `edit_no_clips.json` | 缺字段 / 缺顶层列表 → 报具体字段错误并终止 |
| `edit_same_start.json` | 第二段与首段起点相同 → 该段跳过（非整体拒绝） |
| `edit_first_offset.json` | 首片段起点 3.0 秒 → 不补静音，成品从该起点开始计时 |
| `edit_mixed.json` | 综合场景：成功 + 格式跳过 + 缺失跳过 + 淡化告警 |

控制台每行一个片段结果，业务人员可按前缀识别：

```
[成功] 片段1 ...            正常合成
[跳过] 片段2 ... —— 原因     该段未参与合成
[警告] 片段4 ... —— 说明     参与合成但有需关注事项（如淡化未生效）
[错误] ...                  时间线/结构非法，整体拒绝（stderr，退出码 2）
```

## 三、报告字段说明（report.json）

```json
{
  "total_duration_seconds": "1.800",   // 实际合成秒数，字符串，固定三位小数
  "total_frames": 79380,               // 实际写入样本数（时长由它决定）
  "sample_rate": 44100,
  "channels": 1,
  "sample_width_bits": 16,
  "no_usable_clip": false,             // true 表示无可用片段、未生成 WAV
  "clips": [
    {
      "index": 1,
      "file": "wav/clip_a.wav",
      "start_seconds": 0.0,
      "requested_fade_seconds": 0.0,   // 请求淡化
      "applied_fade_seconds": 0.2,     // 实际淡化（0 表示未淡化）
      "status": "成功",                // 仅允许 “成功” / “跳过”
      "reason": "",                    // 跳过原因（成功时为空）
      "note": "",                      // 业务提示，如淡化超范围未生效
      "frames": 44100
    }
  ]
}
```

## 四、关键处理规则

1. **格式门禁**：仅接受单声道 / 16-bit PCM / 44100Hz；不符即跳过，
   原因逐项列出（如“声道数=2，采样率=48000Hz”）。
2. **时间线定位**：以第一个有效片段的起点为输出 0 点（首片段非 0 起点不补静音）；
   各片段按原起始秒换算成样本位置摆放，混音缓冲只由实际样本写入构成，
   不会为被跳过的片段主动插入静音块。
3. **交叉淡化**：淡化样本数 = `round(fade × 44100)`；当前片段在重叠区线性淡入
   `k=(j+1)/(f+1)`，前一片段同区对称淡出，两端斜率连续、无突变尖峰/爆音；
   非淡化区增益恒为 1，样本与源片段逐样本一致。
4. **淡化合理性**：请求淡化样本数大于实际重叠（或无重叠）时该处**不做淡化**，
   报告 `note` 写明请求值与可承受范围；淡化为 0 表示不请求淡化，不告警。
5. **混音精度**：64-bit 整数累加，写盘前夹到 [-32768, 32767]，避免中间溢出。
6. **失败边界**：
   - 结构/时间线非法（缺字段、负起点、非严格递增、JSON 损坏）→ 退出码 2，不产生任何文件；
   - 同起点非首片段 → 仅该片段跳过（退出码仍为 0）；
   - 全部片段跳过 → 只写报告、不写 WAV，`no_usable_clip=true`。

## 五、自动化验收

```powershell
python -I verify.py
```

该脚本在系统临时目录建一次性工作区，以子进程方式跑全部场景，并做 WAV 头字段、
报告字段、样本级一致性（非淡化区逐样本等于源、淡化区边界无额外跳变、
无淡化时重叠区等于原样相加）等断言，结束后自动清理。

### 实际验证结果（本机 Python 3.10.20，Windows 11）

```
[PASS] 验收1  默认场景生成 WAV 与报告，片段数一致
[PASS] 验收2  格式不符片段跳过且写明原因，其余正常合成
[PASS] 验收3  缺失片段跳过，报告含该路径及原因，其余继续合成
[PASS] 验收4  淡化超出范围时不做淡化且报告可识别
[PASS] 验收5  淡化0.2s合成1.8s、重叠区平滑、非淡化区与源一致
              （帧数=79380；起点边界跳变=376 与源自然步长376一致；
               终点边界跳变=564 与源自然步长564一致，无额外突变）
[PASS] 验收6  起始秒非严格递增时报错退出（退出码2）且不生成文件
[PASS] 验收7  全部跳过时无 WAV，报告 no_usable_clip=true 并列出各段原因
[PASS] 验收8  缺省参数写默认文件，带参数写指定路径
[PASS] 验收9  报告合法 JSON，状态仅成功/跳过，total_duration_seconds="1.800"
[PASS] 验收10 中间缺失时剩余片段按原起始秒定位（132300 帧=2.0s+1.0s），报告标明缺失段
[PASS] 验收11 缺字段或顶层缺列表时报具体错误且不生成输出
[PASS] 验收12 控制台逐片段输出结果并显示最终输出路径
[PASS] 验收13 输出 WAV 为单声道/16-bit/44100Hz，样本数与报告时长一致
[PASS] 验收14 同起点片段跳过；首片段非0起点不补静音、从该起点计时
================ 验收汇总 ================
通过 14/14
```

另用标准库独立核验成品：`channels=1 width=16bit rate=44100 frames=79380 dur=1.800s`，
与报告 `total_frames=79380 / total_duration_seconds="1.800"` 完全一致。
