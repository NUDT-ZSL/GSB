# 确定性语音情感与对话理解组件

面向本地合成语音样本的可复现实验组件。它从 WAV 样本与对话脚本出发，完成逐帧声学特征、段级情感打分、对话级上下文融合、意图-情感联合输出、规模自适应策略选择、增量缓存、原子快照、异常隔离与 JSON 诊断报告。

实现只依赖 Python 3.10+ 与 NumPy，不依赖网络、GPU、容器、外部音频数据或随机数发生器。

## 目录

```text
src/voice_dialog/
  audio.py        WAV 扫描、RIFF/格式校验、PCM 解码
  features.py     切帧、自相关基频、能量、过零率、语速节奏与段级聚合
  model.py        固定声学原型、归一化常量、两类确定性分类模型
  scoring.py      最近质心 / 加权逻辑回归、稳定 ppm 归一化、边界样本
  dialogue.py     对话脚本解析、前驱链与缺失回退
  fusion.py       因果窗口平滑、同/跨说话人权重、联合输出
  adaptive.py     规模指标、候选策略评分与确定性选择
  cache.py        内容寻址缓存与命中/失效统计
  snapshot.py     阶段快照、版本/配置哈希校验、原子替换
  pipeline.py     并发阶段执行、稳定排序、报告装配
  synthetic.py    固定种子、无 RNG 状态的 WAV/对话生成器
  cli.py          generate / run 命令
configs/          演示配置与异常注入清单
data/demo/        已生成的 76 个正常段、4 个异常样本与 18 轮对话脚本
tests/            20 个 unittest 用例
scripts/          样本生成、验收、Windows 冒烟测试
runs/final/       最终全量与增量报告、缓存与快照
artifacts/        最终诊断报告、验收摘要和测试日志
```

## 快速开始

### Linux / macOS

```bash
python3 -m venv .venv
.venv/bin/python -m pip install numpy
PYTHONPATH=src .venv/bin/python -m voice_dialog.cli generate \
  --out data/demo --segments 76 --turns 18 --seed 20261117

PYTHONPATH=src .venv/bin/python -m voice_dialog.cli run \
  --samples data/demo \
  --script data/demo/dialog_script.json \
  --config configs/demo_config.json \
  --injections configs/anomaly_injection.json \
  --state runs/final/full_state \
  --out runs/final/full_report.json \
  --mode full
```

更新输入或配置后，将 `--mode` 改为 `incremental` 并复用同一个 `--state`。若要与空白状态的全量重建做严格比较，为新的一次全量运行指定新的 `--state` 目录。

也可以直接作为 Python 组件调用：

```python
from pathlib import Path
from voice_dialog.config import load_config, load_injections
from voice_dialog.pipeline import Pipeline

pipeline = Pipeline(
    samples_dir=Path("data/demo"),
    script_path=Path("data/demo/dialog_script.json"),
    config=load_config(Path("configs/demo_config.json")),
    injections=load_injections(Path("configs/anomaly_injection.json")),
    state_dir=Path("runs/api_state"),
    mode="incremental",
)
report = pipeline.run()
```

### Windows

在 PowerShell 中：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install numpy
.\.venv\Scripts\python -m pip install -e .

.\.venv\Scripts\python -m voice_dialog.cli generate --out data\demo --segments 76 --turns 18 --seed 20261117
.\.venv\Scripts\python -m voice_dialog.cli run `
  --samples data\demo `
  --script data\demo\dialog_script.json `
  --config configs\demo_config.json `
  --injections configs\anomaly_injection.json `
  --state runs\final\full_state `
  --out runs\final\full_report.json `
  --mode full
```

`scripts/windows_smoke.py` 会在临时目录检查 WAV 写读、同目录临时文件加 `os.replace`、原子 JSON 写入和线程池创建：

```powershell
.\.venv\Scripts\python scripts\windows_smoke.py
```

本实现只使用 Windows 原生可用的 `wave`、`os.replace`、`tempfile`、`ThreadPoolExecutor` 与 `signal` 接口。本次实测环境为 Linux + Python 3.11；Windows 冒烟脚本可在目标机器直接复跑。

