# 技术说明

## 1. 确定性边界

同一个 Python/NumPy 运行时、同一份输入文件与配置下，所有 JSON 产物使用 `sort_keys=True`、固定分隔符、禁用 NaN，并在写出前固定浮点小数位。特征、嵌入、对齐、参数与 PCM 都由固定顺序的 NumPy 运算生成，不使用系统时间、随机 UUID 或非确定线程调度。

样本生成器只使用 `numpy.random.PCG64(fixed_seed)`。参考片段 revision 1 与 revision 2 的参数和 seed 都固定，因此删除数据目录后重新生成得到逐字节相同文件。

默认实验配置：

```text
sample_rate = 16000
frame_ms    = 20
hop_ms      = 20
frame_samples = 320
hop_samples   = 320
```

## 2. 样本与真值

`generate_dataset` 生成：

```text
data/
  manifest.jsonl
  refs.json
  config.json
  audio/utt-*.wav
  refs/ref-spk-*/r0001.wav
  refs/ref-spk-*/r0002.wav
  truth/utt-*.jsonl
```

清单字段严格为 `utterance_id, speaker_id, ref_id, start_ms, end_ms, text, seed, ref_revision, precision_profile`。真值字段为 `frame_index, f0_true, energy_true, phoneme_id`，每帧 20ms。

真值帧数按实际分析配置生成；默认 20/20 时每 20ms 一行，重叠配置（如 20/10）时每个 10ms 起点对应一个 20ms 分析窗。

合成器先逐帧确定 `f0`、能量包络与音素 schedule，再用固定共振峰控制的 8 次谐波叠加生成 PCM。真值中的 `f0_true` 来自合成过程，`energy_true` 从落盘前 PCM 帧独立计算。

## 3. 五个阶段

### 3.1 帧级特征

帧起点为 `i * hop_samples`，终点为 `start + frame_samples`：

```text
frame_count = 1 + (sample_count - frame_samples) // hop_samples
```

每帧输出：

- `energy_sum = sum(x^2)`、`energy_rms = sqrt(mean(x^2))`
- 过零率 `zcr`
- 60–400Hz 范围内的归一化自相关 `f0_candidate` 与 `f0_score`
- 频谱质心与 0–500Hz / 500–2000Hz / 2000Hz+ 三带能量占比

精度 profile 决定写入产物前的量化小数位。缓存键使用源 PCM 原始字节 SHA-256、帧长、帧移、采样率、profile、提取器版本和 seed。`ref_revision` 被显式排除，因为参考快照不是该阶段输入。

### 3.2 参考音色嵌入

只读取参考 WAV；不读取真值。参考片段按相同帧配置提取特征后计算 7 维向量：

```text
[log2(mean_f0/100), log(mean_rms), 4*mean_zcr,
 mean_centroid/nyquist, band_low, band_mid, band_high]
```

向量做逐参考片段 L2 归一化：

```text
embedding = v / ||v||_2
```

该口径写入产物字段 `normalization="l2"`、`normalization_scope="per-speaker-reference"`。参考帧按帧序号等分成 `unit_count` 个连续桶，每个桶的帧向量均值再 L2 归一化，得到发音单元原型。

### 3.3 发音单元对齐

每帧与目标 `ref_id` 的音素原型计算余弦相似度。候选排序为：

1. 分数降序；
2. `speaker_id` 升序；
3. `phoneme_id` 升序。

实现入口为 `choose_alignment_candidate`。对齐阶段不打开任何 truth 文件。

### 3.4 转换参数映射

显式映射：

```text
f0_ratio       = target_mean_f0 / source_mean_f0
mapped_f0      = clamp(source_f0 * f0_ratio ^ f0_strength, min_f0, max_f0)
energy_ratio   = target_mean_rms / source_mean_rms
mapped_energy  = source_rms * energy_ratio ^ energy_strength
formant_scale  = (target_centroid/source_centroid) ^ formant_strength
```

