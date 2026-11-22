# 本地情感分档引擎

面向客服通话质检的确定性情感分档实验工程。工程不依赖真实数据集、外部服务、GPU、Docker 或容器环境；所有 PCM、逐帧真值和对话清单均由固定种子在本地生成。

核心约束是：**流式分块增量视图与离线全量视图必须逐帧一致**。两条路径共享帧切分、声学打分、门限映射、因果滑窗和平局规则；流式检查点每次从已到达帧完整重算当前窗口，不使用近似累计器。

## 环境与快速开始

目标环境为 Windows + Python 3.11 + NumPy：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

python -m emotion_engine generate
python -m emotion_engine offline --dialog-id dialog_0001 --json-out reports/offline_dialog_0001.json
python -m emotion_engine stream  --dialog-id dialog_0001 --json-out reports/stream_dialog_0001.json
python -m emotion_engine smoke
python -m unittest discover -s tests -v
```

默认路径已固定：

| 用途 | 默认路径 |
| --- | --- |
| 配置 | `config/default.json` |
| 对话清单 | `samples/dialogs.jsonl` |
| 说话人参数 | `samples/speakers.jsonl` |
| PCM | `samples/dialog_0001.wav`、`samples/dialog_0002.wav` |
| 逐帧真值 | `samples/truth/dialog_0001.jsonl`、`samples/truth/dialog_0002.jsonl` |
| 烟雾报告 | `reports/smoke_report.json` |

`smoke` 在缺少样本时会先执行确定性生成，随后完成离线、流式、缓存、快照及异常码检查。成功时退出码为 0，失败时为 1，业务异常命令为 2。

最小 API 用法：

```python
from emotion_engine import OfflineEngine, StreamingEngine, load_config

config = load_config("config/default.json")
offline_view = OfflineEngine(config).analyze_pcm(
    pcm, sample_rate, frame_speaker_ids, declared_speakers, verify_recounts=True
)

