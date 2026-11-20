# 对话语音情感增量分析组件

这是一个纯本地、确定性的对话语音情感分析领域组件。它从内容寻址缓存与依赖失效追溯出发，把一次分析建模为：

1. 分帧声学特征提取
2. 说话人嵌入与逐帧指派
3. 连续段情感回归
4. 对话级时长加权聚合

组件不依赖外部服务、容器、账号或网络。样本 PCM、逐帧真值、对话清单和注册样本清单都由固定种子在本地生成。

## 快速开始

环境要求：Python 3.10+、NumPy 1.24+、pytest 8+。

Windows 命令行：

```bat
py -3.10 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python scripts\generate_samples.py --out samples
python examples\run_demo.py
python -m pytest -q
```

Linux/macOS：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/generate_samples.py --out samples
python examples/run_demo.py
python -m pytest -q
```

示例会把临时样本、缓存、配置快照和追溯记录放在临时目录，完成后自动删除；使用 `python examples/run_demo.py --workdir .voice-demo` 可保留全部产物。

## 核心 API

```python
from voice_emotion import (
    PipelineConfig,
    VoiceEmotionPipeline,
    load_dataset,
)

dataset = load_dataset("samples/dialogs.jsonl", "samples/enrollments.jsonl")
pipeline = VoiceEmotionPipeline(".voice-cache", PipelineConfig())

first = pipeline.analyze_dataset(dataset)
updated = pipeline.analyze_dataset(dataset_after_change, previous_run=first)

print(updated.data["report"]["reused_nodes"])
print(updated.data["report"]["recomputed_nodes"])
print(updated.data["report"]["affected_frames"])
print(updated.data["report"]["affected_segments"])

