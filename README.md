# Transcript Governance Diagnostics

面向 Linux/WSL Ubuntu 的确定性转写治理组件。样本 PCM、v1 扁平行式转写和 v2「分片 JSON + SQLite 元数据库」均由固定种子生成；组件提供格式解析、双向迁移、跨版本裁决、刷新传播、一致性查询、内容寻址缓存、依赖重放和乐观快照提交。

## 快速开始

```bash
python3 --version          # 需要 3.10+
python3 -m pip install -r requirements.txt
python3 -m pip install -e .

python3 scripts/generate_samples.py samples
transcript-governance diagnose --samples samples --reports reports

# 或直接运行完整示例
python3 examples/run_diagnostic.py

# 自动化测试
python3 -m pytest -q
```

诊断成功后生成：

- `reports/diagnostic-report.md`：人工可读报告。
- `reports/diagnostic-report.json`：结构化检查结果、错误码、刷新计划和缓存重放链。
- `samples/sample-16khz-mono.pcm`：16 kHz、单声道、signed-int16 little-endian PCM。
- `samples/sample-16khz-mono.wav`：同一 PCM 的 WAV 封装，便于试听。
- `samples/transcripts/sample.v1.txt`：v1 管线格式。
- `samples/transcripts/v2/manifest.json` 与 `shard-*.json`：v2 分片。
- `samples/transcripts/v2/metadata.sqlite3`：绝对毫秒、说话人、词级时序和修订号。

当前固定种子为 `421`，增益为 `0.72`；PCM 内容哈希写入 `samples/sample-manifest.json` 和诊断报告。

## 库用法

```python
from transcript_governance.samples import generate_sample_tree
from transcript_governance.formats_v1 import read_v1
from transcript_governance.formats_v2 import read_v2_dataset
from transcript_governance.audio import FrameConfig, extract_features
from transcript_governance.migrate import migrate_v1_to_v2, migrate_v2_to_v1
from transcript_governance.adjudicate import adjudicate
from transcript_governance.consistency import check_consistency, TimelineIndex

paths = generate_sample_tree("samples")
v1 = read_v1(paths.v1)
v2 = read_v2_dataset(paths.v2_dir)

features = extract_features(paths.pcm.read_bytes(), FrameConfig(frame_ms=25, hop_ms=10))
print(features.energy_quantiles, features.speech_segments_ms)

as_v2 = migrate_v1_to_v2(v1)
as_v1 = migrate_v2_to_v1(v2)
assert as_v2.boundaries_preserved and as_v1.boundaries_preserved

verdict = adjudicate(v1, v2)
print(verdict.conclusions)

regions = [(u.start_ms, u.end_ms) for u in v2.utterances]
report = check_consistency(v2.utterances, declared_regions=regions)
timeline = TimelineIndex(v2.utterances, declared_regions=regions)
print(timeline.at(500))
```

## 需求与实现映射

| 需求 | 主要模块 | 验证入口 |
| --- | --- | --- |
| 固定种子 PCM、分帧能量/ZCR/分位数、VAD 边界 | `src/transcript_governance/audio.py` | `tests/test_audio.py` |
| 同一帧网格对齐和差异归因 | `src/transcript_governance/audio.py`、`attribution.py` | `test_alignment_and_attribution_reuse_frame_grid` |
| v1/v2 版本头/字段集解析与业务码 | `formats_v1.py`、`formats_v2.py`、`validation.py` | `test_formats_migration.py` |
| v1 ↔ v2 默认值、来源、丢弃/展平、边界不变 | `migrate.py` | 迁移与展平测试 |
| confidence → revision → ID 字典序裁决 | `adjudicate.py` | `test_adjudication_outputs_three_overlap_conclusions` |
| 重叠/词级触发/依赖链失效与逐毫秒等价 | `refresh.py` | `test_incremental_adjudication_matches_full_recompute_ms_by_ms` |
| `E_OVERLAP`/`E_GAP`、毫秒点查询、缓存链重放 | `consistency.py`、`cache.py` | 一致性和缓存测试 |
| 内容寻址缓存与区间定向失效 | `cache.py` | `test_interval_targeted_invalidation_keeps_unrelated_objects` |
| 版本/校验和快照、`E_STAGE_BUSY` | `snapshot.py` | `test_snapshot_*` |
| 端到端验收报告 | `diagnose.py`、`cli.py` | `reports/diagnostic-report.*` |

## 关键语义

