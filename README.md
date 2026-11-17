# 确定性 TTS 声学渲染与编辑工作台

这是一个仅依赖本地 CPU、NumPy 与 soundfile 的离线 TTS 声学特征渲染工作台。它面向固定种子生成的 16 kHz、单声道、16 bit PCM 样本，提供：

- 音素级四子状态状态机：清浊判决、基频轨迹、谱包络、能量归一化。
- 整数谐波叠加、定点能量缩放、确定性舍入与帧移重叠相加。
- 基于音频/标注/配置/编辑历史指纹的逐帧缓存。
- 编辑后的局部失效与精确复用，包括纯时长变更和参数覆盖的不同失效语义。
- 临时文件 + `os.replace` 原子快照，支持 SIGTERM/SIGINT 可处理退出和强退恢复。
- 固定排序键的线程池执行，保证并发度不影响最终 PCM、特征顺序和诊断顺序。
- WAV、NumPy 声学参数文件、JSON 诊断报告和业务码清单。

## 环境

要求 Python 3.10+、NumPy、soundfile；不需要网络服务、GPU、数据库或容器。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

若 Debian/Ubuntu 缺少 `ensurepip`，先安装 `python3-venv`，或使用官方 `get-pip.py` 初始化环境。

## 目录

```text
tts_workbench/
  inputs.py      输入扫描、WAV/标注校验、缺失特征推导
  config.py      声学配置校验、acoustic_hash/config_hash
  commands.py    JSONL 编辑命令、原子应用、帧区间编辑
  features.py    清浊/量化/频带/定点能量/确定性噪声
  engine.py      状态机编排、线程池、缓存、OLA、快照、报告
  snapshot.py    快照版本校验与原子持久化
scripts/
  generate_samples.py
samples/demo/    固定种子本地样本
tests/           集成与恢复验收测试
```

## 输入

输入目录示例：

```text
samples/demo/
  acoustic_config.json
  labels.jsonl
  edit_commands.jsonl
  generator_seed.json
  utt_alpha.wav
  utt_beta.wav
```

### acoustic_config.json

```json
{
  "sample_rate": 16000,
  "frame_length_ms": 20.0,
  "frame_shift_ms": 10.0,
  "f0_min_hz": 70.0,
  "f0_max_hz": 400.0,
  "f0_quantization_hz": 5.0,
  "spectral_bands": 16,
  "voicing_threshold": 0.5,
  "energy_target_dbfs": -18.0,
  "max_batch_frames": 12,
  "concurrency": 3,
  "snapshot_interval_frames": 16
}
```

校验包括：正整数采样率、正帧长/帧移、帧长样本数能被帧移样本数整除、合法基频范围/量化步长、频带数、清浊门限、能量目标、渲染批大小、快照间隔，以及 `concurrency > 0`。并发数非正不会被静默改成 1，而是输出 `E_CONFIG_INVALID`。

### labels.jsonl

每行一个 utterance：

```json
{
  "utterance_id": "utt_alpha",
  "wav": "utt_alpha.wav",
  "phonemes": ["AH", "N", "SIL", "IY"],
  "duration_frames": [16, 10, 6, 16],
  "f0_hz": [138.0, 116.0, 0.0, 182.0],
  "energy": [0.80, 0.58, 0.03, 0.72],
  "noise_floor": [0.012, 0.030, 0.155, 0.018],
  "voicing_score": [0.94, 0.78, 0.06, 0.91]
}
```

- `phonemes` 与 `duration_frames` 长度必须一致，后者必须是正整数。
- 后四个逐音素数组可省略；省略时从对应参考音频片段估计基频、能量、噪声底和清浊分数。
- utterance ID 全局唯一；缺少 WAV、重复 ID、长度不一致或 JSON 损坏进入 `E_LABEL_INVALID` 失败项。
- WAV 必须是 RIFF/WAVE、PCM_16、mono、配置采样率，否则进入 `E_AUDIO_FORMAT`。
- 合法 utterance 不受其他失败项影响，继续渲染。

### edit_commands.jsonl

每行一条命令。`seq` 是提交序号，命令按序号升序严格应用，相同序号再次出现时记为 `skipped_duplicate`。

```json
{"seq": 1, "utterance_id": "utt_alpha", "phoneme_index": 1, "op": "set_f0_scale", "value": 1.04}
{"seq": 2, "utterance_id": "utt_alpha", "phoneme_index": 1, "op": "set_duration", "duration_frames": 14}
{"seq": 3, "utterance_id": "utt_beta", "phoneme_index": 2, "op": "set_energy_scale", "value": 1.10}
```

