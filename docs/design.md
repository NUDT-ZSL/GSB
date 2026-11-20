# 设计说明

## 1. 目标与边界

组件面向“已有分帧特征产物之上”的对话语音情感分析。为了可独立验证，项目附带确定性合成器，但分析流水不依赖外部模型或网络。其关键性质是：

- 同一 PCM、配置和数据版本产生同一特征、标签、段边界、分数和排序。
- 缓存身份按节点依赖构造，而不是为整个任务只使用一个全局键。
- 上游复用与下游失效可以同时发生。
- 历史分数保留当时的配置快照、节点键和版本，不随当前数据变化。

## 2. 数据契约

### 对话清单

每行一个 JSON 对象，必需字段：

```json
{
  "dialog_id": "dlg_0001",
  "sample_rate": 16000,
  "duration_ms": 2400,
  "speaker_ids": [1, 2, 3],
  "seed": 20261121,
  "audio_path": "audio/dlg_0001.wav",
  "truth_path": "truth/dlg_0001.truth.jsonl"
}
```

`audio_path` 与 `truth_path` 相对清单文件解析。重复 `dialog_id` 返回 `E_DUPLICATE_DIALOG`。

### 逐帧真值

每行：

```json
{"frame_index":0,"speaker_id":2,"valence":0.12,"arousal":0.33,"is_speech":true}
```

`frame_index` 必须从 0 连续递增。`valence` 与 `arousal` 范围是 `[-1, 1]`。`speaker_id` 必须属于对话声明集合，否则返回 `E_SPEAKER_UNKNOWN`。

### 注册清单

每行：

```json
{"speaker_id":1,"seed":20261121,"duration_ms":900,"sample_index":0}
```

同一 `speaker_id` 可有多条样本；它们用 `sample_index` 等字段排序后合并嵌入统计量。追加一条注册样本会改变注册样本版本。

## 3. 确定性约束

### 样本合成

- 所有随机数由 `SHA-256(canonical_json(parts))` 派生的种子生成，不使用进程级随机状态。
- 说话人基频、三个共振峰及增益、语速、亮度、能量由 `speaker_id + seed` 稳定决定。
- 对话时间轴由 `dialog_id + seed + duration + speakers` 稳定决定。
- PCM 先以 float64 合成，再统一量化为小端 16-bit PCM。
- 真值在量化后的时间轴上按样本索引中心取样，输出六位小数。
- JSON/JSONL 使用排序键、紧凑分隔符和 LF 换行。

### 分帧特征

给定采样率 `sr=16000`：

```text
N = round(frame_ms * sr / 1000)
H = round(frame_shift_ms * sr / 1000)
starts[i] = i * H,  i = 0..floor((sample_count - N) / H)
```

窗口为显式周期 Hann：

```text
w[n] = 0.5 - 0.5 * cos(2*pi*n/N), n = 0..N-1
```

每帧计算：

- `rms_energy = dot(x, x) / N`
- `zero_crossing_rate = count(signbit(x[n]) != signbit(x[n-1])) / (N-1)`
- `spectral_centroid = sum(f * |X(f)|) / sum(|X(f)|)`
- `spectral_rolloff`：累计幅度首次达到 `rolloff_percent * total` 的频率

特征载荷包含 dtype、形状、按 C 顺序排列的小端数组字节和输出哈希。`frame_count` 必须在特征矩阵、标签、真值文件和段范围中一致。

## 4. 说话人嵌入与切段

注册片段由每条注册记录重新合成，再走相同的特征提取。每帧嵌入变换：

```text
v = [0.35*log1p(energy), 4*zcr, centroid/nyquist, rolloff/nyquist]
```

每个 `speaker_id` 保存所有注册片段帧的均值和标准差。对帧 `v` 与说话人的对角马氏距离：

```text
d = sum(((v - mean) / max(std, 1e-9))^2)
score = -d
```

选择最高分；完全同分按 `speaker_id` 升序。`segment_threshold > 1` 时，小于阈值的连续 run 按以下规则合并：

1. 选择当前最短 run，长度相同选择最早 run。
2. 首段并入后一段，末段并入前一段。
3. 中间段并入较长的相邻 run，左右等长时并入左侧。

重复直到所有 run 达到阈值或只剩一个 run。

## 5. 回归与聚合

段级对每个目标分别做岭回归。设计矩阵为上面的四维嵌入向量，回归前按段中心化：

```text
beta = (Xc^T Xc + lambda*I)^-1 Xc^T yc
pred = Xc beta + mean(y)
```

默认使用解析留一交叉预测。若全拟合的帽子矩阵对角为 `h_i`，则：

```text
loo_i = y_i - (y_i - pred_i) / (1 - h_i)
score = mean(clip(loo_i, -1, 1))
se = sqrt(sum((clip(loo_i) - score)^2) / max(1, n - 1) / n)
ci = clip(score +/- z * se, -1, 1)
```

