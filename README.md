# AED Scale：面向变规模数据的确定性音频事件检测组件

本项目从零实现了一个可集成的帧级音频事件检测（AED）领域组件，并附带本地确定性合成数据、完整 CLI 示例、一致性自检和自动化测试。核心问题是：当对话数、总帧数、候选密度、说话人数与特征维度变化时，在精细路径与逼近路径之间做出可复现、可解释的算法取舍。

所有音频、逐帧真值、模板和指标都由固定种子在本地生成；不依赖外部音频、模型权重、网络服务或容器环境。

## 快速开始

环境：Python >= 3.10，NumPy >= 1.24。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# 可选：安装命令行入口 aed-scale
pip install -e .

# 运行 23 项测试
python3 -m unittest discover -s tests -v

# 生成完整小/大规模示例、执行检测并写出报告
python3 -m aed_scale demo --out artifacts/demo --seed 20261121
```

主要输出：

- `artifacts/demo/report.json`：完整报告，含逐帧判决、候选事件、逐事件命中明细、混淆统计、路径决策、业务码、降级标记和自检结果。
- `artifacts/demo/summary.json`：便于快速查看的指标摘要。
- `artifacts/demo/datasets/{small,large}`：WAV、逐帧真值 JSONL、对话清单 JSONL 和合成元数据。

单独运行已有数据集：

```bash
python3 -m aed_scale run \
  --dataset artifacts/demo/datasets/small \
  --report artifacts/demo/small_report.json \
  --max-resident-mb 0.94 \
  --max-candidate-comparisons 5000 \
  --max-fine-frames 1000 \
  --min-recall-gain 0.02
```

## 目录

```text
aed_scale/
  synthesis.py     确定性多说话人、事件模板与真值合成
  features.py      20ms 帧 / 10ms 帧移的声学特征与逐字节序列化
  detection.py     模板原型、背景模型、逐帧评分、分类、候选分组
  postprocess.py   最短时长/间隔合并、完整时间线、区间校验
  adaptive.py      规模观测、资源估算、显式优先级裁决
  metrics.py       帧级/事件级指标、混淆统计与明细重放
  consistency.py   跨阶段自检与 E_INCONSISTENT
  pipeline.py      数据集端到端执行
  cli.py           generate/run/demo 命令行
  validation.py    对话时间轴与增益校验