谐波权重由目标三带能量、音素编号与谐波次数共同决定。参数帧保存：

```text
sample_start, mapped_f0_hz, mapped_energy_rms, amplitude,
formant_scale, phase_start_cycles, unit_speaker_id,
phoneme_id, alignment_score, harmonic_weights
```

`phase_start_cycles` 逐帧累积，避免合成时依赖隐式累计状态。

### 3.5 逐帧波形合成

每帧由 `phase_start_cycles` 与 `mapped_f0_hz` 生成连续相位，再叠加参数中保存的谐波权重，按 `amplitude` 定标。默认 hop 等于帧长时直接拼接；hop 小于帧长时使用 Hann 窗 overlap-add 并做窗口和归一化。最终以确定性的 `np.rint` 转为 int16 PCM。

合成阶段读取真值帧数，仅用于对齐校验：

```text
frame_count == truth_frame_count
sample_count == (truth_frame_count - 1) * hop_samples + frame_samples
```

不一致返回 `E_FRAME_MISMATCH`，`details` 包含 `utterance_id`、`first_deviation_frame`、实际/期望帧数与样本数。

## 4. 内容寻址缓存

节点目录：

```text
<state>/cache/frame_features/<key[:2]>/<key>.json
<state>/cache/timbre_embedding/...
<state>/cache/conversion_params/...
<state>/cache/synth_pcm/...
```

每个文件包含 `key_components`、`key`、`artifact_hash` 和完整 artifact。键为规范化 JSON 的 SHA-256，产物哈希为 artifact 规范化 JSON 的 SHA-256。相同键读取并校验；不同键重算。

### 键组成

| 节点 | 音频内容哈希 | ref_revision | 帧长/帧移 | 映射版本 | 上游产物哈希 |
|---|---|---|---|---|---|
| `frame_features` | 源 PCM | 显式排除（参考无关） | 是 | 不适用 | 无 |
| `timbre_embedding` | 参考 PCM | 是 | 是 | 不适用 | 无 |
| `conversion_params` | 源 PCM + 参考 PCM | 是 | 是 | 是 | 特征、音色、对齐 |
| `synth_pcm` | 源 PCM + 参考 PCM | 是 | 是 | 是 | 转换参数 |

此外还包括采样率、precision profile、阶段版本和相关 seed/配置。`conversion_params` 与 `synth_pcm` 使用的映射配置哈希同时覆盖 `mapping_version` 与全部映射参数。

`python -m spkvc cache ... --utterance-id utt-001` 在 `nodes` 中返回四类缓存节点的完整键、键组成、产物哈希、缓存路径和命中状态；对齐是阶段快照派生产物，单独列在 `derived_nodes`。

## 5. 五阶段快照

阶段顺序固定：

```text
features -> timbre -> alignment -> params -> synthesis
```

`snapshots.json` 是唯一可见的快照索引，包含：

- 全局单调递增 `version_counter`
- 运行签名
- 每个阶段的 `stage_index`、`version`
- 完整 `completed_utterance_ids`
- payload 内容 SHA-256
- payload 相对路径
- index bundle 自身的 SHA-256

阶段完成后才调用提交。payload 先写入 payload 目录并 `fsync`，随后整个 index bundle 写入临时文件、`fsync`、`os.replace`。任何时刻读者只能看到旧的完整提交或新的完整提交。

恢复时：

1. 校验 bundle 校验和、阶段连续性、版本单调性、完成集合与每个 payload 校验和。
2. 从最近连续已提交阶段数作为下一阶段索引。
3. 已提交阶段直接从 payload 加载，不重新执行。
4. 未提交的进程内数据不存在于快照，下一阶段从未提交阶段的起点重建。

错误语义：