- 毫秒区间统一按半开区间 `[start_ms, end_ms)` 处理；前一条 `end_ms ==` 后一条 `start_ms` 不算重叠或缺口。
- 帧网格从 0 开始，帧长和帧移可配置；最后一帧用零填充以保持帧长固定，同时记录真实采样结束位置。
- 读取器返回 `LoadResult`，其中 `diagnostics` 是结构化 `Diagnostic(code,message,source,line,utterance_id,details)`，不会只用异常文本表达业务错误。
- 缓存键由 `CacheKey.build(...)` 规范化 JSON 后计算 SHA-256，纳入 PCM 哈希、转写摘要、格式版本、配置和上游产物哈希。
- 快照提交采用文件锁 `fcntl.LOCK_EX | fcntl.LOCK_NB`；锁竞争立即返回 `E_STAGE_BUSY`，不等待。

## 业务码

| 业务码 | 触发条件 |
| --- | --- |
| `E_FIELD_MISSING` | 必填列/JSON 字段缺失或为空 |
| `E_FIELD_TYPE` | 毫秒、置信度等字段类型或列数错误 |
| `E_FORMAT_HEADER` | 缺少可识别版本头且字段集不合法 |
| `E_TIME_REGRESSION` | 源顺序绝对毫秒或词级时间倒退 |
| `E_DUPLICATE_UTTERANCE_ID` | 同一转写集合内 ID 重复 |
| `E_INVALID_INTERVAL` | `start_ms > end_ms` 或词区间非法 |
| `E_METADATA_MISMATCH` | v2 JSON 分片与 SQLite 元数据不一致 |
| `E_OVERLAP` / `E_GAP` | 时间轴重叠或声明区域未覆盖 |
| `E_CACHE_MISS` / `E_CACHE_CORRUPT` | 缓存键不存在或摘要校验失败 |
| `E_DEPENDENCY_CYCLE` | 重放依赖链存在环 |
| `E_SNAPSHOT_VERSION_CONFLICT` | 期望基础版本与当前单调版本不一致 |
| `E_SNAPSHOT_CHECKSUM_MISMATCH` | 基础或候选快照校验和不一致 |
| `E_SNAPSHOT_STALE` | 快照基线过旧，需要重新加载并整体替换 |
| `E_STAGE_BUSY` | 已有非阻塞阶段提交锁 |

## 迁移策略

v1 → v2 默认补全：

- `speaker = "S0"`，来源 `default:V1ToV2Policy.default_speaker`。
- `revision = 1`，来源 `default:V1ToV2Policy.default_revision`。
- `words = []`，来源 `default:empty-word-timing-list`。
- 也可选择 `V1ToV2Policy(word_timing="even_allocate")` 按文本权重推断词区间，并记录推断来源。

v2 → v1 对 v1 无法表达的字段提供显式策略：

- 说话人：`discard`（默认）或 `flatten_prefix` 为 `[S1] 文本`。
- 词级时序：`flatten_text`（默认，仅保留词形）、`discard` 或 `flatten_timing_tokens`（编码为 `word[start-end]`）。
- 修订号：`discard`（默认）或 `flatten_id_suffix` 为 `UTT-1#r3`。

所有策略都逐字保留 `start_ms` 和 `end_ms`，`MigrationResult.boundaries_preserved` 会给出逐项校验。

## 刷新与重放

`plan_v2_revision(...)` 计算：

1. 修订条目的旧/新区间并集。
2. 变化词的旧/新词时间并集，形成 `word_time` 触发器。
3. 与上述区间相交的其他 utterance。
4. 显式依赖图的传递下游失效。

`AdjudicationRefreshService` 只重算受影响的重叠对，复用其余已提交裁决；随后比较增量结果和整体重新裁决在区域内每一个毫秒点的规范化状态哈希。诊断样本中修订 `UTT-001` 时仅失效 `v1:UTT-001`、`v2:UTT-001`，只重算 1 个裁决对并复用 4 个裁决对；自动化测试还覆盖了 300 个毫秒点的增量/全量状态完全相等。

## 目录结构

```text
src/transcript_governance/   # 可集成库
scripts/generate_samples.py  # 固定种子生成脚本
examples/run_diagnostic.py   # 完整示例
tests/                       # 27 个 pytest 回归测试
samples/                     # 已生成的确定性样本
reports/                     # 最新诊断报告和运行时存储
docs/verification.md         # 实际验证命令与结果
```
