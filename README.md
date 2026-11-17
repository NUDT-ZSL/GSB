# voiceprint-diag

面向音色克隆与发音建模场景的可集成、确定性、定点化声纹推理组件。项目包含固定种子合成样本、完整推理流水线、逐级误差诊断、内容指纹缓存、原子快照恢复和示例报告；不依赖真实录音、外部服务或容器。

## 快速开始

### Windows 10/11

```powershell
py -3.10 --version
py -3.10 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt

.\.venv\Scripts\python -m voiceprint_diag generate-demo --output demo_input
.\.venv\Scripts\python -m voiceprint_diag run `
  --samples demo_input\samples `
  --config demo_input\config.json `
  --pairs demo_input\pairs.json `
  --anomalies demo_input\anomalies.json `
  --output output
```

### Linux / WSL

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m voiceprint_diag generate-demo --output demo_input
.venv/bin/python -m voiceprint_diag run \
  --samples demo_input/samples \
  --config demo_input/config.json \
  --pairs demo_input/pairs.json \
  --anomalies demo_input/anomalies.json \
  --output output
```

运行完成后：

- `output/report.json`：完整诊断报告。
- `output/canonical_results.json`：去除耗时与执行元数据后的规范结果，用于跨并发/恢复点比对。
- `output/cache/`：按内容缓存键命名的各阶段产物。
- `output/state/state.json` 与 `output/state/stages/`：全局清单和阶段原子快照。

本仓库已包含用种子 `20261117` 生成的 `demo_input/` 和一次完整运行的 `output/`。

## 输入目录结构

```text
demo_input/
  config.json
  pairs.json
  anomalies.json
  samples/
    speaker_a/a1.wav
    speaker_a/a2.wav
    speaker_b/b1.wav
    ...
    defects/bad_rate.wav
    defects/bad_bits.wav
    defects/short_silence.wav
```

程序只读取输入，不写入样本目录。运行产物全部写入 `--output` 指定的独立目录。

## 配置

配置字段完整示例见 `demo_input/config.json`，可用别名包括 `frame_length_ms`、`mel_max_hz`、`n_mels`、`fixed_fraction_bits`、`threshold` 等。

| 字段 | 含义 |
| --- | --- |
| `frame_ms`, `hop_ms` | 帧长与帧移毫秒；实现中按采样率四舍五入为样本数。 |
| `mel_bands`, `mel_max_freq`, `cepstral_order` | Mel 频带数、上限频率和倒谱阶数。 |
| `embedding_dim`, `quantization_levels` | 声纹向量维度和对称量化档位数。 |
| `fractional_bits` | 工作定点 Q 格式的小数位宽。 |
| `precision_trace_bit_step` | 误差追踪时高于工作位宽的档位步长。 |
| `similarity_threshold` | 余弦判定阈值；`similarity_fixed >= threshold_fixed` 判为同一说话人。 |
| `min_valid_frames`, `silence_energy_threshold` | 最低有效帧数与静音过滤门限（自然对数能量）。 |
| `concurrency`, `snapshot_interval` | 线程池并发数和阶段内进度快照间隔。 |
| `feature_significant_digits` | 特征与报告数值的有效位数。 |
| `precision_tolerance` | 判断某环节是否首次超过容差的绝对误差门限。 |
| `projection_seed`, `projection_matrix_scale`, `projection_clip` | 定点投影矩阵种子、整数幅度和量化裁剪范围。 |
| `similarity_scale` | 定点余弦的整数刻度，示例为 `1e9`。 |
| `global_version` | 快照兼容版本。 |
| `quantile_probabilities` | 确定性分位数列表。 |

严格校验：Python 3.10+、NumPy 1.24–2.x、16 kHz、单声道、16 bit PCM。

## 说话人对清单

支持 `pairs` 统一数组，也支持 `same_speaker_pairs` / `cross_speaker_pairs` 分组。对结果始终按清单原始顺序输出。

```json
{
  "speakers": {
    "speaker_a": ["speaker_a/a1.wav", "speaker_a/a2.wav"]
  },
  "same_speaker_pairs": [
    {"id": "same-a-01", "left": "speaker_a/a1.wav", "right": "speaker_a/a2.wav"}
  ],
  "cross_speaker_pairs": [
    {"id": "cross-ab-01", "left": "speaker_a/a1.wav", "right": "speaker_b/b1.wav"}
  ]
}
```

## 异常注入清单

示例中的异常是实际写入输入目录的损坏 WAV 或静音 WAV，不是仅写在报告里的标记。流水线检测后把 `id` 记入 `anomalies.hit_ids`。

```json
{
  "injections": [
    {
      "id": "INJ-RATE-001",
      "sample": "defects/bad_rate.wav",
      "stage": "audio",
      "kind": "wrong_sample_rate",
      "expected_error": "E_AUDIO_FORMAT"
    }
  ]
}
```

