# 音色克隆与发音建模误差实验组件

本项目从零实现一个可本地集成、完全确定性的音色克隆与发音建模组件。它围绕固定种子的
16 kHz 单声道 PCM 样本，覆盖帧级特征提取、参考音色嵌入、发音单元对齐、三档数值精度、
逐级误差传播、相似度裁决、快照版本化、故障注入诊断和跨级一致性自检。

项目不依赖外部音频、账号、网络服务、容器或系统时间。合成、排序、快照校验和以及
JSON 输出均由固定种子和稳定排序决定。

## 快速开始（Windows）

要求 Python 3.10+ 与 NumPy。

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python -m timbre_lab demo --workdir artifacts\demo
```

若不做 editable install，可临时设置：

```bat
set PYTHONPATH=src
python -m timbre_lab demo --workdir artifacts\demo
```

完整演示同时运行六类故障注入，并把结果写入文件：

```bat
python -m timbre_lab demo --workdir artifacts\demo --inject-errors --output artifacts\demo\demo_report.json
```

仅生成确定性样本：

```bat
python -m timbre_lab generate --workdir artifacts\demo --seed 20261120 --speakers 3 --utterances-per-speaker 2
```

重新验证已有样本：

```bat
python -m timbre_lab verify --dataset artifacts\demo --snapshot-dir artifacts\demo\snapshots
```

单独运行故障注入诊断：

```bat
python -m timbre_lab diagnose --dataset artifacts\demo --unattributed
```

运行测试：

```bat
set PYTHONPATH=src
python -m unittest discover -s tests -v
```

`examples/run_demo.py` 是最小集成示例。

## 目录

```text
src/timbre_lab/
  synthesis.py       固定种子 PCM、参考片段、真值、manifest
  features.py        精确帧边界、能量、过零率、f0 候选、频谱特征
  embedding.py       19 维音色统计、8 维对齐向量、确定性候选和平局
  precision.py       float64/float32/定点三档共享算子语义
  error_spectrum.py  分级偏差、区间、放大比、主导归因、真值观测
  decision.py        门限、严格/容差带、次正规保护、top-k
  diagnostics.py     注入式输入验证和顶层未归因异常
  snapshots.py       三类快照的整包版本化和校验和
  pipeline.py        端到端领域流水线
  selfcheck.py       跨级一致性自检
  component.py       可集成对象门面
  cli.py             命令行 JSON 示例