## 输入格式

### 样本目录

`--samples` 指向包含 WAV 和对话脚本所引用相对路径的根目录。扫描会递归收集所有 `.wav`；演示脚本中的 `audio/seg_0001.wav` 因此相对于 `data/demo`。

WAV 必须满足：RIFF/WAVE 容器、16000 Hz、单声道、16 bit PCM、未压缩。格式不正确时记录 `E_AUDIO_FORMAT` 并继续处理其他文件。

### 对话脚本

```json
{
  "dialogue_id": "demo",
  "turns": [
    {
      "turn_id": "T001",
      "order": 1,
      "speaker": "A",
      "audio": "audio/seg_0001.wav",
      "prev_turn": null,
      "intent": "greet",
      "text": "optional text",
      "emotion_label": "happy"
    }
  ]
}
```

`emotion_label` 只用于诊断准确率，不参与打分。未提供 `prev_turn` 字段时，按 `order` 自动连接上一轮；显式 `null` 表示没有前驱。`order` 与 `turn_id` 均要求唯一。

### 配置

完整示例见 `configs/demo_config.json`。主要字段：

- `frame_length_ms` / `frame_shift_ms`：切帧参数，末帧补零。
- `acoustic_feature_set`：可选特征池。
- `emotion_granularity`：`coarse`（negative/neutral/positive）或 `fine`（angry/disgust/fear/happy/neutral/sad）。
- `classifier`：`auto`、`nearest_centroid` 或 `weighted_logistic_regression`；显式值会覆盖选择器给出的分类器。
- `fusion.window_mode`：`adaptive` 使用策略窗口，`fixed` 强制使用 `fusion.window`。
- `fusion.decay` / `same_speaker_weight` / `cross_speaker_weight`：因果上下文权重。
- `fusion.missing_predecessor_rule`：`nearest_available` 或 `zero_context`。
- `feature_dimension_limit`：有效特征维度上限。
- `scale_tier`：`auto` 或强制 `small` / `medium` / `large`。
- `scale_tiers`：按 `segments + 2*turns + duration_weight*avg_duration` 计算规模单位。
- `concurrency`：线程池上限。
- `snapshot_interval`：批次快照最少处理任务数。

### 异常注入

演示清单为 `configs/anomaly_injection.json`。支持的 `kind`：

- `nonfinite_sample`：解帧后向目标流的首个样本写入 NaN，命中 `E_NONFINITE_SAMPLE`。
- `zero_duration`：目标 WAV 零帧，命中 `E_ZERO_DURATION`。
- `bad_audio_format`：目标 WAV 采样率等不合规，命中 `E_AUDIO_FORMAT`。
- `invalid_granularity`：目标段使用 `value` 中的非法粒度，命中 `E_INVALID_GRANULARITY`。
- `missing_predecessor`：目标轮次强制走缺失前驱回退，命中 `E_MISSING_PREDECESSOR` 并显式记录回退来源。

注入清单按 `id` 排序后参与指纹计算，顺序变化不会改变结果。每个命中的注入编号写入报告 `injections.hits`。

## 算法与取舍

### 帧特征

- 帧数 `ceil(sample_count / shift)`，末帧不足部分补零。
- 基频：去均值、Hann 窗、FFT 自相关，在 60–500 Hz 的滞后范围搜索归一化自相关峰；峰值低于 0.25 判为清音，最后按 10 Hz 固定档位量化。
- 短时能量：帧 RMS。
- 过零率：相邻样本异号比例。
- 帧级节奏：局部上升沿搜索得到瞬时语速，局部能量平滑度得到瞬时节奏值。
- 段级特征：基频均值/标准差/范围/斜率、能量均值/标准差/动态范围、过零率均值/标准差、清浊比例、语速、节奏规则性、停顿比例、活动比例。
- 有效特征按策略表 ∩ 配置特征池后，再按配置维度上限截断。

### 段级情感

两类分类器走真正不同的路径：

