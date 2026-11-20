# 对话语音情感增量打分组件

这是一个从零实现、可嵌入本地进程的对话语音情感 DAG 示例。它使用固定种子离线合成
16 kHz 单声道 PCM、逐帧真值和两份清单，完整演示：

- 帧级特征、说话人注册嵌入、逐帧指派、连续段切分；
- 段级情感回归、置信区间和对话级时长加权聚合；
- 内容寻址缓存、完整缓存键、版本快照和并发提交控制；
- 追加注册样本、单段真值修正、帧移/切段阈值变化时的依赖失效；
- 增量结果与从零全量运行的逐字节一致性校验；
- 旧段级/对话级分数的缓存键、输入版本和节点路径追溯与重放。

不依赖容器、数据库、线上服务或真实音频附件。所有样本和结果均可在本地重放。

## 快速开始

目标环境为 Windows + Python 3.11+，或在同类 Linux 环境运行。安装依赖：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install numpy pytest
```

生成样本并验证：

```powershell
python scripts/generate_samples.py --out data/samples
pytest -q
python scripts/demo.py --samples data/samples `
  --work artifacts/demo --json artifacts/demo-report.json
```

Linux/macOS 等价命令：

```bash
python scripts/generate_samples.py --out data/samples
pytest -q
python scripts/demo.py --samples data/samples \
  --work artifacts/demo --json artifacts/demo-report.json
```

本次实际执行环境是 Linux、Python 3.10.12、NumPy 1.26.4、pytest 8.3.3。
实现只使用 Python 3.10+ 语法，可在 Python 3.11 下运行；详见
`VERIFICATION.md`。

最小集成方式：

```python
from dialog_emotion import (
    AppendRegistration,
    CorrectTruth,
    IncrementalPipeline,
    load_bundle,
)

bundle = load_bundle("data/samples")
engine = IncrementalPipeline(
    bundle,
    cache_dir="artifacts/cache",
    config_snapshot_dir="artifacts/config",
    lineage_dir="artifacts/lineage",
)
baseline = engine.run()
updated = engine.run([AppendRegistration("spk_a", 991, 850)])
updated = engine.run(
    [CorrectTruth("dlg-002", valence=0.12, arousal=0.88, segment_index=1)]
)
print(updated.aggregates)
```

## 交付内容

| 路径 | 作用 |
|---|---|
| `dialog_emotion/` | 可集成领域组件与全部 DAG 节点 |
| `dialog_emotion/pipeline.py` | 增量编排、失效集合、全量/增量比较 |
| `dialog_emotion/cache.py` | 内容寻址缓存、配置快照、文件锁 |
| `dialog_emotion/lineage.py` | 旧分数追溯与按缓存键重放 |
| `scripts/generate_samples.py` | 离线确定性样本生成 |
| `scripts/demo.py` | 完整 JSON 演示报告 |
| `tests/` | 31 项自动化测试 |
| `data/samples/` | 已生成 PCM、真值变体、对话/注册清单 |
| `artifacts/demo-report.json` | 已执行验收产生的完整 JSON 报告 |
| `TECHNICAL.md` | 键设计、失效传播和追溯算法说明 |
| `VERIFICATION.md` | 实际命令、结果、需求追踪和限制 |

## 数据格式

对话清单 `dialogs.jsonl` 包含 `dialog_id`、`sample_rate`、`duration_ms`、
`speaker_ids`、`seed`，并附 `pcm_file` 与 `truth_files`。注册清单
`speakers.jsonl` 每行包含 `speaker_id`、`register_seed`、`register_ms`。

每个真值变体为 JSONL，核心字段为：

```json
{"frame_index":0,"speaker_id":"spk_b","valence":0.66,"arousal":0.42,
 "is_speech":true,"time_ms":10.0,"frame_ms":20.0,"hop_ms":10.0}
```

帧长 20 ms。默认同时生成帧移 10 ms 和 15 ms 两个真值变体，使“调整帧移”
仍能与逐帧真值帧数严格对齐。

## DAG 与缓存边界

```text
frame_features
  -> speaker_assignment
  -> segment_boundary
  -> segment_regression (每段一个缓存节点)
  -> dialog_aggregate
```

缓存键分为两部分：

- `identity`：直接决定节点输出的依赖，参与键哈希。包括音频分块内容哈希、
  相关注册样本版本、帧移、切段阈值、真值作用域哈希和上游内容哈希。
- `audit_context`：当前全局配置/真值/注册版本，写入条目以便追溯，但不是
  直接依赖。未变化的帧特征不会因“追加样本”而失效；单段修正也不会改变
  其他段的真值作用域哈希。