record_id = first.data["dialogs"][0]["lineage"]["dialog_record_id"]
historical = pipeline.trace_score(record_id)
print(historical["replayed"])
print(historical["node_path"])
```

`analyze_dataset` 返回 `RunResult`，其中：

- `data["dialogs"]`：特征摘要、逐帧标签、连续段、段级分数、对话分数、节点键和追溯记录 ID。
- `data["ranking"]`：按对话 valence 降序、`dialog_id` 升序打破平局的稳定排序。
- `data["report"]`：重算/复用节点清单、受影响帧与段、降级类别。
- `result_fingerprint`：只覆盖分析结果，可用于比较增量结果与全量重算结果。

常用细粒度接口：

- `extract_features`：确定性的四维逐帧特征。
- `build_speaker_registry` / `assign_speakers`：注册嵌入与逐帧指派。
- `regress_segment`：确定性岭回归及置信区间。
- `aggregate_segments`：从逐段明细和显式权重重算对话分数。
- `validate_run_consistency`：跨节点一致性自检。
- `CacheStore` / `ConfigStore`：内容寻址缓存与整体配置快照。

## 样本与阶段约定

样本生成脚本写出：

- 16 kHz、单声道、16-bit PCM WAV。
- 逐帧真值 JSONL：`frame_index`、`speaker_id`、`valence`、`arousal`、`is_speech`。
- 对话清单 JSONL：`dialog_id`、`sample_rate`、`duration_ms`、`speaker_ids`、`seed`、`audio_path`、`truth_path`。
- 注册样本清单 JSONL：`speaker_id`、`seed`、`duration_ms`、`sample_index`。

帧边界按样本索引精确计算：

```text
frame_samples = round(frame_ms * 16000 / 1000)
hop_samples   = round(frame_shift_ms * 16000 / 1000)
starts        = 0, hop_samples, 2 * hop_samples, ...
```

只保留完整窗口。特征列为短时能量、过零率、谱质心、谱滚降。特征帧数必须与真值帧数完全一致，否则返回 `E_FRAME_COUNT`。

如果刻意修改 `frame_shift_ms`，必须提供与新分帧计划帧数一致的真值文件；组件不会用当前真值静默截断或补齐历史数据。`voice_emotion.dataset.regenerate_truth` 可用于本地测试夹具重建真值。

说话人阶段由注册清单中的 seed/时长/样本序号重新合成注册片段，计算四维嵌入向量的均值与标准差。每帧使用对角马氏距离打分，分数越高越相似；同分按 `speaker_id` 升序。`segment_threshold` 表示最短连续段帧数，短段按确定性规则合并到较长邻居，平局合并到左侧。

段级回归使用固定特征变换和岭回归，默认采用确定性留一交叉预测，避免分数退化为段内真值均值；置信区间由留一预测分数的标准误与配置的 `confidence_z` 得到。对话级权重默认是 `segment_duration_ms`，即 `frame_count * frame_shift_ms`；每一段的权重、总权重和公式均写入结果。

## 缓存键与失效路径

每个节点产物都有内容哈希，缓存条目记录 `cache_key`、`key_components`、`context_versions`、全局单调递增 `version` 和内容 `checksum`。缓存路径为：

```text
<cache>/objects/<key前两位>/<key>.json
```

依赖感知的键组成：

| 节点 | 参与节点身份哈希的依赖 |
| --- | --- |
| `features` | 音频分块哈希、PCM 哈希、特征参数版本与参数哈希、帧长、帧移、FFT、窗函数、特征 dtype |
| `assignment` | 上游特征键与输出哈希、对话 speaker_ids、这些说话人的注册样本版本、帧移、切段阈值、说话人配置 |
| `segment` | 上游指派键与输出哈希、段边界/说话人、该段真值切片哈希、对话相关注册版本、特征参数、帧移、切段阈值、回归配置 |
| `dialog` | 全部段键与段哈希、对话真值版本、对话相关注册版本、特征参数、帧移、切段阈值、回归配置、聚合配置、上游段集合哈希 |

这里采用“相关依赖进入当前键、完整上下文写入条目”的规则：注册版本按对话声明的说话人取子集，追加某人的样本不会使不包含该人的对话失效；追加注册样本不会改变特征键，因此特征缓存可复用；单段真值修正只改变该段的真值切片键，因此其他段仍可复用。最终对话键通过上游键链同时覆盖特征、注册、真值、帧移、切段阈值和上游哈希，且每个缓存条目都记录完整的 `context_versions` 供审计。

实际失效路径：

| 变更 | 复用 | 重算 |
| --- | --- | --- |
| 追加注册样本 | 特征；不含该说话人的对话全部节点 | 使用该说话人的对话：指派、全部段、对话 |
| 修正单个预测段内真值 | 特征、指派、其他段 | 受影响段、对话 |
| 修改声学特征参数或帧移 | 无可复用前缀 | 特征、指派、全部段、对话 |
| 修改切段阈值 | 特征 | 指派、全部段、对话 |
| 只修改聚合权重口径 | 特征、指派、全部段 | 对话 |

增量运行后的分析结果与全新缓存全量重算结果通过 `result_fingerprint` 比较；测试会验证两者逐字节一致。

## 历史分数追溯

每次段级和对话级分数都会写入不可变追溯记录，包括：

- `record_id`
- 输入版本（特征参数、注册样本、真值修正、帧移、切段阈值、回归/聚合配置）
- 配置快照版本与校验和
- 从特征到最终节点的完整节点路径、每个节点的历史缓存键、条目版本、输出哈希和父节点
- 当时的段级或对话级分数

`trace_score(record_id)` 只使用记录中的历史配置快照和缓存键：

- 对话级分数会从历史段级明细和当时权重公式独立重算并核对。
- 不读取当前数据集、当前真值或当前版本配置来替代历史版本。
- 缺键、校验和不符、版本不符或重算结果不一致时返回 `E_LINEAGE_BROKEN`，并在 `details` 中给出 `broken_node`、`version`、`cache_key` 和原因。

`list_scores(dialog_id)` 可列出某对话的全部历史段级/对话级记录摘要。

## 版本化缓存与并发写

- 缓存条目、配置快照共用单调递增版本时钟。
- 配置每次变化写入完整的新快照并原子替换 `configs/current.json`，不增量修补；历史 `config-vXXXXXXXX.json` 保留用于追溯。
- 条目和配置都带规范化 JSON 内容校验和；损坏、校验和不符、当前配置与历史不一致时返回 `E_CACHE_CORRUPT`。
- 同一缓存键使用 `O_CREAT|O_EXCL` 写锁且不等待；并发冲突方立即得到 `E_CACHE_BUSY`。
- 键相同且载荷相同时直接复用原条目，不产生新版本。

## 资源上限与自检

`ResourceLimits` 支持：

- `max_frames_per_analysis`
- `max_cache_entries`
- `max_feature_bytes`
- `on_limit="error" | "degrade"`

默认超限返回 `E_RESOURCE_LIMIT`。降级模式会为以下类别打标：

- `frame_budget_soft_limit`
- `feature_precision_float32`
- `cache_retention_limited`

`validate_run_consistency` 与 `pipeline.self_check` 会检查：

- 特征、标签、真值和段范围覆盖同一 `frame_index` 集合。
- 段边界无重叠、无空洞且覆盖全部帧。
- 指派段与回归段一一对应。
- 对话聚合能由段明细和权重独立重算。
- 复用集合与重算集合无交集，并集覆盖当前全部节点。
- 受影响帧/段集合合法。

不一致返回 `E_INCONSISTENT`。

## 业务错误码

| 错误码 | 场景 |
| --- | --- |
| `E_SAMPLE_RATE` | 采样率不是 16000 |
| `E_FRAME_CONFIG` | `frame_ms` 非法、帧移超过帧长、FFT/窗函数/切段阈值非法 |
| `E_FRAME_COUNT` | 特征帧数与真值帧数不一致 |
| `E_DUPLICATE_DIALOG` | 对话清单重复 `dialog_id` |
| `E_ENROLLMENT_MISSING` | 注册记录缺少 `speaker_id`、注册剪辑不足一帧或对话说话人未注册 |
| `E_SPEAKER_UNKNOWN` | 指派或真值标签越出对话声明的 `speaker_ids` |
| `E_INTERVAL_INVALID` | 置信区间上下界顺序非法或区间字段不完整 |
| `E_RESOURCE_LIMIT` | 帧数、缓存条目或驻留特征内存超限 |
| `E_CACHE_CORRUPT` | 缓存/配置/存储清单损坏或校验和不符 |
| `E_CACHE_BUSY` | 同一缓存键或配置指针正被其他写者占用 |
| `E_LINEAGE_BROKEN` | 历史追溯链断裂或历史键无法重放相同分数 |
| `E_INCONSISTENT` | 跨节点、缓存载荷或自检不一致 |
| `E_INPUT_INVALID` | JSONL 字段、数值范围或未知容器格式非法 |

所有异常都是 `BusinessError`，可通过 `exc.code`、`exc.details` 或 `exc.to_dict()` 读取结构化信息。

## 项目结构

```text
voice_emotion/
  audio.py         PCM 读取/写入、分块内容哈希
  synthesis.py     固定种子多说话人语音与逐帧真值合成
  dataset.py       清单加载、样本生成、真值版本
  features.py      阶段 1
  speaker.py       阶段 2
  regression.py    阶段 3/4
  keys.py          依赖感知缓存键
  cache.py         内容缓存、全局版本、配置快照、写锁
  lineage.py       不可变追溯记录与历史键重放
  pipeline.py      增量编排、资源限制、自检