- `weighted_centroid`：在固定标准化空间计算加权欧氏距离，距离取负后稳定 softmax。
- `balanced_weighted_logistic_regression`：围绕固定类别原型构造确定性网格训练集，执行固定全批量多分类逻辑回归；样本权重按类别平衡。没有随机初始化。

概率首先转为百万分率整数再归一化，避免逐 float 累加产生不稳定尾差；同分按类别名的字典序决断。最高与次高得分差不超过 `boundary_margin` 时标记边界样本，并输出差值及 `reference_precision`。

### 对话融合

对轮次 `t` 与其最多 `window` 个前驱：

```text
base_weight(d) = decay^d
speaker_weight = same_speaker_weight 或 cross_speaker_weight
raw_weight = base_weight × speaker_weight
fused = Σ(raw_weight × scores) / Σ(raw_weight)
```

当前轮基础权重为 1。每个上下文项输出距离、衰减因子、说话人关系、原始权重、归一化贡献、得分来源与可用性。前驱缺失时：

- `nearest_available`：按脚本顺序回退到最近的未使用轮次，并写入 `fallback_source`。
- `zero_context`：仅保留当前轮，并写入 `zero_context` 回退来源。

融合后的情感与脚本意图组合为 `joint_output`，同时输出结构化 intent/emotion 与 `intent:emotion` 标签。

### 规模自适应

候选策略表按 `small` / `medium` / `large` 三档给出特征子集、分类器和融合窗口。评分综合：

- 策略基础分。
- 片段数拟合度。
- 平均时长拟合度。
- 对话轮次数拟合度。
- 粗/细粒度适配度。

输出包含规模指标、档位来源、全部候选分数及分项、选中策略、最终有效算法和可读理由。显式配置的分类器或固定融合窗口作为约束覆盖，并记录在 `constraints`。

## 增量、并发与一致性

### 缓存键

```text
feature_key = H(filename, content_sha256, feature_stage_config, scale_tier,
                relevant_injections)
score_key   = H(feature_artifact_digest, granularity, classifier, margin,
                effective_features, scale_tier, relevant_injection)
fusion_key  = H(current_turn, causal_context_score_digests, fallback_source,
                fusion_stage_config, scale_tier, relevant_injection)
```

因此：

- 粒度或分类器变化只重算打分及下游融合。
- 融合窗口/衰减/说话人权重变化只重算融合。
- 单个音频变化只重算该文件特征、该段打分，以及因果窗口覆盖它的轮次融合。
- 无关注入项、无关轮次文本或远端轮次变化不会使当前任务错误失效。
- 确定性失败也缓存为负结果，重复增量运行不会再次计算失败段。

### 并发顺序

每个任务携带由阶段、文件名或轮次派生的稳定排序键。线程池只负责计算，结果按原任务序号归并；样本、帧、轮次、失败清单和注入命中均按固定规则排序。并发度变化只影响执行统计，不改变业务结果。

### 快照和信号

每个阶段以同目录临时文件、`flush + fsync`、`os.replace` 提交快照。快照含：

```text
snapshot_version, stage, config_hash, injection_hash,
processed_cursor, cursor, processed[], artifact_digest, status
```

恢复时若阶段版本、配置哈希、注入哈希或阶段名不符，写入 `E_CONFIG_MISMATCH`，再依靠内容寻址缓存做安全的局部复用。`SIGINT`、`SIGTERM` 或 Windows `SIGBREAK` 会设置停止标志：完成当前批次、提交当前阶段快照、写出中断状态报告，并以退出码 `0` 退出。

## 报告

`artifacts/demo_report.json` 是最终完整诊断报告，包含：

- `config`、`config_fingerprint`、`injection_fingerprint`。
- `scale_decision`：指标、候选评分、选择理由和有效算法。
- `segments`：稳定顺序、逐帧记录、帧摘要、全量/有效特征、各类别得分、最高类别、边界标记、参考精度与差值、失败码。
- `dialog_turns`：融合得分、情感、意图、联合标签、上下文贡献、前驱、回退来源。
- `failures`：阶段、文件、轮次、业务码、异常类型、注入编号和说明。
- `injections.hits`：实际命中的注入编号。
- `cache`：各阶段命中、重算、损坏清单。
- `snapshots`：版本、配置/注入哈希、游标、产物摘要与 E_CONFIG_MISMATCH 记录。
- `diagnostics`：计数、帧数、准确率、算法指纹、分类器路径统计。
- `report_fingerprint`：剔除执行统计后的稳定报告指纹。

