# 技术设计

## 1. 目标与边界

系统处理本地确定性 16 kHz 单声道 PCM。每次分析是五节点串联 DAG；输入只有 PCM、样本清单、标注清单与应用配置。实现不做真实网络访问，不依赖音频设备，回放对象是“区间清单”及其 lineage，而不是音频波形播放。

## 2. 数据与确定性

### 2.1 合成

`meeting_vad/synth.py` 以 `numpy.random.default_rng(seed)` 生成固定底噪，语音段由确定性多谐波正弦、幅度调制和线性淡入淡出组成。段按显式时间轴拼接到 PCM。

关键约定：

- clip 种子 = `global_seed + seed_offset`。
- 噪声种子 = `clip_seed + noise_seed_offset + noise.seed_salt`。
- 量化：`round(clip(x,-1,1) * 32767)`，随后写 16-bit little-endian。
- 清单 JSONL 使用排序键和紧凑分隔符。

### 2.2 分帧

帧长和帧移均先转为样本数：

```text
frame_samples = round(sample_rate * frame_ms / 1000)
hop_samples   = round(sample_rate * frame_hop_ms / 1000)
frame_count   = ceil(total_samples / hop_samples)
```

帧起点为 `0, hop, 2*hop, ... < total_samples`，最后一帧不足帧长时零填充。每个帧记录精确 `start_sample/end_sample/valid_samples`。能量按固定帧长归一化；过零率只在实际有效样本上计算。平滑为边缘填充的居中移动平均。帧数先由 `duration_ms` 推算，若实际 PCM 样本数或帧数不符则抛 `E_FRAME_MISMATCH`。

### 2.3 活动初判

对平滑能量取 `noise_quantile` 分位作为底噪，构造：

```text
low_threshold  = noise_floor * low_multiplier
high_threshold = noise_floor * high_multiplier
```

能量高于高门限进入活动，低于低门限退出活动；间隔不超过 `min_silence_ms` 的候选合并。输出按起始样本升序，同起点时按起点、终点、候选 ID 稳定排序。

### 2.4 边界精修

在每个候选区间起止各 `window_ms` 样本范围内搜索帧能量斜率：

- 起点：选择窗口内最大正斜率帧起点。
- 终点：选择窗口内最小负斜率帧终点。
- 若斜率不足 `min_slope`，保留候选边界。
- 精修后必须满足 `abs(delta) <= window_samples`、`new_start < new_end`、不早于前一精修终点、不晚于下一候选起点。
- 若包住其他候选区间，抛 `E_REFINE_INVALID`，details 含受影响区间与允许边界。

### 2.5 说话人归属

每个精修区间计算窗口内平均能量与平均过零率，与受控标签模板计算归一化距离，得分 `1/(1+distance)`。若存在重叠标注，对应标签获得 `annotation_bonus`。排序键：

```text
(-score, speaker_label, segment_id)
```

因此同分先按标签字典序，再按区间标识升序。置信区间为 `score ± confidence_margin` 后裁剪到 `[0,1]`；标注也可显式提供上下界，非法顺序返回 `E_INTERVAL_INVALID`。

### 2.6 导出

导出项按 `(clip_id, start_sample, end_sample, segment_id)` 排序，JSONL 行使用：

```text
canonical_json(item) + "\n"
```

字段包含 clip、segment、候选 ID、样本与毫秒边界、说话人、标注版本、置信区间。没有时间戳或随机字段。

## 3. 缓存与快照

### 3.1 节点键

```text
payload_hash = SHA256(canonical_json(payload))
cache_key    = SHA256(canonical_json(node + version + key_details))
```

各节点键详情：