支持的操作：

| op | 字段 | 语义 |
| --- | --- | --- |
| `set_duration` | `duration_frames` | 改变音素帧数；纯时长变更 |
| `set_f0_scale` | `value` | 在当前音素基频缩放上累乘 |
| `set_energy_scale` | `value` | 在当前能量缩放上累乘 |
| `override_phoneme` | `f0_hz`/`energy`/`noise_floor`/`voicing_score` 的任意子集 | 参数覆盖 |
| `set_voicing_score` | `value` | 覆盖清浊分数 |
| `replace_frames` | `start_frame`,`end_frame`,`frames`,`frame_count?` | 替换半开区间 `[start,end)` |
| `insert_frames` | `start_frame`,`frames`,`frame_count?` | 在指定帧位置插入 |
| `delete_frames` | `start_frame`,`end_frame` | 删除半开区间 |

局部帧操作的帧索引均相对当前音素。`frames` 中每项可包含 `f0_hz`、`energy`、`noise_floor`、`voicing_score`、`voiced`，缺省字段继承当前位置基准值。区间越界、插入声明帧数不一致、删除后帧数为零等均输出 `E_RANGE_OUT_OF_BOUND`，且命令不会改变已有状态。

会话中可反复提交新命令：

```bash
# 第一次渲染，应用输入目录内的 edit_commands.jsonl
python -m tts_workbench render --input samples/demo --work runs/demo

# 后续会话追加一个命令文件；快照中的旧序号自动跳过
python -m tts_workbench render \
  --input samples/demo \
  --work runs/demo \
  --commands new_edits.jsonl
```

`--commands` 可重复。`--reset` 会忽略已有快照并从头构建，但仍使用当前输入目录与追加命令。

## 确定性算法

每个音素依次完成以下状态，并在报告中记录转移条件和特征摘要：

1. `VOICING_DECISION`：`voicing_score >= threshold` 判为浊音；相等固定判为浊音；`f0_raw <= 0` 强制清音。每帧比较结果显式写入报告。
2. `F0_TRACK`：以音素基准基频和固定帧相位锚点线性内插，再按 `[f0_min_hz, f0_max_hz]` 与量化步长离散化。量化使用整数格点、`floor(x + 0.5)`，并显式记录上下界钳制。
3. `SPECTRAL_ENVELOPE`：按整数谐波 `1..spectral_bands` 生成频带幅度；频率超过 Nyquist 的谐波置零。
4. `ENERGY_NORMALIZE`：波形先转 Q15 整数，整数 RMS 到目标 RMS 的增益使用 Q16 定点；舍入为对称 half-up。目标 dBFS 由配置目标与逐帧能量共同决定。

帧拼接使用长度 `frame_length_samples`、步长 `frame_shift_samples` 的重叠相加，并按每样本覆盖次数做确定性除以。输出长度是 `(总帧数-1)*帧移 + 帧长`。

噪声不是随机数生成器输出，而是由音素、帧起点、量化基频和采样位置派生的 counter-based 整数哈希噪声。执行相关的并发数、批大小和快照间隔不参与 `acoustic_hash`，因此不会改变噪声种子或 PCM。

## 缓存与失效

缓存包括：

- 帧特征键：声学配置、参考音频帧片段 SHA-256、音素内容、基频编辑历史、量化基频、帧位置。
- 能量帧键：帧特征键、能量编辑历史、目标 dBFS、采样位置和定点舍入方案。
- 音素键：标签/音频/配置指纹、音素序号、逐帧键、各子状态编辑摘要。
- utterance 键和帧区间键：由 utterance 指纹、音素帧键、能量键和命令摘要派生。
- 拼接键：稳定排序后的逐帧能量键与 PCM 帧哈希。

纯时长变更复用未变化帧的 F0/谱包络；受影响音素与后续时长位移音素只失效能量归一化/拼接（局部帧内容直接变化时还会失效谱包络）。基频或能量参数覆盖分别使对应子状态及下游拼接失效。报告中的 `invalidations`、`reuse_ranges` 和每条命令的 `observed_reuse_and_invalidation` 给出实际范围。

## 快照、恢复与退出

阶段：

1. `load_validate`
2. `phoneme_features`
3. `frame_concat`
4. `writeback`
5. `complete`

