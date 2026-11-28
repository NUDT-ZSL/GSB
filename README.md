# 音频档案单声道录音劣化评估

本项目为历史磁带/黑胶转录的单声道音乐录音提供确定性、可逐段核对的修复前评估：

- 按录音段分帧，逐帧输出峰值、平顶持续样本数、均值直流、低能量分位数噪声底、一阶差分能量突跳。
- 分别检测硬削波/轻度限幅、直流偏移、底噪抬升、脉冲爆音，并输出采样点区间。
- 每种劣化按互不覆盖的阈值分为轻/中/重，汇总录音级等级并映射到三种修复流程。
- 用生成器独立嵌入的受损区间与逐帧噪声底真值逐段核对，真值缺失段只报告检测结果。
- 自动用两组合法帧长参数评估，保留两套完整结果；不一致段列入人工复核，不静默覆盖。

运行只读取本地清单与 WAV，不访问网络。

## 环境与安装

需要 Python 3.10+ 与 NumPy：

```bash
python3 -m pip install -r requirements.txt
```

本次验证环境为 Python 3.14、NumPy 2.5。

## 快速开始

```bash
# 1. 固定种子生成分钟级单声道样本、受损区间真值与逐帧噪声底真值
python3 generate_dataset.py --out data --seed 20261128

# 2. 主帧长 512/帧移 128，并自动用 1024/256 复评
python3 assess.py \
  --manifest data/manifest.json \
  --frame-length 512 --hop 128 \
  --compare-frame-length 1024 --compare-hop 256 \
  --json-out artifacts/assessment.json \
  --report artifacts/assessment_report.md \
  --frames-dir artifacts
```

帧长/帧移既可写样本数（`512`），也可写时间（`0.032s`、`32ms`）。若换算不是整数样本，会在参数元数据中标记 `fractional=true`，并采用四舍五入后的整数样本数。

默认会写出：

- `artifacts/assessment.json`：完整帧级指标、区间、分级、流程、真值核对、双帧长对照、验收清单。
- `artifacts/assessment_report.md`：可直接引用到修复工单的分段报告。
- `artifacts/frames_primary.csv`、`artifacts/frames_comparison.csv`：两套帧参数的逐帧明细。

可用 `--no-frames-csv` 关闭 CSV。验收通过时进程退出码为 0，否则为 1。

## 数据集

`generate_dataset.py` 使用固定种子 `20261128` 生成 7 条 18 秒单声道 WAV（合计 126 秒，16 kHz/16 bit），每条含 3 个 6 秒录音段：

| 录音 | 内容 |
|---|---|
| `tape_A_hard_clip` | 硬削波轻/中/重 |
| `vinyl_B_soft_limit` | 轻度限幅轻/中/重 |
| `tape_C_dc_offset` | 直流偏移轻/中/重 |
| `tape_D_noise_floor` | 底噪抬升轻/中/重 |
| `vinyl_E_impulse` | 脉冲爆音轻/中/重 |
| `archive_F_mixed` | 清洁段、四类混合劣化段、真值缺失段 |
| `preview_G_light` | 清洁、轻度底噪、轻度直流 |

清单 `data/manifest.json` 的关键结构：

```json
{
  "seed": 20261128,
  "sample_rate": 16000,
  "truth_frame_length": 256,
  "truth_hop": 64,
  "default_frame_length": 512,
  "default_hop": 128,
  "alternate_frame_length": 1024,
  "alternate_hop": 256,
  "recordings": [
    {
      "recording_id": "tape_A_hard_clip",
      "path": "tape_A_hard_clip.wav",
      "segments": [
        {
          "segment_id": "tape_A_hard_clip/seg_00",
          "start_sample": 0,
          "end_sample": 96000,
          "truth_available": true,
          "ground_truth": {
            "damages": {
              "clipping": [{"start_sample": 36480, "end_sample": 38016, "subtype": "hard", "grade": "light"}],
              "dc_offset": [],
              "noise": [],
              "impulse": []
            },
            "segment_grades": {"clipping": "light", "dc_offset": null, "noise": null, "impulse": null},
            "overall_grade": "light",
            "workflow": {"code": "light_denoise"},
            "noise_floor": {
              "frame_length": 256,
              "hop": 64,
              "frame_starts": [0, 64, 128],
              "dbfs": [-72.1, -72.0, -72.2]
            }
          }
        }
      ]
    }
  ]
}
```

`archive_F_mixed/seg_02` 故意设置 `truth_available=false` 且 `ground_truth=null`；评估器仍检测四类劣化，但报告标注“无法核对”。

## 处理流程

### 1. 分帧

