# 本地清唱音高与歌词对齐反馈工具

本项目从单声道清唱 WAV 出发，生成逐帧基频、音符切分、歌词音素对齐、音节映射、参考音高评分，以及可供程序批量读取和人工讲评的反馈报告。实现只依赖 Python 3 与 NumPy，不使用网络、外部模型或 GPU。

## 快速开始

```bash
python3 -m pip install -r requirements.txt

# 生成确定性样本、参考音高与逐帧/逐音素手工标注
python3 scripts/generate_samples.py --output samples

# 处理单个样本
python3 -m vocal_feedback \
  --input samples/s01_ballad \
  --output reports/s01 \
  --config samples/analysis_config.json

# 处理整个样本目录
python3 -m vocal_feedback \
  --input samples \
  --output reports/batch \
  --config samples/analysis_config.json

# 用独立手工标注复核已有报告
python3 scripts/evaluate.py \
  --report reports/batch \
  --config samples/analysis_config.json \
  --output reports/batch/evaluation_summary.json

# 自动化测试
python3 -m unittest discover -v
```

也可安装为命令行程序：

```bash
python3 -m pip install -e .
vocal-feedback --input samples --output reports/batch --config samples/analysis_config.json
```

## 输入约定

单个样本目录推荐包含 `sample.json`：

```json
{
  "id": "s01_ballad",
  "audio": "audio.wav",
  "lyrics": "lyrics.txt",
  "reference": "reference.json",
  "annotations": "annotations.json"
}
```

- `audio.wav`：单声道未压缩 PCM WAV；立体声会明确报错。
- `lyrics.txt`：UTF-8 歌词，每行一句。中文按单字音节处理，标点忽略。
- `reference.json`：音名加时间区间，例如
  `{"start_s": 0.32, "end_s": 0.80, "pitch": "D4"}`；
  也接受 CSV/TSV 的 `start,end,pitch` 列。
- `annotations.json`：可选。提供逐帧基频、音符区间和音素区间后，主流程自动生成验收指标。

无 `sample.json` 时，也会按 `audio.wav`、`lyrics.txt`、`reference.json`、`annotations.json` 或同名文件自动发现。所有路径均可通过 `--input`、`--wav`、`--lyrics`、`--reference`、`--annotations` 指定。

## 输出

每个样本输出到独立目录：

| 文件 | 内容 |
|---|---|
| `report.json` | 完整机器可读报告，含逐帧候选、原始/纠正/平滑轨迹、映射、评分、区间和验收结果 |
| `report.md` | 教师可读的逐句摘要、音符表、音节/音素表、低置信区间 |
| `frames.csv` | 逐帧时间、频率、MIDI、浊音、方法、选择理由、置信度、来源和能量 |
| `notes.csv` | 音符起止、代表音高、置信度、边界证据和歌词映射 |
| `phonemes.csv` | 每个音素的类别、起止、置信度与对齐代价 |
| `syllables.csv` | 音节、主音符、关联音符和音素 |
| `scoring.csv` | 参考音高、音分偏差、命中、偏高/偏低方向与节奏误差 |
| `unmapped_notes.csv` | 无法映射到歌词的音符及原因 |

批量模式还会写 `batch_summary.json`。清音帧在 JSON/CSV 中令 `f0_hz`、`midi` 为 `null`（CSV 为空），不使用 `0` 代替无音高。

## 核心处理

1. **双方法基频**：每帧同时计算归一化自相关候选与实倒谱候选。选择时结合谐波梳强度、谐波覆盖率、谱平坦度、低频/高频先验和周期置信度，并逐帧记录 `method`、`selection_reason`、两个候选及 `voicing_confidence`。浊音判决以自相关周期性为主要证据，避免把噪声型辅音当成倒谱基频。
2. **轨迹处理**：先只对两侧上下文一致的孤立八度跳变做 ±12 半音纠正，保留 `raw_f0_hz`/`raw_midi` 和 `octave_corrected_*`。再按局部音高变化率选择 3/5/7/9 帧中值窗口；快速滑音使用短窗口，连续滑音不会被压成单音。
3. **音符切分**：综合音高跃变、能量包络下凹和浊音度下降产生边界。短于配置最短时长的碎片按音高距离并入相邻音符。每个音符输出代表 MIDI、音分表示、置信度和边界证据。
4. **歌词映射**：先按时间顺序做音符到音节的单调分组。音符多于音节时允许拖腔，优先把无停顿、弱声学边界的连续音符并入同一音节。主音符取元音起始所在的音符，其余标为 `melisma`。映射后校验音节总数。
5. **音素对齐**：从音频能量上升与浊音起点确定辅音/元音边界；元音起点由音符浊音段锚定，辅音时长受上限约束，不会被拉伸到整个长音。对齐代价和置信度显式结合音素类别、浊音率、能量与过零率。
6. **评分**：参考音高按配置的半音容差（默认 50 音分）判命中，输出有符号音分、偏高/偏低/居中方向；根据音符起音误差与相邻起音间隔（IOI）误差计算节奏稳定性。
7. **低置信区间**：报告汇总低置信音帧、低置信音符、低置信音素和未映射音符的时间区间及原因，便于批量复核。

## 配置

配置为 JSON 对象，常用字段：

```json
{
  "frame_length_s": 0.040,
  "hop_length_s": 0.010,
  "fmin_hz": 70.0,
  "fmax_hz": 1000.0,
  "voicing_threshold": 0.34,
  "min_note_duration_s": 0.11,
  "note_pitch_jump_semitones": 1.15,
  "pitch_hit_cents": 50.0,
  "boundary_tolerance_s": 0.10
}
```

也可用 `--frame-length`、`--hop-length` 覆盖。未知配置键会报错，避免拼写错误静默生效。

## 确定性验收结果

仓库内样本由 `scripts/generate_samples.py` 固定随机种子生成，覆盖普通长音、颤音、高音区、低音区、滑音、跨音符拖腔和刻意偏高/偏低。独立手工标注复核结果：

| 样本 | 基频命中率 | 浊音 F1 | 音符边界 MAE | 音素边界 MAE | 音节映射 |
|---|---:|---:|---:|---:|---|
| `s01_ballad` | 100.0% | 0.984 | 10.14 ms | 9.28 ms | 一致 |
| `s02_high_melisma` | 100.0% | 0.986 | 10.30 ms | 9.28 ms | 一致 |
| `s03_low_range` | 100.0% | 0.985 | 9.22 ms | 8.53 ms | 一致 |

验收门槛在标注文件的 `tolerances` 中定义，默认要求基频命中 ≥ 90%、浊音 F1 ≥ 0.88、音符/音素边界 MAE ≤ 100 ms、音素边界命中率 ≥ 85%，且音节映射一致。

## 适用范围与限制

- 面向单声部、较干净、基频在配置范围内的清唱；强混响、多人齐唱或伴奏残留会降低浊音和音符边界质量。
- 内置离线中文发音表覆盖示例和常见练习字；表外汉字会退化为以字面作为元音标签。真实教学批量使用时可扩展 `vocal_feedback/lyrics.py` 中的 `_PINYIN`，无需外部服务。
- 参考音高是评分基准；滑音若本身偏离参考音高会成为真实评分偏差，工具不会为了“好听”自动掩盖。
- 强制对齐采用确定性的声学启发式与类别代价，不依赖训练模型。标注容差、低置信阈值和最短音符时长均可在配置中调整。