`work/state.json` 含快照 schema 版本、engine 版本、配置哈希、标注哈希、命令游标、已完成音素、子状态位置、部分特征摘要和缓存。每次写入先在同目录写临时文件、`fsync`，再 `os.replace` 原子替换。缓存与快照一起提交，避免“特征已算但快照未提交”的不一致。

重启时先校验快照版本和配置哈希：

- 版本或配置不一致：输出 `E_CONFIG_MISMATCH`，退出码 78，拒绝续跑，且不覆盖已有快照和输出。
- 配置一致、标注哈希变化：丢弃旧缓存并从标签重新构建。
- 配置和标签一致：复用快照缓存，从已提交子状态/音素继续。

CLI 安装 SIGTERM/SIGINT 处理器。收到信号后停止提交新任务，等待当前线程批次完成，写入完整快照并以退出码 0 结束；可处理的退出不承诺必须是完整渲染状态。强制终止（例如 SIGKILL 或测试中的 `os._exit`）后，只使用最后一次完整替换的快照恢复，且不保证存在退出报告。

## 输出

每次完整运行写：

```text
work/
  state.json
  report.json
  output/
    utt_alpha.wav            可回放 PCM_16 WAV
    utt_alpha.npz            逐帧 f0、voiced、频带幅度、能量、PCM、缓存键
    utt_alpha.params.json    参数摘要与各类 SHA-256
    utt_beta.wav
    utt_beta.npz
    utt_beta.params.json
```

`report.json` 包含：

- 每个 utterance 的排序键、音素级四子状态轨迹、逐帧比较结果和中间特征摘要。
- 清浊统计、量化基频分布、缓存命中/重算统计和稳定任务顺序。
- `invalidations`、`reuse_ranges`、命令应用记录、恢复点与快照阶段。
- `failures`、`business_codes` 和 `business_code_summary`。

业务码：

| code | 含义 |
| --- | --- |
| `E_AUDIO_FORMAT` | WAV 容器/格式/采样率/声道/位深/长度不合规 |
| `E_LABEL_INVALID` | 标注 JSONL、ID、时长、数组长度或 WAV 匹配不合规 |
| `E_CONFIG_INVALID` | 声学配置不合规 |
| `E_CONFIG_MISMATCH` | 快照版本或配置哈希不匹配，拒绝续跑 |
| `E_RANGE_OUT_OF_BOUND` | 局部帧区间、帧数声明或删除后长度不合规 |
| `E_COMMAND_INVALID` | 其他命令结构/参数错误 |
| `E_COMMAND_ORDER` | 新命令序号早于已处理游标 |

## 样本生成

```bash
python scripts/generate_samples.py --out samples/demo --seed 20261117
```

固定种子实际保存在 `samples/demo/generator_seed.json`。样本脚本使用 32-bit LCG 和确定性谐波叠加，不使用 `random` 模块。

## 测试与实测结果

```bash
python -m pytest -q
```

当前自动化验收共 13 项，覆盖：

- 全量渲染、二次全缓存复用、重复命令幂等。
- `concurrency=1` 与 `concurrency=6` 的 PCM/声学参数逐样本一致。
- 量化档位改变基频离散值及 PCM。
- 清浊门限同分固定判浊。
- 能量/基频/时长编辑的局部失效、前序复用和与从零全量渲染等价。
- 越界替换、插入帧数不一致、删除至零帧的 `E_RANGE_OUT_OF_BOUND` 与状态不变。
- 音频格式、标注、配置、快照版本/配置不一致业务码和部分成功处理。
- `os._exit` 强退后原子快照恢复，与不中断全量结果逐样本一致。
- 真实 SIGTERM 在完成当前任务批次并写快照后退出码 0，恢复后与全量一致。
- “编辑 → 强退 → 恢复 → 再编辑”与一次性全量运行结果逐样本一致。

实测运行（本机 Python 3.11 / NumPy 2.4.6 / soundfile 0.14.0）：

```text
$ .venv/bin/python -m pytest -q
............. [100%]
13 passed

$ .venv/bin/python -m tts_workbench render --input samples/demo --work runs/smoke
{"business_codes": [], "exit_code": 0, "failures": 0, "status": "complete", ...}

# 同一 work 再次运行
feature_hits=111, feature_misses=0,
energy_hits=111, energy_misses=0,
concat_hits=2, concat_misses=0,
phonemes_reused=9, phonemes_recomputed=0
```

具体断言见 `tests/`；测试全部使用本地样本和临时目录，不访问网络服务或外部账号。