全量与增量一致性比较使用 `stable_report_view`，它剔除 `cache`、运行时信息和快照错误历史，但保留业务结果、决策、失败、注入、诊断和已提交快照。

## 实际验证结果

最终演示输入：80 个 WAV（76 个正常段 + 4 个异常样本）、18 轮对话、平均时长 0.821554 秒、共 6365 帧。

```text
规模档位             : medium（自动）
选中策略             : medium_balanced_lr
有效分类器           : weighted_logistic_regression
有效特征 / 融合窗口  : 12 / 5
段级诊断准确率       : 15/18 = 83.33%
异常注入命中         : 5/5
预期业务码           : E_AUDIO_FORMAT, E_ZERO_DURATION,
                       E_NONFINITE_SAMPLE, E_INVALID_GRANULARITY,
                       E_MISSING_PREDECESSOR
全量报告指纹         : b6830dd28ea37adcf7b06fd9fb0a05d8432b3b0521b2154ae030f6b0649549ff
增量报告指纹         : b6830dd28ea37adcf7b06fd9fb0a05d8432b3b0521b2154ae030f6b0649549ff
稳定视图逐字段相等   : true
未变化增量缓存       : features 79/79, scores 77/77, fusion 18/18 命中
```

规模策略探针（相同配置、确定性重算两次）：

| 规模 | 片段 / 轮次 | 规模单位 | 档位 | 策略 | 分类器 | 窗口 | 特征数 |
|---|---:|---:|---|---|---|---:|---:|
| 小 | 30 / 6 | 50 | small | small_balanced_nc | nearest_centroid | 3 | 12 |
| 中 | 300 / 40 | 388 | medium | medium_balanced_lr | weighted_logistic_regression | 5 | 12 |
| 大 | 2000 / 100 | 2208 | large | large_compact_nc | nearest_centroid | 2 | 6 |

自动验收结果保存在 `artifacts/validation_summary.json`，其中 14 项检查全部通过。另一个集成测试从初始状态依次执行：粗/细粒度切换、分类器切换、融合窗口变化、单段音频替换和规模档切换；每次增量运行后都在独立空白状态做当时配置的全量重建，并逐字段比较，7 次比较全部一致。

测试结果：

```text
Ran 20 tests in 23.340s
OK
```

完整日志见 `artifacts/unittest.log`。测试覆盖音频格式、零时长、非有限样本、帧确定性、维度上限、两分类路径、稳定归一化、边界与同分决断、缺失前驱、加权贡献、规模策略、线程数一致性、选择性失效、全量/增量一致性、快照版本错配和实际 `SIGINT` 退出码 0。

Windows 冒烟脚本本次运行结果：

```json
{"all_passed": true, "atomic_json": true, "os_replace": true, "thread_pool": true, "wav_roundtrip": true}
```

## 复跑全部验证

```bash
PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v

PYTHONPATH=src .venv/bin/python scripts/validate_acceptance.py \
  --full-report runs/final/full_report.json \
  --incremental-report runs/final/incremental_report.json \
  --config configs/demo_config.json \
  --out artifacts/validation_summary.json

PYTHONPATH=src .venv/bin/python scripts/windows_smoke.py
```

## 设计边界

- 固定原型与逻辑回归参数服务于本地合成样本示范，不声称泛化到真实语料；接入真实数据时应替换为经许可训练并版本化的模型。
- 非有限采样注入发生在 PCM 解码后的确定性注入点，因为标准 16-bit PCM 文件本身不能直接存储 NaN。
- 融合采用因果前驱窗口，不读取未来轮次，便于在线扩展与稳定失效判定。
- 报告包含逐帧记录，规模很大时可单独增加报告瘦身开关；当前实现优先保证诊断透明度。