这样声学特征通过段内回归关系实际参与段分数，而不是仅输出真值均值。配置也允许显式的 `in_sample` 方案用于对照实验。

聚合权重：

- `segment_duration_ms`：`weight_i = frame_count_i * frame_shift_ms`
- `segment_frame_count`：`weight_i = frame_count_i`

对话分数：

```text
score = sum(score_i * weight_i) / sum(weight_i)
se_agg = sqrt(sum((weight_i / sum(weight))^2 * se_i^2))
```

每段权重、总权重、公式、区间方法和配置哈希都写入聚合详情。自检和追溯会从段明细独立重算，而不是信任对话缓存中的分数。

## 6. 缓存键状态机

### 节点身份

缓存键是 JSON 字典的 SHA-256，字典含 `schema`、`node` 和依赖分量。`upstream_hash` 是每个非根节点必有的父产物内容哈希。

```text
features   <- audio chunk root + feature config
assignment <- features(key, hash) + dialog-scoped enrollment + speaker/segment config
segment    <- assignment(key, hash) + segment identity + truth slice + regression config
dialog     <- segment keys/hashes + truth/enrollment/feature + aggregation config
```

### 为什么不在特征键里放注册版本

若所有节点键都包含所有版本，追加注册样本会错误地使特征失效。组件的规则是：

- 影响当前节点输出的版本进入当前节点键。
- 不影响当前节点、但属于同一次分析的上下文写入 `context_versions`，用于审计。
- 下游键通过上游键和输出哈希形成传递依赖。
- 最终对话键因此同时覆盖全部要求的版本与上游哈希，同时保留按路径失效能力。

### 缓存条目

条目是不变的规范化 JSON：

```json
{
  "schema": "vem.cache-entry.v1",
  "cache_key": "...",
  "node": "segment",
  "version": 17,
  "key_components": {"...": "..."},
  "context_versions": {"...": "..."},
  "payload": {"...": "..."},
  "checksum": "..."
}
```

`version` 来自共享单调时钟。`checksum` 覆盖除自身外的完整条目。读取时同时核对路径 key、schema、checksum 和载荷内部输出哈希。

## 7. 增量算法

对每个对话：

1. 读取当前 PCM、真值和配置，计算当前版本与帧计划。
2. 先查特征键；命中则复用，否则只计算特征。
3. 查指派键；对注册版本、切段阈值或特征上游变化敏感。
4. 对每个段查独立段键；真值切片未变且回归配置未变的段直接复用。
5. 查对话键；仅聚合配置变化时只有对话节点重算。
6. 与前一运行比较节点键和真值帧哈希，生成受影响帧/段集合。
7. 自检复用/重算集合和跨阶段帧集合。

“仅重算受影响后缀”在实现上由逐阶段缓存查找保证，不通过先全量计算再丢弃结果实现。段级键拆分为每段一条，因此单段修正不会重算整对话的所有段。

## 8. 追溯记录与重放

追溯记录含 `node_path`，每个路径项：

```json
{
  "node": "assignment",
  "cache_key": "...",
  "version": 7,
  "output_hash": "...",
  "parents": ["feature-cache-key"]
}
```

重放步骤：

1. 核对记录自身的校验和。
2. 按 `config_version` 读取历史配置快照并核对校验和。
3. 按记录键逐项读取历史缓存条目。
4. 核对条目版本、节点类型、输出哈希和父节点哈希。
5. 段记录比较缓存段分数与记录分数；对话记录从段缓存重算聚合并比较输出哈希与分数。
6. 任一失败返回 `E_LINEAGE_BROKEN`，定位到节点和版本。

重放函数不接收 `Dataset`，从 API 层面避免使用当前数据替代历史数据。

## 9. 并发与版本替换

### 写同一缓存键

写入先检查条目是否存在；不存在时用独占创建锁文件：

```text
objects/<prefix>/<key>.lock
```

创建失败立即返回 `E_CACHE_BUSY`，不重试、不阻塞。写入使用同目录临时文件加原子替换。

### 配置整体替换

配置变化时：

1. 获取当前配置指针锁。
2. 从共享单调时钟取新版本。
3. 写完整历史快照 `config-vXXXXXXXX.json`。
4. 原子写完整 `current.json`。

读取当前配置会核对：

- 当前文件校验和。
- 对应历史文件是否存在且完全相同。
- 历史最大版本是否高于当前版本。

任何不一致返回 `E_CACHE_CORRUPT`，不使用增量修补。

## 10. 自检不变量

`validate_run_consistency` 的核心不变量：

```text
feature.frame_indices == assignment.label_indices == truth.frame_indices
segments.partition(all_frames)
assignment.segments == regression.segments
aggregate(segment_details, weights) == dialog.scores
reused_node_ids intersect recomputed_node_ids == empty
reused_node_ids union recomputed_node_ids == affected_node_ids == all current nodes
affected_frames subset of all frames
affected_segments subset of current segments
```

违反任一不变量返回 `E_INCONSISTENT`，并携带缺失、额外、重叠或越界集合。