- `envelope`：每个 clip 的 PCM SHA-256、所有分块 SHA-256 及分块清单哈希、样本数、帧长/帧移样本数、平滑参数。
- `vad`：包络上游哈希、各 clip 曲线哈希、门限版本与完整门限参数。
- `refine`：VAD 上游哈希、候选 ID、门限版本/参数、精修参数。
- `speaker`：refine 上游哈希、标注内容哈希与最大修订号、门限版本/参数、受控标签、模板、bonus、margin。
- `export`：speaker 上游哈希、音频内容总哈希、帧移、门限版本/参数、标注哈希与修订号、导出 schema。

这确保“输出偶然相同但输入版本不同”时仍生成不同缓存键，避免回放记录歧义。

### 3.2 代次提交

`SnapshotStore.commit_generation` 接收本次全部节点产物：

1. 计算整代签名 `config + 所有节点 cache_key/content_hash/upstream_hash`。
2. 若与当前代次相同，直接复用当前版本。
3. 否则创建 staging 目录，写入所有条目和 manifest，原子重命名为新 `vNNNNNN`。
4. 原子更新 `CURRENT.json`。

旧代次不修改。显式请求不大于当前快照版本时抛 `E_SNAPSHOT_ROLLBACK`。配置版本低于当前快照配置版本时同样拒绝。

### 3.3 损坏检测

加载条目时依次校验：

1. 条目 `record_checksum`。
2. 由 `node/node_version/key_details` 重算的缓存键。
3. 由 payload 重算的内容哈希。
4. 上游哈希是否与键详情一致。
5. manifest 中的缓存键与内容哈希是否与条目一致。

manifest 和 `CURRENT.json` 同样包含 SHA-256 校验。任何不符返回 `E_CACHE_CORRUPT`，并带 `entry` 与 `version`。

## 4. 增量执行

`AnalysisSession._execute` 按拓扑顺序获取每个节点：

- 先由输入与上游哈希构造键，再查找当前或历史内容寻址条目。
- 命中则直接复用；未命中才执行节点计算。
- 标注修正导致得 `speaker` 键变化时，只重算与修正标注时间范围相交的说话人记录；其余记录按候选 ID 与样本边界从旧 speaker 快照复制。
- `export` 必须重新序列化完整 JSONL，但无需重算上游节点。

每次增量都会调用独立的 `_compute_fresh_export` 全量执行五节点，比较 JSONL 字符串和 SHA-256。若不同，返回 `E_INCONSISTENT` 并定位首个差异区间。

自检同时验证：

- 每帧首尾覆盖整个 clip，无样本空洞。
- 导出区间按时间升序且不重叠。
- 重算节点集合与复用节点集合无交集。
- 两集合并集等于全部五个节点。
- 受影响节点集合被两集合覆盖。

## 5. 回放

回放记录以导出缓存键寻址，包含：

- 导出键与导出 JSONL SHA-256。
- 五节点各自的精确快照代次、缓存键、内容哈希、节点版本、上游哈希。
- 配置版本、帧参数、门限版本、标注哈希/修订号、导出 schema。
- clip 时长。

回放步骤：

1. 校验回放记录自身 checksum。
2. 检查 lineage 是否完整，精确代次中的条目文件是否存在。
3. 加载并校验五个节点条目。
4. 校验 `envelope -> vad -> refine -> speaker -> export` 上游哈希链。
5. 从 speaker 快照重新执行导出。
6. 与记录 JSONL 逐字节比较，再逐项比较，最后返回可查询的区间列表。

缺文件返回 `E_LINEAGE_BROKEN`；文件存在但内容校验失败返回 `E_CACHE_CORRUPT`；重导出不符返回 `E_LINEAGE_BROKEN`。

## 6. 资源与校验

- `max_envelope_frames`：全部 clip 包络帧数总和上限。
- `max_snapshot_entries`：单代次条目数上限。
- 超限抛 `E_RESOURCE_LIMIT`。
- 启动时校验 16 kHz、帧参数、标签模板、资源上限、清单字段、clip/segment 唯一性及时间范围。
- 运行期配置写入 `runtime/config.current.json`，带配置身份 checksum；启动时优先恢复不低于静态配置版本的运行期配置。
