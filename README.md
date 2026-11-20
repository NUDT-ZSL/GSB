# 对话级语音情感分析 DAG：增量缓存与谱系重放

本项目从零实现一个可集成的本地对话级语音情感分析组件。它包含：

- 固定种子的多说话人 16kHz 单声道 PCM 对话合成器。
- 逐帧真值、对话清单、说话人注册清单生成器。
- 四节点有向无环图：帧特征、说话人嵌入与逐帧指派、段级情感回归、对话级聚合。
- 内容寻址缓存、键组成审计、版本化整体快照和同键并发写控制。
- 注册样本追加、真值修正、帧移调整、切段阈值调整后的依赖失效与后缀重算。
- 不可变分数谱系、只读追溯与缓存键重放。
- 配置/输入/资源/跨节点一致性自检及统一业务码。
- 完整 CLI、pytest 测试和可重复运行的验收 demo。

项目不依赖 Docker，不访问外部服务，只使用 Python 3.10+、numpy 和本地文件。

## 快速开始

环境已验证：

```text
Python 3.10.12
numpy 1.26.4
pytest 8.3.3
```

在项目根目录执行：

```bash
python3 -m dialog_emotion.cli generate --out data --force
python3 -m dialog_emotion.cli analyze \
  --dataset data \
  --cache artifacts/cache \
  --lineage artifacts/lineage \
  --out artifacts/report.json \
  --verify-full-recompute
```

报告包含逐帧特征摘要、说话人连续段、逐段 valence/arousal、置信区间、对话级加权分数、缓存键、输入版本、失效报告、自检结果和业务码。

一次性运行全部验收场景：

```bash
python3 -m dialog_emotion.cli demo --workspace .
```

验收报告写入 `artifacts/demo_report.json`，各场景完整报告写入 `artifacts/reports/`。

## 目录

```text
dialog_emotion/
  synth.py       确定性对话、真值、注册参考音频生成
  dataset.py     清单读取、输入校验、修正应用
  config.py      配置校验、配置快照版本化
  nodes.py       四个 DAG 节点及一致性校验
  cache.py       内容寻址缓存、快照、并发写锁
  pipeline.py    DAG 编排、增量失效、全量对照
  lineage.py     不可变分数记录、追溯、只读重放
  demo.py        完整验收与错误码矩阵
  cli.py         generate/analyze/query/append-registration/demo
tests/
  test_component.py
  test_cache.py
data/
  dialog.jsonl
  registry.jsonl
  corrections.jsonl
  audio/*.wav
  truth/*_hop20ms.jsonl
  truth/*_hop10ms.jsonl
  refs/*.wav
  timeline/*.jsonl
artifacts/
  demo_report.json
  reports/
  demo_cache/
  demo_lineage/
```

## 确定性样本

`generate` 生成 3 个多说话人对话，固定种子为 `20261121`、`20261122`、`20261123`。每个说话人有固定的基频、三个共振峰、能量增益、语速和相位种子；对话按时间轴将各轮语音叠加为 16kHz、16-bit、单声道 PCM WAV。

关键文件：

- `dialog.jsonl`：至少包含 `dialog_id`、`sample_rate`、`duration_ms`、`seed`。额外提供 `speaker_ids`、`audio_path`、`truth_path` 和 `truth_index`。
- `registry.jsonl`：包含 `speaker_id`、`ref_seed`、`ref_duration_ms`、`embedding_version`，并额外提供参考 WAV 路径和样本 ID。
- `truth/*_hop20ms.jsonl`：20ms 帧长、20ms 帧移逐帧真值。
- `truth/*_hop10ms.jsonl`：20ms 帧长、10ms 帧移逐帧真值，用于验证帧移调整。
- 逐帧真值字段：`frame_index`、`speaker_id`、`valence`、`arousal`、`is_speech`。
- `corrections.jsonl`：初始为空；修正行包含 `dialog_id`、`start_frame`、`end_frame`、`valence`、`arousal`。