scripts/
  generate_samples.py
  self_check.py
examples/
  run_demo.py
tests/
docs/
  design.md
```

更详细的数据契约与状态机见 [docs/design.md](docs/design.md)。

## 实际验证记录

验证日期：2026-11-21，环境：Python 3.10.12、NumPy 1.26.4、pytest 8.3.3。

- `python -m pytest -q`：`28 passed`。
- 样本生成：3 条 2.4 秒对话、3 个说话人、16 kHz 单声道 PCM；重复生成文件逐字节一致。
- 示例 `python examples/run_demo.py --dialogs 2 --duration-ms 2400`：
  - 33 个节点复用，2 个节点重算（恰好是受影响段和对话聚合）。
  - 增量结果与全新缓存全量重算的 `result_fingerprint` 一致。
  - 修正前后两个历史对话分数均可按记录键重放成功。
  - 非法帧移返回 `E_FRAME_CONFIG`。
  - 自检结果为 `{"status": "ok", ...}`。
- 已覆盖测试：样本确定性、特征逐字节复现、帧数错误、说话人越界、区间非法、缓存复用/损坏、配置整体替换、并发键写冲突、注册追加、单段真值修正、帧移/切段阈值/声学参数/聚合配置变更、资源上限与降级、历史重放与断链、自检失败。