tests/             23 项 unittest
```

## 数据契约

### 对话清单 `manifest.jsonl`

每行至少包含：

```json
{"dialog_id":"dialog_000","sample_rate":16000,"duration_ms":2200,"modality_mask":["audio"]}
```

示例生成器额外写入 `wav_path`、`truth_path`、`speaker_count`、`seed`、`frame_ms`、`hop_ms`。

### 逐帧真值 `frames.jsonl`

每行字段：

```json
{"frame_index":0,"event_id":null,"event_type":"background","speaker_id":null,"is_speech":false}
```

受控事件类型及稳定编号：

| 编号 | 类型 | 是否可作候选 |
|---:|---|---|
| 0 | `background` | 否，仅作背景/填充 |
| 1 | `cough` | 是 |
| 2 | `keyboard` | 是 |
| 3 | `knock` | 是 |
| 4 | `laughter` | 是 |

同分时按事件类型编号升序；类型内再按帧索引升序。帧真值按帧单元与事件时间段的交叠确定。

### 规范帧格

- 采样率固定 `16000 Hz`；默认帧长 `20 ms`（320 样本），帧移 `10 ms`（160 样本）。
- 第 `i` 帧从样本 `i * hop_samples` 开始，窗口长 `frame_samples`；最后一个窗口越界时做确定性零填充。
- 帧数定义为 `ceil(pcm_samples / hop_samples)`，因此所有 PCM 样本都落入一个帧单元，最后一帧可能不足一个帧移。
- 2.2 秒音频对应 220 帧；3.0 秒对应 300 帧。
- `FeatureResult.canonical_bytes()` 使用固定头、长度和 little-endian float64 矩阵序列；相同 `dialog_id`、配置与输入逐字节一致。

### 特征

精细路径使用 10 维：

`log_energy, zcr, spectral_centroid_hz, spectral_rolloff_hz, spectral_bandwidth_hz, spectral_flatness, spectral_flux, peak_abs, low_band_ratio, high_band_ratio`

逼近路径保留核心 4 维：

`log_energy, zcr, spectral_centroid_hz, spectral_rolloff_hz`

谱特征使用 Hann 窗与 `n_fft=512`，滚降比默认 `0.85`。所有随机性只存在于数据合成；特征计算无随机性。

## 检测与后处理

### 模板检测与分类

1. 为四类事件生成固定种子的多组确定性波形模板。
2. 用相同帧配置提取模板帧，去除低能帧后取每类标准化特征中位数中心。
3. 用确定性说话人模板与静音建立 `background` 中心，避免把语音或静音误判为事件。
4. 每帧计算到五个类别中心的归一化欧氏距离，得到 `exp(-distance/0.8)` 事件得分。
5. 仅在事件类别优于背景类别且达到路径阈值时输出候选；分类平局由事件类型编号升序打破。
6. 连续同类帧合并为候选事件，输出 `start_frame/end_frame/event_type/confidence`。

所有模板均由本地代码生成，没有外部模型权重。

### 完整时间线

后处理先按类型合并间隔不超过 `min_gap_ms` 的候选，再执行最短事件时长过滤，最后将所有帧划分为事件段或 `background` 填充段。因此：

- `segments` 是首尾相接、覆盖 `[0, frame_count)` 每条检测帧的完整时间线。
- `events` 只保留活动事件，按时间升序、互不重叠，并落在音频时长内。
- 帧时间用样本精确边界换算为毫秒，末段按真实音频样本数裁剪。

### 两条路径

| 项目 | 精细 `fine` | 逼近 `approximate` |
|---|---|---|
| 特征 | 10 维 | 4 维核心特征 |
| 分析步长 | 每帧，10ms | 每 2 帧取锚点，等效 20ms 分析帧移 |
| 未抽样帧 | 不适用 | 继承最近锚点判决，仍覆盖全部规范帧 |
| 分类 | 3 遍时序细化 | 单遍 |
| 后处理 | 精确合并 + 时长过滤 | 阈值筛选 + 锚点边界吸附 |
| 默认阈值 | 0.60 | 0.74 |

逼近路径没有改变真值帧格：它只在规范帧格上降低分析密度和特征维度。这一点保证精细/逼近路径的帧级指标具有相同分母和可比口径。

## 自适应裁决

规模观测固定包含：

- 对话数 `dialog_count`
- 总帧数 `total_frames`
- 平均每帧候选数 `avg_candidates_per_frame`（候选覆盖帧数 / 总帧数）
- 说话人数 `speaker_count`
- 特征维度 `feature_dimension`
- 预计精细路径相对逼近路径的事件召回增益 `recall_gain_estimate`

资源估算包含全量特征矩阵、评分矩阵、标签、候选、固定开销，以及按“对话数 × 说话人数”计的上文记账开销。候选比较次数按“候选帧数 × 事件类别数 × 分类遍数”估算。

显式优先级固定为：

1. 硬性驻留内存上限
2. 候选比较次数预算
3. 精细路径帧数上限
4. 预计事件召回增益

只有四项全部满足才选择精细路径；否则选择逼近路径并写入具体原因、资源估算、各对话路径和降级标记。若逼近路径仍超预算，记录为 best-effort fallback。决策对象的规范 JSON SHA-256 写入 `decision_sha256`；相同观测逐字节一致。

## 指标口径

### 帧级

- `frame_accuracy = 完全一致帧数 / 总帧数`
- `event_frame_recall = 真值活动且预测活动的帧数 / 真值活动帧数`
- `event_frame_precision = 真值活动且预测活动的帧数 / 预测活动帧数`
- 同时输出完整五类混淆矩阵和逐类 precision/recall/F1。
- 空分母比率定义为 `0.0`，分母同时以计数保留。

### 事件级

- 真值事件由连续且 `event_id` 相同的真值帧重建。
- 重叠比采用帧单元时间 IoU：`intersection_frames / union_frames`。
- 默认命中阈值 `0.5`，要求事件类型一致且一对一匹配。
- 匹配按 `(-iou, truth_start_frame, prediction_start_frame)` 确定性贪心选择。
- 明细记录每个真值/预测事件的起止帧、起止毫秒、IoU、是否命中及互相引用的 ID。
- `event_recall = 命中真值事件数 / 真值事件数`
- `event_precision = 命中预测事件数 / 预测事件数`

报告中的 `metrics_by_path` 分路径聚合；总体指标仅使用同一规范帧格上的计数聚合，不混用不同路径特征维度的中间量。

## 一致性自检

`run_self_check(report)` 检查以下类别，失败统一抛 `E_INCONSISTENT` 并携带 `category` 与 `dialog_id`：

- `frame_index_sets`：真值、特征、检测、后处理的帧索引集合哈希和计数完全一致。
- `timeline_coverage`：完整时间线从 0 开始、首尾相接、覆盖全部检测帧、毫秒区间合法。
- `event_details`：真值/预测命中互相引用，一对一且 IoU 达标。
- `metrics_recompute`：帧指标从逐帧判决重算；事件指标从逐事件明细重算；混淆统计一致。
- `path_coverage`：每个对话都有路径记录，所有帧都有判决。

测试覆盖了对指标、时间线和帧索引集合的主动篡改检测。

## 错误码

| 业务码 | 触发场景 |
|---|---|
| `E_CONFIG_INVALID` | 采样率非 16000、帧长非法、帧移大于帧长、非法 FFT/配置字段 |
| `E_DUP_DIALOG` | 对话清单出现重复 `dialog_id` |
| `E_EVENT_TYPE` | 真值、候选或时间轴出现受控集合外事件类型 |
| `E_TIMELINE_INVALID` | 增益越界（默认允许 `[0, 4]`）、时间轴超出音频、空区间、WAV 时长与清单不符 |
| `E_FRAME_MISMATCH` | 特征帧数与逐帧真值帧数不一致 |
| `E_EVENT_RANGE` | 候选起止帧越出 `[0, frame_count)` |
| `E_OVERLAP` | 事件区间重叠 |
| `E_OUT_OF_RANGE` | 事件区间越出音频时长 |
| `E_INCONSISTENT` | 跨阶段帧集合、覆盖或指标重算不一致 |

`AedError.to_dict()` 包含 `code`、`message`、`dialog_id`、`field` 和定位所需的 `details`（事件索引、帧位置、区间等）。

## 集成 API

```python
from aed_scale import (
    FeatureConfig,
    DetectionConfig,
    extract_features,
    detect_candidates,
    postprocess_candidates,
    path_profiles,
    decide_path,
    ScaleObservation,
    ResourceConstraints,
    run_dataset,
)