生成同样输入时，WAV 字节、真值行和清单均由固定算法和固定种子决定。

## DAG 与计算口径

### 1. 帧特征节点 `frame_features`

- 帧长和帧移均先换算为整数样本数；不能精确落在整数样本索引的配置返回 `E_CONFIG_INVALID`。
- 帧数按 `floor((sample_count - frame_length) / frame_shift) + 1` 计算。
- 帧数与真值行数不一致返回 `E_FRAME_COUNT_MISMATCH`，明细包含 `dialog_id` 和 `expected_frame_count`。
- 每帧输出：短时能量、过零率、谱质心、85% 谱滚降。
- 输出矩阵值量化后写入规范化 JSON；同输入同配置逐字节一致。
- 驻留内存估算超过 `max_feature_memory_mb`：
  - `resource_policy=error`：返回 `E_RESOURCE_LIMIT`。
  - `resource_policy=degrade`：降为 `float32` 并记录 `feature_precision_float32`。

### 2. 说话人嵌入与逐帧指派节点 `speaker_embedding`

- 按 `speaker_id` 聚合一条或多条注册样本。
- 参考样本优先读取 `ref_path`；缺失时按 `ref_seed` 和 `ref_duration_ms` 确定性合成。
- 用对话帧特征的统计量归一化参考特征，得到每个说话人的嵌入均值和离散度。
- 逐帧计算加权平方距离，取最小距离说话人。
- 距离完全相同时按 `speaker_id` 升序打破平局。
- `segment_threshold_ms` 会平滑过短标签段。
- 输出逐帧标签、连续段 `[start_frame, end_frame)`、嵌入统计量和段边界。
- 标签不在对话声明集合中返回 `E_SPEAKER_SET_INVALID`，包含 `frame_position`。
- 段边界不连续或未覆盖全部帧返回 `E_SEGMENT_GAP`，包含 `frame_position`。

### 3. 段级情感回归节点 `segment_emotion`

- 按说话人连续段建立确定性岭回归，目标为该段逐帧修正真值。
- 特征设计包含截距、能量、过零率、谱质心和谱滚降的标准化值。
- 段分数取段内设计向量均值处的回归预测。
- 置信区间使用 95% 正态近似，显式记录 `ridge_ols_normal_ci`。
- 区间下界大于上界返回 `E_INTERVAL_INVALID`。
- 每个段附带 `source_digest`。局部变更后，只有输入片段摘要变化的段重新计算；未变化段直接从上一段级产物复用明细。

### 4. 对话级聚合节点 `dialog_aggregate`

- 权重口径显式记录为 `segment_duration_ms`。
- 段时长口径为 `frame_count * frame_shift_ms`。
- 权重量化后强制和为 1。
- valence、arousal 及区间上下界均按权重聚合。
- 聚合结果由段明细和已记录权重独立重算；不一致返回 `E_INCONSISTENT`，并标记 `aggregate_recomputation` 等类别。

## 缓存键与内容寻址

每个缓存键由规范化键组成明细计算 SHA-256：

```text
key = sha256(canonical_json({"cache_key_schema": "key-v1", "parts": key_parts}))
```

关键纳入项：

| 节点 | 键中的关键输入 |
| --- | --- |
| `frame_features` | 分块音频内容哈希、采样率、帧长、帧移、FFT、窗函数、特征精度、资源策略 |
| `speaker_embedding` | 上游帧特征产物哈希、注册清单版本、切段阈值、嵌入版本、对话声纹集合 |
| `segment_emotion` | 上游帧特征哈希、上游说话人哈希、真值文件哈希、真值修正版本、回归参数 |
| `dialog_aggregate` | 上游段级产物哈希、权重口径、时长口径 |

缓存条目结构：