这不是遗漏版本，而是精确失效：例如帧特征不依赖注册样本或真值，因此这些
版本只作为审计上下文；真正依赖它们的下游节点把这些输入纳入 `identity`，
并通过上游内容哈希获得完整传递依赖。

每个缓存条目包含：

- 64 位 SHA-256 键和产物 `content_hash`；
- `key_components.identity` / `key_components.audit_context`；
- 单调递增的条目 `version`、父节点键和整条 JSON 的 `checksum`。

配置快照每次变化都写入完整的新快照并原子替换 `current.json`，旧版本保留在
`archive/` 中。同一键的并发提交使用非阻塞 `O_EXCL` 文件锁，后到者返回
`E_STAGE_BUSY`。

## 增量语义

| 变化 | 复用 | 重算 |
|---|---|---|
| 追加某说话人注册样本 | 全部帧特征；不包含该说话人的对话全部节点 | 相关对话的指派及其实际变化的边界/段分数/聚合 |
| 修正单段真值 | 帧特征、指派、边界、其他段 | 该段回归、对话聚合 |
| 调整帧移 | 不适用音频特征，注册数据仍独立 | 帧特征及受影响后缀 |
| 调整切段阈值 | 帧特征、逐帧指派 | 边界、实际变化的段回归、聚合 |

`run()` 报告同时给出 `reused_nodes`、`recomputed_nodes`、
`affected_nodes`、受影响帧/段、自检结果。`compare_runs()` 按 DAG 拓扑顺序
比较所有阶段产物，不一致返回 `E_INCREMENTAL_DRIFT` 和首个偏离节点。

## 追溯

每个段级/对话级输出都写入持久谱系记录：

- 节点路径，例如 `frame_features -> speaker_assignment ->
  segment_boundary -> segment_regression -> dialog_aggregate`；
- 每个阶段的缓存键；
- 注册版本、真值修正版本、配置快照版本与内容哈希；
- 当时的段级分数。

`LineageStore.replay(record_id, cache)` 验证所有键和条目，再从缓存帧特征和
记录的真值快照重算段分数，从缓存段分数与权重重算对话聚合。缺键或重算不
一致分别返回 `E_LINEAGE_BROKEN`，并在 `missing_key_level` 指出缺失层级。

## 业务错误码

| 错误码 | 场景 |
|---|---|
| `E_SAMPLE_RATE` | 采样率不是 16000 |
| `E_FRAME_INVALID` | `frame_ms` 非法或无法对齐样本 |
| `E_FRAME_SHIFT_INVALID` | 帧移非法或超过帧长 |
| `E_DUPLICATE_DIALOG` | 对话清单重复 `dialog_id` |
| `E_SPEAKER_MISSING` | 注册行缺失 `speaker_id` |
| `E_FRAME_MISMATCH` | 特征帧数与真值不同，含 `dialog_id` 和实际帧数 |
| `E_SPEAKER_UNKNOWN` | 指派标签不在对话 `speaker_ids` 中 |
| `E_SEGMENT_GAP` | 段不连续或未覆盖全部帧 |
| `E_INTERVAL_INVALID` | 置信区间上下界顺序非法 |
| `E_CACHE_CORRUPT` | 缓存/配置快照校验和或内容哈希损坏 |
| `E_STAGE_BUSY` | 同一缓存键存在并发提交 |
| `E_RESOURCE_LIMIT` | 缓存条目、候选比较或驻留特征内存超限 |
| `E_INCREMENTAL_DRIFT` | 增量结果与全量运行不同，含首个偏离节点 |
| `E_LINEAGE_BROKEN` | 谱系缺记录、缺键或分数无法复现，含缺失层级 |

没有站点、容器或网络调用。`E_RESOURCE_LIMIT` 采用显式失败策略，不静默降级。

## 资源上限

`PipelineConfig.limits` 可配置：

- `max_cache_entries`
- `max_candidate_comparisons`
- `max_resident_feature_bytes`

默认分别为 4096、5,000,000 和 128 MiB。超出即返回 `E_RESOURCE_LIMIT`，
详情中包含实际值和限制值。

## 验收摘要

本次实际执行结果：

- `pytest -q`：`31 passed`；
- 样本帧数与真值完全一致，例如 `dlg-001` 在 20/10 下均为 299 帧，
  在 20/15 下均为 199 帧；
- 四类增量事件全部通过自检；
- 增量最终状态与全新缓存全量运行的 22 个节点哈希全部一致；
- 基线旧段级/对话级分数在多次变更后可重放且分数相同；
- 演示报告覆盖 14 个业务码场景。

更完整的命令输出、哈希和需求追踪见 `VERIFICATION.md`。