tests/               25 项标准库 unittest 验收测试
docs/DESIGN.md       算法、误差传播与快照设计说明
artifacts/demo/      实际生成的样本、真值、快照和完整 JSON 报告
```

## 本地样本契约

每个数据集包含：

```text
manifest.jsonl        utterance 清单
references.jsonl      参考音色片段清单
dataset.json          数据集元数据
audio/*.wav           16 kHz、单声道、16-bit PCM
refs/*.wav            每说话人固定参考片段
truth/*.truth.jsonl   20 ms 逐帧真值
```

`manifest.jsonl` 每行至少包含：

```json
{"utterance_id":"utt_s00_000","speaker_id":0,"ref_id":"ref_s00","sample_rate":16000,"frame_ms":20.0,"pos_scale":1.0,"precision_tier":"float64","seed":123}
```

同时写入 `hop_ms`、`num_frames`、`num_samples`、`pcm_file`、`truth_file` 和
`pcm_sha256`，使 seed 与片段形状可以被独立验证。

逐帧真值字段固定为：

```text
frame_index, speaker_id, f0_true, energy_true, phoneme_id
```

默认帧几何为 20 ms 帧长、20 ms 帧移，即 320 个样本。帧移可配置，但所有边界始终由
整数样本索引计算：

```text
start_i = i * hop_samples
end_i   = start_i + frame_samples
```

因此 `frame_index` 集合、PCM 长度、真值行数三者可以严格互验。

## 处理链

### 1. 帧级特征

每帧输出短时能量、RMS、过零率、自相关基频候选、自相关峰值、频谱质心、
频谱带宽、频谱平坦度和 7 个频带的 log 能量比。

基频候选先在 70–400 Hz 对应的 lag 范围计算归一化自相关，再在接近全局峰值的局部峰中
选择最短 lag，避免把 2T 次谐波误判为基频。候选不是语音识别结果；它与 `f0_true`
的差值和口径会显式进入 `truth_observation`。

同一 utterance、seed 和配置下，`FeatureTable.to_jsonl_bytes()` 逐字节一致。
帧数或索引与真值不一致时返回：

```text
E_FRAME_MISMATCH
stage = feature_frames
details.first_mismatch_frame = 首个不符帧
```

### 2. 参考音色嵌入与发音单元对齐

参考片段的 19 维音色统计量为：

```text
mean/std(log f0)
mean/std(log energy)
mean/std(zcr)
mean/std(autocorr peak)
mean/std(normalized spectral centroid)
mean(spectral flatness)
mean(normalized spectral bandwidth)
mean(log band energy ratio), 7 个固定频带
```

嵌入维度、统计口径、标准差自由度、频带边界和 L2 归一化方式均写入结果和快照。

发音单元候选集合为 `speaker_id × phoneme_id`。每帧使用 8 维声学向量和候选向量做
余弦相似度；候选遍历前按 `(speaker_id, phoneme_id)` 升序排序，因此同分时严格按
`speaker_id`、再按 `phoneme_id` 打破平局，不依赖字典或输入遍历顺序。

### 3. 三档精度执行

三档共享相同阶段：

```text
feature_scale     x -> x * feature_scale
embedding_norm    x -> x / ||x||
log_transform     x -> sign(x) * log1p(|x|)
similarity_accum  选中单元余弦相似度 -> 逐帧 cumsum
```

- `float64`：阶段张量保持 float64。
- `float32`：阶段输入/输出和算术使用 NumPy float32。
- `fixed`：在每个阶段边界执行整数网格量化。

定点量化：

```text
q = round_mode(x * fixed_scale)
x_fixed = q / fixed_scale
```

支持的舍入模式为 `half_even`、`half_away`、`toward_zero`、`floor`、`ceil`。
未知精度档返回 `E_CONFIG_INVALID` 且 `field=precision_tier`；非正缩放因子返回
`E_CONFIG_INVALID` 且 `field=fixed_scale`。

为了保证“切换精度档不改变帧集合与单元指派集合”，单元指派由 canonical float64
执行结果一次性确定；其他档只改变这些固定帧、固定单元的数值计算。每个执行结果都携带
`frame_set_digest` 和 `assignment_digest`，自检会逐 utterance 比较。

定点档的每个阶段同时保留量化前张量、量化后张量和量化残差。`verify_fixed_recomputation`
会用配置中的 scale 和 rounding mode 重新量化每个阶段，并要求逐元素完全一致。

### 4. 误差传播与归因

`float64` 是同一快照内的参考真值。对 `float32` 与 `fixed`，逐级输出：

- 绝对偏差：逐帧分量绝对差的均值；
- 相对偏差：绝对偏差和 / float64 参考绝对值和；
- 95% 均值误差区间：`mean ± 1.96 * SEM`；
- 误差放大比：当前阶段 / 上一阶段平均绝对偏差；
- 主导来源：平均绝对偏差最大的阶段；
- `evidence_frames`：主导阶段中贡献最大的帧及每个分量的偏差明细；
- `detail_digest`：偏差明细的 SHA-256，供裁决追踪。

主导来源只会是：

```text
feature_scale
embedding_norm
log_transform
similarity_accum
```

真值观测口径同时输出：

```text
f0_true:    Hz
energy_true: 未加窗帧内 PCM 平方均值
f0 观测:    帧级自相关候选
energy 观测: 同一帧边界内的未加窗平方均值
```

### 5. 克隆音色相似度

克隆裁决使用 19 维统计量的尺度化距离：

```text
d = sqrt(sum_i weight_i * ((left_i - right_i) / scale_i)^2))
similarity = exp(-d / 8)
```

`scale_i` 与 `weight_i` 是代码中的显式常量。相似度被裁剪到 `[-1, 1]`；
`|similarity| < max(subnormal_floor, float64.tiny)` 时归零，并计数
`subnormal_guard_count`。

比较策略：

- `strict`：仅 `similarity > threshold` 接受；
- `tolerance`：`similarity >= threshold - tolerance` 接受，并标记 `near_threshold`。

top-k 排序使用稳定键，容差策略会把相差不超过 tolerance 的分数组内按 candidate ID
排序。门限越界、tolerance 非正、top-k 非正分别返回 `E_CONFIG_INVALID`，字段为
`threshold`、`tolerance`、`top_k`。

## 结构化业务码

| 业务码 | 触发条件 | 关键定位 |
|---|---|---|
| `E_CONFIG_INVALID` | 精度档、scale、rounding、门限、tolerance、top-k 等非法 | `field` |
| `E_FRAME_MISMATCH` | 特征帧数与真值不一致 | `first_mismatch_frame` |
| `E_SAMPLE_RATE_MISMATCH` | 样本采样率不是 16000 | `utterance_id`、实际/期望采样率 |
| `E_SEED_SHAPE_MISMATCH` | seed 推出的帧形状与 PCM/帧数不一致 | `seed`、实际/期望形状 |
| `E_REF_MISSING` | `ref_id` 缺失或无法解析 | `utterance_id`、`ref_id` |
| `E_DUPLICATE_UTTERANCE` | manifest 中 `utterance_id` 重复 | `occurrences` |
| `E_PHONEME_OUT_OF_RANGE` | 真值 phoneme ID 越界 | `frame_index`、允许范围 |
| `E_SNAPSHOT_ROLLBACK` | 旧版本覆盖新版本或同版本异内容 | snapshot category/version |
| `E_SNAPSHOT_CORRUPT` | 快照 JSON、类别、版本或校验和损坏 | category/version/reason |
| `E_INCONSISTENT` | 跨级一致性自检失败 | `category` |

所有输入诊断都会记录 `stage` 和 `utterance_id`。没有被任何业务阶段归因的异常进入
顶层 `unattributed_exceptions`，不会静默丢失。

## 快照与重启

`SnapshotManager` 管理三个互相独立的类别：

```text
embedding_stats
precision_config
error_spectrum
```

每条快照包含单调递增 `version`、完整 payload 和内容校验和。提交前先对所有类别做
rollback/内容冲突预检，然后以完整文件 `os.replace` 原子替换。加载时重新计算校验和；
重启后的 `reload()` 整体替换内存快照表，并报告 `added`、`removed`、
`version_changed` 或 `checksum_changed`。

## 命令输出

`demo` / `verify` 的标准输出是 JSON，包含：

```text
error_spectrum
truth_observation
clone_decisions
self_check
snapshots
business_codes
degraded
degradation_flags
```

`degraded` 表示存在业务错误、故障注入或自检失败；
`degradation_flags.precision_loss_observed` 单独表示较低精度档确实出现数值差异。

## 可集成 API

```python
from timbre_lab import (
    PipelineConfig,
    SnapshotManager,
    generate_dataset,
    run_pipeline,
    run_selfcheck,
)

dataset = generate_dataset("artifacts/demo", seed=20261120)
result = run_pipeline(dataset, PipelineConfig())
assert run_selfcheck(result)["ok"]

manager = SnapshotManager("artifacts/demo/snapshots")
result.publish_snapshots(manager)
print(result.report(include_error_details=False))
```

也可使用对象门面：

```python
from timbre_lab import TimbreCloneComponent

component = TimbreCloneComponent()
features = component.extract_features(pcm, "utt_local")
```

公开入口还直接导出 `extract_features`、`estimate_timbre`、`align_units`、
`execute_precision`、`compute_error_spectrum`、`diagnose_records`、
`adjudicate_clone_similarity`、`run_selfcheck` 等函数。

## 实际验证结果

验证环境：Python 3.12.13、NumPy 2.5.5。

```text
python -m unittest discover -s tests -v
Ran 25 tests
OK
```

覆盖内容包括：

- 同 seed 生成的全部文件逐字节一致；
- 特征 JSONL 逐字节一致，帧边界等于 `i * hop_samples`；
- `E_FRAME_MISMATCH` 返回首个不符帧；
- 三档执行帧集合、单元指派集合、重复运行结果一致；
- 定点档按 scale/rounding 可逐级重算；
- 误差谱含四阶段偏差、区间、放大比、主导来源与真值口径；
- 严格/容差带、次正规保护、稳定 top-k；
- 快照回退、同版本异内容、校验和损坏；
- 六类输入故障注入和顶层未归因异常；
- 自检能识别区间倒置、定点重算破坏和裁决追踪破坏；
- CLI 的 JSON、业务码和降级标记。

额外跨种子验证：10 个 seed、每个 3 说话人 × 2 样本，共 60 个 utterance，
克隆排序错误 0，门限拒绝 0。

默认 `seed=20261120`、3 说话人 × 2 样本的实际演示结果：

```text
speakers=3 utterances=6 frames=412
float32 dominant=similarity_accum
fixed   dominant=similarity_accum
fixed stage quantization recompute=OK
self-check=OK (all 5 categories)
clone decisions=6/6 match expected speaker and accepted
injected business codes=6
unattributed exceptions=2
truth energy_mae=0
truth f0_mae_hz=5.740481780368553
truth f0_relative_mae=0.033240111502469076
```

完整报告位于 `artifacts/demo/demo_report.json`。

## 约束与口径说明

- PCM 是 16 kHz、单声道、16-bit WAV；不使用浮点 WAV 或外部编解码器。
- 误差区间是“逐帧平均绝对偏差均值的 95% 正态置信区间”，不是逐样本容差保证。
- `float64` 是快照内数值参考，不是物理声学绝对真值；物理观测真值仍是
  `f0_true` 和 `energy_true`。
- 单元指派刻意由 canonical float64 固定，确保不同精度档的差异只来自精度损失。
- 合成说话人基频基准按确定性分位点分散生成，避免把“两个几乎同音的对象”误当成
  精度算法问题。