```json
{
  "schema_version": "cache-entry-v1",
  "node": "segment_emotion",
  "key": "sha256:...",
  "key_parts": [{"name": "...", "value": "..."}],
  "payload": {"...": "..."},
  "artifact_hash": "sha256:...",
  "version": 14,
  "record_checksum": "sha256:...",
  "pair_checksum": "sha256:..."
}
```

- `artifact_hash` 是产物内容的规范化哈希。
- `record_checksum` 覆盖整个条目主体。
- `pair_checksum` 交叉绑定缓存键、产物哈希和条目版本。
- 产物内容损坏返回 `E_CACHE_CORRUPT`，包含条目和版本。
- 键无法由 `key_parts` 复现，或键和产物哈希无法交叉核对，返回 `E_CACHE_KEY_MISMATCH`。
- 每次提交后扫描全部条目并整体重写 `snapshot.json`，版本单调递增，校验和覆盖完整快照。
- 同一缓存键使用 `O_EXCL` 锁；并发后到者立即得到 `E_CACHE_BUSY`，不阻塞且不会覆盖已提交条目。

## 增量失效与后缀重算

`analyze` 会在缓存目录的 `state/` 下保存上一轮键和段摘要。局部更新时：

- 追加注册样本：帧特征复用；说话人节点及其下游重算。
- 修正单段真值：帧特征和说话人节点复用；段级节点重算，未受影响段明细复用；对话聚合重算。
- 调整帧移：新真值帧几何进入键，帧特征及其全部下游重算。
- 调整切段阈值：帧特征复用；说话人及下游重算。

失效报告同时输出：

- `recomputed_nodes` / `reused_nodes`：唯一的 `dialog_id:node` 事件集合，严格无交集。
- `recomputed_node_types` / `reused_node_types`：节点类型汇总。
- `affected_frames`：节点和对话维度的受影响帧区间。
- `affected_segments`：节点和对话维度的受影响段 ID。
- `segment_detail_reuse`：段级内部明细复用/重算数量。
- 自检：复用与重算无交集，并集覆盖本轮受评估集合。

增量运行可选 `--verify-full-recompute`。该选项完全绕过缓存重算一遍，并逐字节比较所有节点产物；不一致返回 `E_INCREMENTAL_DIVERGENCE`，包含 `dialog_id`、`node` 和 `first_divergent_segment`。

## 谱系追溯与重放

每次分析为每个段和每个对话分数写入不可变文件：

```text
artifacts/lineage/scr_<id>.json
```

记录包含：

- `score_id`
- `dialog_id`、`scope`、`segment_id`
- 当时的分数和置信区间
- 完整节点路径
- 每个节点的缓存键和产物哈希
- 音频、真值、修正、注册、配置等输入版本
- `run_id`

查询并重放：

```bash
python3 -m dialog_emotion.cli query \
  --lineage artifacts/demo_lineage \
  --cache artifacts/demo_cache \
  --score-id scr_5bf5a473ff0d47be73ec039f
```

实现使用只读缓存对象，不创建缓存目录、不提交条目、不重写分数记录。重放会逐节点核对键、产物哈希和缓存校验和；对话分数还会由缓存中的段明细和权重独立重算。断链返回 `E_LINEAGE_BROKEN`，并指明 `breakpoint_node` 和 `breakpoint_key`。

完整验收中的两个谱系 ID：

```text
段分数：scr_5bf5a473ff0d47be73ec039f
对话分数：scr_a15fb04bf9e60a05f5e9af67
```

## 业务码