当前示例覆盖：错误采样率、错误声道数、错误位深、RIFF 数据长度越界、有效帧不足。

## 流水线

1. **音频与特征**：严格解析 RIFF/WAVE 的 `RIFF`、`fmt `、`data` 块；校验文件长度、块长度、PCM tag、采样率、声道、位深、`block_align`、`byte_rate` 和整帧数据长度。帧切分采用“全部完整帧 + 至多一个补零尾帧”。逐帧计算自然对数能量、过零率、自相关基频、Mel 能量和 DCT 倒谱。所有浮点中间量使用 `float64`，写出前按配置有效位数舍入。
2. **确定性汇聚**：按能量门限过滤帧，剩余帧以帧序号升序累加。输出基频、对数能量和每个倒谱系数的均值、方差、P10/P50/P90。分位数使用精确有理数的 type-7 线性插值。样本带参与帧索引、区间、统计指纹和累加范围风险信息。
3. **定点嵌入**：统计量以当前 Q 位宽进入整数投影；矩阵由投影配置指纹派生固定种子，并用自实现 SplitMix64 生成，避免依赖 NumPy RNG 版本。投影、裁剪、档位编码、平方范数和整数开方单位化均为整数运算。报告保留逐维投影整数、档位码、舍入残差、残差绝对值和、矩阵指纹和 int64 风险界。
4. **定点相似度**：用整数点积与 Q16 整数平方根计算范数，再按 `similarity_scale` 半远离零舍入。同时输出双精度参考余弦。恰等于阈值时固定判为 `SAME_SPEAKER` 并标记边界；多对相似度定点值相同时同样标记边界。
5. **误差追踪**：每条样本用工作位宽和 `fractional_bits + precision_trace_bit_step` 完整重算特征量化、定点汇聚、投影量化和相似度。报告逐项给出低/高精度值、绝对差、特征各字段最坏帧及完整逐帧差值、各统计项差值、各嵌入维差值和各对相似度差值；按“特征→汇聚→量化→相似度”顺序标出首次超差环节。若低/高精度判定相反，或嵌入级参考判定与定点相似度判定相反，记录 `E_PRECISION_SENSITIVE`。误差来源区分 `quantization_rounding` 与 `fixed_point_accumulation_overflow_risk`。

### 基频峰选择

先在配置的 `[f0_min_hz, f0_max_hz]` 范围内计算归一化自相关。为避免包络造成的周期倍增，选择“不低于全局峰 78% 的最早局部峰”；若多个峰完全并列，仍取较小滞后。该规则保持确定性，并在示例中把三个合成说话人的基频统计稳定分开。

### 定点规则

- 浮点转整数：半远离零。
- 均值：整数和除以帧数后半远离零。
- 方差：`sum((x_q - mean_q)^2) / (count * 2^Q)` 后半远离零。
- 分位数：精确 type-7 有理数插值，无浮点累加。
- 范数：`isqrt(sum(x^2) << 32)` 得到 Q16 范数。
- 余弦：`dot * similarity_scale * 2^32 / (norm_a * norm_b)` 后半远离零。
- 所有累加均为 Python 任意精度整数；同时计算 int64 最坏界并报告是否具有溢出风险。

## 缓存键与精确失效

各阶段缓存键均由上游产物键、阶段配置指纹和位宽/档位派生：

```text
feature_key     = H(audio_sha256, feature_config, Q)
aggregate_key   = H(feature_key, aggregation_config, Q)
embedding_key   = H(aggregate_key, embedding_config, Q, quantization_levels)
similarity_key  = H(embedding_key_left, embedding_key_right, pair_spec, similarity_config, Q)
```

因此：

- 修改相似度阈值：只失效所有 `similarity` 产物。
- 修改量化档位：只失效 `embedding` 及 `similarity`。
- 修改 Mel 参数：失效特征及全部下游。
- 替换一个音频：只失效该内容对应的特征、汇聚、嵌入，以及引用该嵌入的比对对。
- 替换后的音频若与另一已有样本内容完全相同，可合法复用后者的内容键产物；这是内容寻址去重，不是漏算。

报告 `incremental.reused`、`recomputed`、`invalidated` 逐项列出缓存键、阶段、标签与产物哈希。

## 并发与确定性

特征提取、汇聚、嵌入和对级相似度均在 `ThreadPoolExecutor` 中执行，最大并发数取自配置。任务按稳定键提交，结果按样本名升序、帧序号升序、对清单原序收集。JSON 使用排序键和规范化序列化。