stream = StreamingEngine(manifest_entry, config)
stream.submit_chunk(sequence=0, start_ms=0, pcm=chunk_pcm, speaker_ids=chunk_frame_speakers)
current_view = stream.checkpoint()
```

## 目录

```text
config/default.json          固定配置、门限策略、滑窗语义、tier_schema
emotion_engine/synth.py      固定种子多说话人 PCM/真值/清单合成
emotion_engine/features.py   样本级帧边界、能量/ZCR/F0、valence/arousal
emotion_engine/tiering.py    严格/容差带门限、滞回、次正规数保护、平局规则
emotion_engine/windows.py    因果尾随滑窗及单帧反算
emotion_engine/segments.py   说话人连续段、加权汇总、独立重算
emotion_engine/aggregation.py 两条路径共用的完整视图构造与逐帧对账
emotion_engine/batch.py      离线全量批处理
emotion_engine/streaming.py  chunk 到达、检查点、延迟观测、流式异常码
emotion_engine/cache.py      内容寻址缓存与选择性失效区间
emotion_engine/snapshot.py   版本化整体替换快照
emotion_engine/cli.py        CLI 与可复现烟雾检查
tests/test_engine.py         标准库 unittest 验收测试
```

## 确定性样本契约

`generate` 使用 `numpy.random.default_rng(seed)` 为每个说话人生成基频、三组共振峰及带宽、振动相位、音节包络相位和基础情感值。轮次时间轴与增益显式写在 `synth.py`，重叠时段按源信号叠加后整体归一化，输出 16kHz、单声道、16-bit PCM WAV。

生成两个对话：

- `dialog_0001`：1800ms，80ms chunk，90 个 20ms 帧，最后一次 chunk 仅 40ms。
- `dialog_0002`：1560ms，60ms chunk，78 个 20ms 帧。

逐帧真值字段严格为 `frame_index`、`speaker_id`、`valence`、`arousal`、`is_speech`。JSONL 按 frame_index 顺序输出。对话清单字段包含 `dialog_id`、`sample_rate`、`duration_ms`、`chunk_ms`、`tier_schema`，并附加 `speakers`、样本路径、种子和样本数。

相同种子、配置和输出路径下，WAV、真值与清单内容稳定；测试会比较文件 SHA-256。

## 计算口径

### 帧切分

帧边界全部使用整数样本索引：

```text
start_sample = frame_index * hop_samples
end_sample   = start_sample + frame_samples
```

默认 `frame_ms = hop_ms = 20`，16kHz 下均为 320 样本。只提交完整帧；不足一帧的尾部样本被忽略并记录在 `audio.ignored_tail_samples`。生成样本没有尾部残片。

### 帧级打分

所有浮点归约使用 `math.fsum` 的显式有序求和，随后统一按配置的 9 位小数取整。非有限值和绝对值小于 `2.2250738585072014e-308` 的次正规值被稳定置零。

- 能量：帧内 RMS 与 dBFS。
- 过零率：相邻样本符号位变化数除以 `N-1`。
- 基频：70–400Hz 滞后范围内做归一化自相关搜索；搜索按滞后升序，严格 `>` 才替换，平局保留较短滞后。
- 归一化能量、F0、ZCR 分量映射到 `[-1, 1]`。
- `valence = 0.45*E + 0.40*F - 0.15*Z`。
- `arousal = 0.55*E + 0.25*F + 0.20*Z`。

`tier_schema` 用 valence 两个阈值和 arousal 一个阈值形成 6 个档位：`angry`、`frustrated`、`tense`、`calm`、`excited`、`satisfied`。

### 门限稳定策略

- `strict`：直接在阈值上分类。valence 区间为 `(-inf,low)`、`[low,high)`、`[high,inf)`；arousal 为 `[threshold,inf)` 进入 high。
- `tolerance`：值落入任一阈值的容差带时，保留上一帧同轴 band；首帧无历史时按 strict 分类。
- 次正规数保护在打分、归一化、均值和窗口汇总处统一生效。
- 记录 `near_threshold_frames`、`threshold_stays`、`instant_tier_switch_count` 与最终 `tier_switch_count`。
- 窗口主导档位计数平局时，严格按 `tier_schema.tier_labels` 顺序取最早者。

### 滑窗

窗口语义在代码和报告中都显式声明为：

```text
name = causal_trailing
window = [max(0, frame_index - window_length + 1), frame_index]
padding_frames = 0
insufficient_policy = use_available_frames_no_fill
window_contains_future_frames = false
```

窗口不填充、不看未来、不截断尾帧。前 `window_length-1` 帧使用实际可达帧数并标记 `insufficient_window = true`。窗口 valence/arousal 为帧级分数的算术平均，档位为窗口内 `instant_tier` 的多数票。`recount_window` 可从任意单帧重算并逐字段比较，偏差返回 `E_WINDOW_RECOUNT`。

### 说话人段

逐帧标签必须属于清单 `speakers` 集合，否则返回 `E_SPEAKER_LABEL_INVALID`。段边界只允许出现在相邻帧 speaker 变化处；不一致返回 `E_SEGMENT_BOUNDARY` 及 `first_deviating_frame`。

段级 valence/arousal 使用帧权重 `energy_rms`（下限 `1e-9`）加权：

```text
segment_mean = fsum(value_i * weight_i) / fsum(weight_i)
```

段级汇总可由逐帧明细和 `frame_weight` 独立重算。段主导档位同样按 schema 顺序破平局。

## 离线与流式

离线 `OfflineEngine` 对整段 PCM 计算全部完整帧后通过 `build_view` 构造视图。

流式 `StreamingEngine` 按 `chunk_ms` 接收 PCM 和该 chunk 内每个帧起点的 speaker 标签。每次检查点都从所有已到达帧重新调用同一个 `build_view` 和 `compute_window`，并与同一帧前缀的离线重建视图比较。发现差异立即返回 `E_STREAM_BATCH_MISMATCH`，明细包含首个偏差帧、两条路径的帧值和 `stream_stats`/`batch_stats`。

可查询当前视图即触发版本化快照提交。逐帧结果、段级汇总和窗口统计分别拥有同一检查点版本号及独立 SHA-256 内容校验和。

### 延迟观测

每个 chunk 使用本机 `time.perf_counter_ns()` 从处理入口计时到检查点视图可查询为止。报告记录：

- `observed_processing_ns` / `observed_processing_ms`
- chunk 帧范围、窗口长度、说话人数
- 按帧数、`frames*window_length`、`frames*speaker_count` 计算的估算记录

估算明确标记 `estimate_only = true`、`used_for_timeout = false`、`timeout_decision = "not_applicable"`，不参与任何超时或验收判定。

### 分块异常

| 场景 | 业务码 |
| --- | --- |
| 时间戳倒挂或与已接收样本不一致 | `E_CHUNK_ORDER` |
| 序号缺号、重复、乱序 | `E_CHUNK_GAP` |
| chunk 越出对话时长、非 hop 对齐或超过 `chunk_ms` | `E_CHUNK_RANGE` |

## 内容寻址缓存

内存缓存分两层，所有 key 由规范 JSON 的 SHA-256 生成。

特征 key 包含：

- PCM 内容哈希（不含 WAV 头）
- 采样率、帧长、帧移
- 特征参数版本

派生视图 key 还包含：

- speaker 序列哈希
- 滑窗参数与 `window_schema_version`
- 门限策略、容差、次正规数保护
- 档位算法版本、tier schema 版本及 schema 内容哈希

同 key 直接复用完整视图。派生 key 改变时保留 PCM 特征层，只重算实际受影响的下游帧：门限变化通过比较新旧 instant tier 确定变化帧，并将其影响范围扩展到包含这些帧的窗口；窗口长度变化只重算窗口起点实际变化的帧；tier schema/tie 规则变化才扩大到窗口全集。只替换与受影响帧区间相交的段级汇总。

每次输出 `recomputed_frame_intervals`、`reused_frame_intervals`、`recomputed_segment_ids`，并断言：

- 重算区间之间已合并；
- 重算与复用区间无交集；
- 两者并集覆盖 `[0, frame_count-1]`。

## 快照

`SnapshotStore` 只接受 `version > current_version` 的整体替换。检查快照内容 SHA-256：

- 版本回退或同版本覆盖：`E_SNAPSHOT_ROLLBACK`
- 内容与校验和不一致：`E_SNAPSHOT_CORRUPT`

自检对账流式与离线前缀的帧集合、段边界和最终档位切换序列，并验证三类快照版本及校验和。

## 业务码

| 业务码 | 含义 |
| --- | --- |
| `OK` | 成功 |
| `E_CONFIG_INVALID` | 配置非法，`details.field` 指明字段 |
| `E_STREAM_BATCH_MISMATCH` | 流式与离线前缀不一致，含首个偏差帧与两侧统计 |
| `E_WINDOW_RECOUNT` | 单帧窗口反算与快照不一致 |
| `E_SPEAKER_LABEL_INVALID` | 说话人标签越界或缺标签 |
| `E_SEGMENT_BOUNDARY` | 段边界与标签切换点不一致 |
| `E_CHUNK_ORDER` | chunk 时间戳/位置异常 |
| `E_CHUNK_GAP` | chunk 序号缺号、重复或乱序 |
| `E_CHUNK_RANGE` | chunk 越界或未对齐 |
| `E_SNAPSHOT_ROLLBACK` | 尝试回退或同版本覆盖 |
| `E_SNAPSHOT_CORRUPT` | 快照内容校验失败 |

所有业务异常均可通过 `EngineError.to_dict()` 输出 `business_code`、`message` 和结构化 `details`。

## JSON 报告

`reports/smoke_report.json` 包含：

- `business_code`、`business_codes`、`degraded`
- `summary`、`offline_summary`、`stream_summary`
- `frames`：逐帧分数、instant/final 档位、所属窗口
- `segments`：段级汇总
- `latency_observations`：真实处理耗时与估算记录
- `deviations`：流式/离线偏差明细
- `checks`：17 项烟雾检查及实际业务码
- `snapshot_metadata`、`self_check`、双重内容校验和

## 实际验证记录

当前工作区实际执行结果（2026-11-23，Linux/WSL，Python 3.10.12，NumPy 1.26.4；代码兼容目标 Python 3.11）：

```text
python -m unittest discover -s tests -v
Ran 9 tests in 10.078s
OK