| 业务码 | 触发条件 |
| --- | --- |
| `E_CONFIG_INVALID` | 采样率非 16000、帧参数非法、字段缺失或路径非法 |
| `E_DUP_DIALOG` | `dialog_id` 重复 |
| `E_REGISTRY_INVALID` | 注册清单缺 `speaker_id`、参考样本非法或对话说话人未注册 |
| `E_FRAME_COUNT_MISMATCH` | 特征帧数与真值帧数不一致 |
| `E_SPEAKER_SET_INVALID` | 指派标签不在对话声明集合 |
| `E_SEGMENT_GAP` | 段边界断裂、空段或未覆盖全部帧 |
| `E_INTERVAL_INVALID` | 置信区间上下界顺序非法 |
| `E_INCONSISTENT` | 聚合重算、跨节点口径或自检不一致 |
| `E_CACHE_CORRUPT` | 条目/快照/配置校验和不符或内容损坏 |
| `E_CACHE_KEY_MISMATCH` | 缓存键与键组成或产物哈希无法互证 |
| `E_CACHE_BUSY` | 同一缓存键已有并发写者 |
| `E_RESOURCE_LIMIT` | 特征内存或缓存条目超限 |
| `E_INCREMENTAL_DIVERGENCE` | 增量结果与无缓存全量结果不一致 |
| `E_LINEAGE_BROKEN` | 谱系记录缺失、链断、产物缺失或分数无法复现 |

成功时业务码为 `OK`。

## 集成 API

```python
from dialog_emotion.cache import CacheStore
from dialog_emotion.config import PipelineConfig
from dialog_emotion.lineage import LineageStore
from dialog_emotion.pipeline import DialogueEmotionPipeline

config = PipelineConfig(
    frame_ms=20.0,
    frame_shift_ms=20.0,
    segment_threshold_ms=100.0,
    max_feature_memory_mb=64.0,
    max_cache_entries=10000,
    max_parallel_nodes=2,
    resource_policy="error",
)

pipeline = DialogueEmotionPipeline(
    cache_root="artifacts/cache",
    lineage_root="artifacts/lineage",
)
report = pipeline.analyze(
    dataset_root="data",
    config=config,
    corrections_path="data/corrections.jsonl",
    use_cache=True,
    verify_full_recompute=True,
)

score_id = report["dialogs"][0]["lineage"]["segment_score_ids"][0]
replay = LineageStore("artifacts/lineage").replay(
    score_id,
    CacheStore("artifacts/cache", read_only=True),
)
```

`analyze` 返回普通字典，可直接序列化；也可通过 CLI 写入 JSON 报告。

## 测试与验收

运行测试：

```bash
python3 -m pytest -q
```

实际结果：

```text
13 passed in 3.36s
```

完整验收：

```bash
python3 -m dialog_emotion.cli demo --workspace .
```

实际结果：

```text
business_code: OK
all_runs_ok: true
all_incremental_equivalence_checks_ok: true
lineage_replay_read_only: true
error_code_matrix_passed: true
错误/降级用例: 17/17
```

各场景摘要：

| 场景 | 变更类型 | 重算节点类型 | 复用节点类型 | 段明细复用/重算 | 全量对照 |
| --- | --- | --- | --- | --- | --- |
| baseline | initial | 四节点 | 无 | 0 / 12 | OK |
| truth_correction | truth_correction | segment_emotion, dialog_aggregate | frame_features, speaker_embedding | 4 / 1 | OK |
| registration_append | registration_append | speaker_embedding, segment_emotion, dialog_aggregate | frame_features | 9 / 1 | OK |
| frame_shift | frame_shift | 四节点 | 无 | 0 / 10 | OK |
| segment_threshold | segment_threshold | speaker_embedding, segment_emotion, dialog_aggregate | frame_features | 7 / 2 | OK |

对话级基线分数：

```text
dlg_001  valence=0.06724322   arousal=0.31087557  segments=5
dlg_002  valence=-0.03526292  arousal=0.14170014  segments=3
dlg_003  valence=0.06520707   arousal=0.13188030  segments=4
```

缓存快照从基线版本 12 单调递增到最终版本 44；每轮均整体重写完整快照。

详细机器可读结果见：

- `artifacts/demo_report.json`
- `artifacts/reports/baseline.json`
- `artifacts/reports/truth_correction.json`
- `artifacts/reports/registration_append.json`
- `artifacts/reports/frame_shift.json`
- `artifacts/reports/segment_threshold.json`