规范结果指纹 `result_fingerprint` 覆盖样本结果、对结果、失败清单、边界、误差追踪和异常命中，不覆盖耗时、缓存动作和恢复元数据。因此同一计算配置在并发度 1/4 下 `deterministic_results` 与 `result_fingerprint` 完全一致；报告中的 `stage_timings_ms` 与缓存复用/重算列表属于执行元数据，天然可能随调度和起始缓存状态变化。

## 快照、恢复与信号

- 每个缓存产物与每个阶段清单都用同目录临时文件写完、`fsync` 后 `os.replace` 整体替换。
- `state/state.json` 记录全局版本、完整配置哈希、兼容配置哈希、异常清单哈希、已完成阶段、各阶段游标、产物版本和耗时。
- 未完成快照仅允许在兼容配置哈希与异常清单哈希一致时续跑；否则抛出 `E_CONFIG_MISMATCH`，CLI 退出码 `3`，且不覆盖旧快照。
- 已完成快照可通过内容键安全执行增量编辑；并发数和快照间隔属于运行参数，不参与续跑兼容哈希。
- 收到 `SIGINT`/`SIGTERM` 时设置停止标志，完成当前阶段所有任务和原子提交后以退出码 `0` 退出；不生成完整报告，只留下可恢复快照。
- 强制终止时只信任已经完整替换的缓存文件和阶段快照；启动时校验缓存 payload、`state_sha256` 和 `stage_snapshot_sha256`，再扫描缓存重建可用索引。

## 报告结构

`report.json` 主要字段：

| 字段 | 内容 |
| --- | --- |
| `config`, `inputs` | 规范化配置及配置/对清单/异常清单哈希。 |
| `deterministic_results.samples` | 样本格式、逐帧特征摘要、汇聚统计、帧区间、嵌入整数/残差/风险、缓存键和失败码。 |
| `deterministic_results.pairs` | 定点/参考相似度、阈值、判定、边界、正确性、双方嵌入键及双方三阶段样本缓存键。 |
| `deterministic_results.precision_trace` | 每条样本四环节逐项差值、首次超差环节、误差来源和精度敏感事件。 |
| `deterministic_results.boundary_pairs` | 阈值相等或定点相似度并列的对。 |
| `deterministic_results.anomalies` | 注入清单命中编号、命中的失败项和未命中项。 |
| `deterministic_results.failures` | 稳定排序的格式、有效帧、样本不可用和精度敏感失败清单。 |
| `deterministic_results.summary` | 同人/跨人对数、误接收、漏判、边界、精度敏感和故障统计。 |
| `incremental` | 复用、重算、失效逐项清单。 |
| `stage_timings_ms` | 各阶段实际耗时。 |
| `result_fingerprint` | 规范结果内容指纹。 |

CLI 退出码：完整运行或受控中断为 `0`，输入/配置错误为 `2`，`E_CONFIG_MISMATCH` 为 `3`。样本级格式或帧数故障仍会生成完整诊断报告，因此不改变进程退出码。

## 测试与验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```

11 个集成测试覆盖真实损坏 WAV、特征可重复性、相似度质量、阈值边界、精度敏感、位宽/量化档位差异、并发一致、阈值/Mel/单样本替换的增量失效、无状态缓存恢复、受控停止、未完成快照续跑和配置不匹配。实际结果见 `VERIFICATION.md`。

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `voiceprint_diag/audio.py` | RIFF/WAVE 严格校验与 PCM 解码。 |
| `voiceprint_diag/features.py` | 切帧、能量、过零率、基频、Mel/DCT 特征。 |
| `voiceprint_diag/aggregation.py` | 定点统计汇聚与分位数。 |
| `voiceprint_diag/fixedpoint.py` | Q 格式、舍入、整数范数、余弦。 |
| `voiceprint_diag/embedding.py` | 固定种子矩阵、量化、残差、单位化。 |
| `voiceprint_diag/similarity.py` | 对级余弦、阈值和参考值。 |
| `voiceprint_diag/pipeline.py` | 并发编排、误差追踪、报告。 |
| `voiceprint_diag/cache.py` | 内容寻址缓存与复用/重算审计。 |
| `voiceprint_diag/snapshots.py` | 原子阶段快照、恢复判定。 |
| `voiceprint_diag/synthesize.py` | 固定种子样本和示例清单生成。 |
| `tests/test_pipeline.py` | 端到端与增量/恢复测试。 |

## 边界与说明

- 这是可集成声纹编码与定点诊断组件，不是训练好的深度说话人模型；投影矩阵由配置确定，适合验证数值路径、缓存和一致性，不用于生产识别精度宣称。
- 示例使用合成基频/共振峰/包络/平稳噪声，不代表真实录音鲁棒性。
- 当前实际执行环境为 WSL/Linux；代码只使用 Python 标准库的跨平台原子替换和 NumPy，Windows 命令已提供，但本仓库记录的实际运行结果来自 Linux。