features = extract_features(pcm, FeatureConfig(), dialog_id="dialog_000", truth_frame_count=len(truth))
profiles = path_profiles(DetectionConfig())
detection = detect_candidates(features, profiles["fine"], detection_config=DetectionConfig())
segments = postprocess_candidates(
    detection.candidates,
    frame_count=features.frame_count,
    duration_samples=len(pcm),
    config=features.config,
    profile=profiles["fine"],
    min_event_ms=80,
    min_gap_ms=150,
    dialog_id="dialog_000",
)

decision = decide_path(
    ScaleObservation(
        dialog_count=1,
        total_frames=features.frame_count,
        avg_candidates_per_frame=0.5,
        speaker_count=3,
        feature_dimension=features.feature_dimension,
    ),
    ResourceConstraints(max_resident_mb=8.0),
)

report = run_dataset("path/to/dataset")
```

## 实际验证结果

最终示例使用 `--seed 20261121`：

- 小规模：2 对话、440 帧、3 说话人；自动选择精细路径。
- 大规模：8 对话、2400 帧、4 说话人；因内存估算 `0.947 MB > 0.940 MB`、候选比较 `19488 > 5000`、帧数 `2400 > 1000`，自动选择逼近路径并标记降级。

| 路径/场景 | 帧准确率 | 帧级事件召回 | 事件召回 | 事件精度 | 事件 F1 | 自检 |
|---|---:|---:|---:|---:|---:|---|
| 小规模 / 精细 | 0.9614 | 0.9755 | 1.0000 | 1.0000 | 1.0000 | OK |
| 小规模 / 强制逼近（对照） | 0.9318 | 0.9212 | 0.8750 | 0.7778 | 0.8235 | OK |
| 大规模 / 自动逼近 | 0.9042 | 0.8833 | 0.9063 | 0.8529 | 0.8788 | OK |

小规模对照中，精细路径相对逼近路径的事件召回提升 `0.1250`、帧准确率提升 `0.0295`。大规模报告中的精细路径估算被三项硬资源约束否决，因此按优先级选择逼近路径；所有帧与对话均被覆盖。

实际报告与哈希见：

- `artifacts/demo/report.json`
- 小规模对话清单 SHA-256：`c2825380b0d8694e264f6e7d7cf79506f4251e623c3277044e2a36d3003d595d`
- 大规模对话清单 SHA-256：`b7f9a22f2ac3246c2539910cbaac24f3f374c5a2408ffe816025028f1094f36a`

测试结果：`Ran 23 tests ... OK`。

## 边界与假设

- 规范帧格固定为 20ms 帧长、10ms 帧移；逼近路径的“长帧移”是检测分析锚点步长，不是改变真值帧格。
- 合成对话中说话人轮次不重叠，事件之间不重叠；事件可以与语音重叠。
- 事件真值从逐帧真值重建，事件级匹配在帧单元上计算，避免毫秒浮点边界导致指标不可重放。
- 输出报告不写时间戳；确定性字段可重复，包含绝对数据集路径的展示字段会随工作目录变化。