- 每条录音按其 `segments` 分段处理，帧只在本段内生成，绝不跨段拼接。
- 帧起点为段内 `0, hop, 2*hop, ...`；每帧长度取 `min(frame_length, 段尾-帧起点)`。
- 段尾长度不足的帧全部标记 `complete=false`，输出实际样本数。由于帧长大于帧移，段尾可能出现连续多个不完整帧，报告中给出不完整帧总数，CSV/JSON 可逐帧核对。
- 段起点/终点与所有帧起点/终点统一使用录音内全局采样点。

### 2. 逐帧指标

每个帧输出：

- `peak_ratio`、`peak_dbfs`：绝对峰值与满量程比例。
- `plateau_samples`：`|x| >= 0.995` 的最长连续样本数。
- `near_plateau_samples`：`|x| >= 0.98 * frame_peak` 的最长连续样本数。
- `dc_offset`：帧均值直流。
- `low_quantile_dbfs`：去直流后，以固定 2 ms 子块 RMS 的 10% 分位数作为低能量分位观测。
- `noise_floor_dbfs`：自适应噪声底轨迹。
- `diff_jump_ratio`、`diff_peak_amplitude`、`jump_duration_samples`：一阶差分能量突跳及局部包络归一化结果。
- `clipping_label`、`impulse_candidate`、`impulse_flatness`：削波/爆音帧标签与频谱平坦度辅助证据。

### 3. 削波与轻度限幅

- 硬削波帧：最长平顶 `>= max(2, 0.125 ms)` 样本且峰值比例 `>= 0.995`。
- 轻度限幅帧：峰值比例在 `[0.90, 0.995)`，近峰值持续 `>= 0.35 ms` 且近峰值样本占比 `>= 0.5%`。
- 连续削波帧（允许 1 帧空隙、至少 2 帧）聚合为段；输出起止采样点、子类型、段覆盖率和帧内削波比例。
- 段覆盖率定义为 `削波帧数 × 帧移 / 段时长`，避免因帧长变化直接改变分级。削波分级：
  - 轻：`[0, 2%)`
  - 中：`[2%, 15%)`
  - 重：`[15%, +inf)`

### 4. 直流偏移

- 帧均值为直流观测，检测门槛 `0.015`。
- 削波/限幅帧及其前后各 2 帧不更新直流估计，而是保持上一个有效值，防止平顶/限幅过渡造成假直流。
- 连续至少 5 帧聚合为 DC 段，分级用段内中位绝对直流：
  - 轻：`[0.015, 0.04)`
  - 中：`[0.04, 0.12)`
  - 重：`[0.12, +inf)`

### 5. 自适应噪声底

- 每帧先得到低能量分位观测；有效更新条件为：该帧完整、非削波/限幅、非脉冲爆音。
- 用最近 0.5 秒有效观测的 5% 分位数作为稳健原始噪声底，再做指数平滑。
- 削波/爆音帧只保持上一噪声底，不写入历史；因此平顶和爆音不会把底噪估计拉高。
- 噪声段检测：噪声底高于段基线 6 dB，或全段中位底噪高于 `-66 dBFS` 的兜底路径；合并 100 ms 内空隙，最短 800 ms（底噪估计本身是慢变量，短于 800 ms 的底噪抬升不单独成段）。
- 噪声分级用段内中位噪声底：
  - 轻：`(-66, -56) dBFS`
  - 中：`[-56, -46) dBFS`
  - 重：`[-46, 0] dBFS`

### 6. 脉冲爆音

- 对一阶差分平方能量做 0.25 ms 平滑。
- 对每个候选峰，取峰两侧（各约 12 ms，避开 1.5 ms 中心区）80% 分位能量作为稳健局部包络；取两侧较大值，确保稳态高能量乐音不会仅因一侧安静而被误判。
- 突跳比为 `sqrt(候选能量 / 局部包络)`；同时计算持续样本数、峰值幅度和 4 ms 窗频谱平坦度。
- 接受条件（满足其一）：
  - `突跳比 >= 4.5` 且频谱平坦度 `>= 0.30`；
  - 绝对突跳幅度 `>= 0.09` 且频谱平坦度 `>= 0.45`。
- 事件持续需在 `0.3–12 ms`，削波饱和样本附近不参与检测。频谱平坦度用于排除削波/限幅产生的周期性高幅度乐音。
- 输出每个爆音的起止采样点、原始峰值幅度、局部包络归一化突跳比、突跳幅度、持续时间和频谱平坦度。
- 段内爆音分级同时看密度和峰值，取较严者：
  - 轻：密度 `< 0.4/s` 且峰值 `< 0.30`
  - 中：密度 `[0.4, 2.0)/s` 或峰值 `[0.30, 0.60)`
  - 重：密度 `>= 2.0/s` 或峰值 `>= 0.60`

### 7. 录音级等级与修复流程