- 提交版本不大于已提交计数器：`E_SNAPSHOT_ROLLBACK`
- payload/bundle 校验和损坏或字段不一致：`E_SNAPSHOT_CORRUPT`
- 并发锁已被持有：立即 `E_STAGE_BUSY`
- 跳阶段提交或签名不一致：`E_SNAPSHOT_ORDER` / `E_STATE_SIGNATURE_MISMATCH`

## 6. 增量失效

增量要求已有完整五阶段基线快照。

### 参考片段变化

检测任一 utterance 的新 `ref_revision` 或其参考 PCM 哈希与旧音色产物记录不同。按 `ref_id` 得到受影响 utterance 集合，失效后缀：

```text
timbre -> alignment -> params -> synthesis
```

`features` 全部复用。同一 `ref_id` 的多个 utterance 共享同一个音色缓存键，因此只有首个节点真实计算，后续节点按缓存复用。

### 仅映射参数版本变化

受影响 utterance 为全部样本，失效后缀：

```text
params -> synthesis
```

`features`、`timbre`、`alignment` 全部复用。

两个变化同时出现时取并集。增量计算完成后，通过一次 `replace_all` 原子发布全部五阶段的新快照版本；未受影响 payload 原样复用，但索引会获得新的单调版本。中途终止时旧 bundle 仍完整可见。

生效配置与新 reference revision 清单写入带签名的 `effective_config.json` / `effective_entries.json`。`run` 只在文件签名与已提交快照签名一致时采用它们，因此“增量 bundle 已替换但输出阶段被终止”的重启仍能恢复正确输入，而提交前终止不会误用新配置。

结果对象输出：

- `affected_utterance_ids`
- `affected_nodes`
- `recomputed_nodes`
- `reused_nodes`
- `snapshot_versions`
- `degradations`

## 7. 校验、资源与自检

业务错误均为 `{ok:false, code, message, field, details}`：

| 场景 | code | field |
|---|---|---|
| 采样率非 16000 | `E_INVALID_SAMPLE_RATE` | `sample_rate` |
| frame_ms 非法 | `E_INVALID_FRAME_MS` | `frame_ms` |
| 帧移大于帧长 | `E_INVALID_FRAME_HOP` | `hop_ms` |
| 重复 utterance_id | `E_DUPLICATE_UTTERANCE_ID` | `utterance_id` |
| ref_id 缺失 | `E_MISSING_REF_ID` | `ref_id` |
| 未知 precision_profile | `E_UNKNOWN_PRECISION_PROFILE` | `precision_profile` |
| 帧/样本/真值不一致 | `E_FRAME_MISMATCH` | `frame_count` |
| 资源超限 | `E_RESOURCE_LIMIT` | 对应上限字段 |

资源上限：

- 单次增量重算 utterance 数超限：`E_RESOURCE_LIMIT`
- 驻留音色嵌入超过上限：LRU 淘汰并标记 `embedding_cache_lru`
- 期望并行节点数超过上限：降级到上限并标记 `parallel_nodes_capped`

自检校验：

1. 五阶段完成集合一致；
2. 重算集合与复用集合互斥，并覆盖全部受影响节点；
3. 从转换参数重新合成 PCM，与快照 PCM、运行结果 PCM 的 SHA-256 三方一致；
4. 缓存键包含必要输入字段；
5. 每个缓存 artifact 哈希有效。

真值不进入转换链路。`spkvc evaluate` 从已提交的转换参数和对齐快照独立读取真值，输出逐 utterance 与总体 `f0_mae_hz`、`energy_mae`、`alignment_accuracy`，用于误差观测。

## 8. Windows 说明

代码使用 `pathlib`、原子 `os.replace` 与 `os.fsync`。并发锁在 POSIX 使用 `fcntl.flock`，在 Windows 使用 `msvcrt.locking`，均为非阻塞模式。工作目录只需本地可读写；建议避免网络共享目录，因为 `os.replace` 与目录 `fsync` 的语义取决于底层文件系统。