python -m emotion_engine smoke
{"all_checks_passed": true, "business_code": "OK", "check_count": 17,
 "report": "reports/smoke_report.json"}
```

实际覆盖：

- 单元测试对 `dialog_0001` 与 `dialog_0002` 均完成逐块流式检查点与离线前缀逐帧一致比较。
- 烟雾检查对 `dialog_0001` 执行 23 个 chunk、90 帧、4 个说话人连续段、快照版本 23。
- 离线重复运行帧内容 SHA-256 一致，第二次缓存模式为 `full_reuse`。
- 切换为 `strict` 门限后特征层命中，只重算帧区间 `[61,70]` 和 `seg_0002`，复用 `[0,60]` 与 `[71,89]`，区间覆盖全帧且无交集。
- 窗口/段级逐帧重算全部通过。
- 非法门限策略、标签越界、段边界错误、窗口反算错误、分块乱序/缺号/越界、快照回退/损坏均返回预期业务码。
- 人为篡改流式帧后，`E_STREAM_BATCH_MISMATCH` 返回首个偏差帧及流式/离线统计。
- 两次独立离线 CLI 报告逐字节相同（SHA-256 均为 `e1dd4e40e5725722eda19fc85388f25f97b14b28d9beab0b74cba7e90c6a6e23`）。

当前样本摘要：

```text
dialogs.jsonl SHA-256       0cc297b9d867dab787603c3e676b103c17812029e2183e8197deb3335644e234
dialog_0001.wav SHA-256     7d848a3fd35050ebb8f49a46f93dbb5918831a4064bddcf6a4f88ec6149d1172
dialog_0001 truth SHA-256   081e8c995c5e9dc5e9ce68de3a58d207eefa314d7195b8af055a0ec8676e1930
frame checksum              923ddab04fd4258edf307e6b82c832b59ddd854bf7f3b44428e040ca0d4beae0
segment checksum            a3c60744e64d929acbac517d3f417c5efe683eb5d2278534eed95bf2d587919d
```

注意：延迟数值随机器负载变化，属于观测数据；逐帧分数、档位、段边界及内容校验和不依赖系统时间。