- 录音段等级：任一类型重度即重；否则两类及以上中度并存在即重；否则有中度取中；否则有轻度取轻；全无则为“无”。
- 录音级等级：对同一录音各段类型等级取最严后再按上述规则汇总。
- 流程映射：
  - `remaster_retranscribe`（母带重新转录）：存在重度类型，或段级达到“两类及以上中度并存”。
  - `declick_repair`（去脉冲修复）：未达重转录条件，但检出脉冲爆音；若同时有底噪抬升，报告建议先去脉冲前做轻度降噪。
  - `light_denoise`（轻度降噪）：仅有轻/中度非脉冲劣化，报告同时提示直流基线校正与削波修复。
  - `archive_only`（无需修复）：四类均未检出。

所有映射依据（类型、分级、触发规则）都写入 JSON 的 `workflow.rationale` 和 Markdown 报告。

### 8. 人工复核临界带

对以下分级阈值设置临界带，命中即列出触发条件：

- 削波覆盖率：`0.02 ± 0.003`、`0.15 ± 0.015`
- 直流：`0.04 ± 0.004`、`0.12 ± 0.0144`
- 噪声底：`-56 ± 2 dBFS`、`-46 ± 2 dBFS`
- 爆音密度：`0.4 ± 0.08/s`、`2.0 ± 0.30/s`
- 爆音峰值：`0.30 ± 0.035`、`0.60 ± 0.06`

Markdown 报告在段内列出“人工复核（分级阈值临界带）”，JSON 为 `manual_review[]`。

### 9. 真值核对

对每个 `truth_available=true` 段：

- 按类型对真值/检测区间做一对一匹配，统计漏报、误报、起止偏差与 IoU。
- 默认段级边界容许偏差：削波 80 ms、直流 80 ms、底噪 600 ms、脉冲 20 ms。
- 比较四类段级分级、录音段整体等级、建议流程。
- 将 256/64 的逐帧噪声底真值按分析帧覆盖范围重采样到分析帧，统计 MAE、RMSE、最大误差、6 dB 内比例，并同时报告“全帧 RMS 直接估计”的 MAE，以展示自适应轨迹相对朴素全帧 RMS 的改善。
- 一致性门槛：噪声 MAE `<= 4 dB` 且 6 dB 内帧比例 `>= 80%`，并且无区间漏报/误报/边界超差、无分级/流程不一致。
- 首个不一致段会输出问题类型、锚点采样点及锚点附近逐帧证据（峰值、平顶、直流、噪声底、真值噪声、误差、突跳比、平坦度、标签）。

`truth_available=false` 段不参与上述统计，只输出检测结果并标注无法核对。

### 10. 双帧长稳定性

- 主参数与对照参数各自完整评估，两套结果都写入 JSON；不存在“选一套覆盖另一套”的路径。
- 按类型对两套区间做一对一匹配；判定稳定需同时满足：
  - 区间数量一致、无未匹配区间；
  - 起止偏差在段级容许偏差内（默认同上：80/80/600/20 ms）；
  - 四类分级、录音段整体等级、建议流程一致。
- 任一不一致都会输出两套参数各自区间/等级/流程差异，并列出“建议复核段落”。

## 输出字段

`assessment.json` 顶层：

- `primary`：主帧参数完整结果。
- `comparison`：对照帧参数完整结果。
- `dual_frame_comparison`：逐段稳定性、最大偏差、复核段列表。
- `acceptance`：验收检查、逐类型真值统计、噪声底指标。
- `grade_thresholds`：清单内嵌分级阈值。
- `io_policy`：输入文件与“不访问网络”声明。

每个录音段包含：

- `frames[]`：逐帧指标。
- `damage_intervals`：`clipping`、`dc_offset`、`noise`、`impulse` 四类区间。
- `segment_grades`、`overall_grade`、`workflow`。
- `manual_review[]`。
- `verification`：真值核对或无法核对说明。

逐帧 CSV 字段与 JSON 一一对应，便于外部表格复核。

所有受损区间均为半开采样点区间 `[start_sample, end_sample)`；`end_sample` 不属于受损区间。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖：

- 分帧不跨段、段尾不完整帧标记；
- 分级半开区间与临界带；
- 清洁乐音不误报爆音、叠加脉冲可检出；
- 固定种子生成结果字节级确定；
- 端到端真值核对、自适应噪声底与双帧长验收。

## 文件

```text
generate_dataset.py        确定性数据生成入口
assess.py                  评估入口
audio_qa/generator.py      音乐合成与劣化/真值生成
audio_qa/analysis.py       分帧、检测、分级、核对编排
audio_qa/metrics.py        逐帧指标
audio_qa/impulses.py       脉冲爆音检测
audio_qa/noise.py          自适应噪声底与噪声段
audio_qa/aggregation.py    削波/直流区间聚合
audio_qa/grading.py        分级阈值、录音级汇总、临界带
audio_qa/workflow.py       修复流程映射
audio_qa/verification.py   独立真值核对
audio_qa/dual.py           双帧长对照
audio_qa/acceptance.py     验收清单
audio_qa/report.py         Markdown 工单报告
tests/                     单元与端到端测试
```
