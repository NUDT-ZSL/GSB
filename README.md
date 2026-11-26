# Entity Link Lab

一个完全本地、确定性的实体提及聚合与链接链溯源组件。它从固定种子生成 NER JSONL、跨句候选清单、v1/v2 规则快照和 SQLite 元数据库，完成候选证据、精确/近似双路径、冲突仲裁、增量失效、规则迁移、重放验证和检查点恢复。

## 快速开始

```bash
python3 --version                 # 需要 3.10+
python3 -m pip install numpy      # 环境已有 numpy 时会直接满足
python3 seed_data.py              # 生成 data/ 下的固定种子样本

python3 -m entity_link_lab run \
  --data data \
  --output output \
  --cache-dir cache \
  --checkpoint-dir checkpoints

python3 -m entity_link_lab incremental \
  --data data \
  --report output/report.json \
  --delta data/incremental_delta.json \
  --output output/incremental_report.json \
  --cache-dir cache

python3 run_acceptance.py
python3 -m pytest
```

当前容器实测 Python 3.10.12、NumPy 2.0.0；全套 24 项测试约 4.6 秒，一键验收约 2.6 秒，单次完整管线约 0.7 秒，满足 12 秒预算。

## 固定种子产物

`python3 seed_data.py` 会生成：

- `data/mentions.jsonl`：每行一个句子，含 `doc_id`、`sent_id`、`sentence` 和 `mentions`；mention 含 `mention_id`、`name`、`entity_type`、句子内码点区间 `start/end`。
- `data/coref_candidates.jsonl`：固定跨句共指候选清单。
- `data/rules_v1.json`：单文件 v1 规则，使用 `relative_effective_seconds` 与 `feature_*` 扁平描述。
- `data/rules_v2/manifest.json`、`data/rules_v2/rules-000.json`、`data/rules_v2/rules-001.json`：v2 分片规则。
- `data/rules_v2/rules_meta.db`：SQLite 元数据库，记录绝对毫秒、规则/代数、时间线、墓碑和规则内容哈希。
- `data/incremental_delta.json`：新增句子与修订 mention 的确定性增量。
- `data/seed_manifest.json`：种子、数量和文件清单。

种子值为 `20261127`。生成器会初始化 `random.Random(20261127)`，随后仍按 `(doc_id, sent_id, start, end, type, id)` 稳定排序。

## 常用命令

```bash
# 规则迁移
python3 -m entity_link_lab migrate \
  --direction v1-to-v2 \
  --source data/rules_v1.json \
  --target validation/migration/v2

python3 -m entity_link_lab migrate \
  --direction v2-to-v1 \
  --source data/rules_v2/manifest.json \
  --target validation/migration/rules_v1_fold.json \
  --mode fold

# 独立重放 output/report.json 中的证据哈希和缓存键
python3 -m entity_link_lab replay --report output/report.json

# 查看报告中的自检结果
python3 -m entity_link_lab selfcheck --report output/report.json

# 模拟处理信号 / 强制终止
python3 -m entity_link_lab run --signal-after 3 \
  --checkpoint-dir /tmp/ell-signal --output /tmp/ell-out
python3 -m entity_link_lab run --kill-after 3 \
  --checkpoint-dir /tmp/ell-kill --output /tmp/ell-out
```

## 输出说明

一次完整运行会写入：

- `output/report.json`：自包含报告，含链、冲突裁决、trace、自检、重放和计数。
- `output/candidates_exact.jsonl`：精确路径候选与全特征证据。
- `output/candidates_approx.jsonl`：近似路径证据与剪枝项。
- `output/approx_plan.json`：类型签名桶、桶内过滤数量、过滤依据和剪枝特征字典。
- `output/chains.json`：链接链、并查集结果、置信度和链校验和。
- `output/traces.json`：规则代数、证据哈希、组件哈希、缓存键和剪枝特征。
- `output/self_check.json`、`output/replay.json`：一致性与独立重放结果。
- `output/incremental_report.json`：受影响 mention、重算 pair、缓存复用闭包和规则代数跳变依据。
- `validation/acceptance_report.json`：一键验收的实际结果。
- `validation/pytest.txt`：最近一次 pytest 输出。

## 确定性与平局规则

所有 JSON 哈希使用 UTF-8、键排序、无空白、拒绝 `NaN` 的规范字节。候选稳定顺序为：

1. `doc_id` 升序；
2. 首个 mention 的全局顺序；
3. 次个 mention 的全局顺序；
4. 首个 mention 码点 `start` 升序；
5. 实体类型字典序；
6. `pair_key` 最终兜底。

冲突裁决顺序为总分降序、`algebra_generation`（规则代数）升序、`rule_generation` 升序、首个码点升序、类型字典序、`pair_key` 升序。每个后续 mention 只选择一个主父节点；所有高于阈值的竞争候选和首个差异指标都会写入 `conflict_decisions`。

## 打分口径

分数使用基点整数（bps，满分 10000），四个贡献项为：

- `shared_proportion`：类型、名称 bigram/token、外围上下文的共享比例；
- `distance_decay`：句间距离与码点距离的 NumPy 指数衰减；
- `document_scale`：文档 mention 规模对应的上下文稳定性；
- `rule_algebra`：规则代数偏置与 generation 增量。

每个组件记录 `weight_bps`、`raw_bps`、`contribution_bps` 和整数重算公式；`total_bps` 等于各贡献之和，可独立重算。

## 错误码

| 错误码 | 触发条件 |
| --- | --- |
| `E_VERSION_UNSUPPORTED` | 规则版本头存在但不是 v1/v2 支持值 |
| `E_FORMAT_CORRUPT` | 必填字段缺失、JSON/SQLite 校验失败、时间线或元数据库时间戳倒退 |
| `E_SEMANTIC_DRIFT` | 迁移策略期望的内部规范表示与重读结果不逐字节一致 |
| `E_STALE_LINK` | trace 仍由旧缓存键/旧依赖版本提供结果 |
| `E_STAGE_BUSY` | 非阻塞提交锁已被另一个提交占用，或检查点父版本冲突 |
| `E_INCONSISTENT` | 双路径、溯源覆盖或失效覆盖自检失败 |
| `E_CHECKPOINT_DIRTY` | 检查点 JSON、版本或内容校验和损坏 |
| `E_DUPLICATE_CHAIN` | 同一会话内重复注册同一 `chain_id` |

## 代码地图

- `entity_link_lab/seed.py`：固定种子样本和规则书。
- `entity_link_lab/ingest.py`：JSONL、码点区间和确定性文本特征。
- `entity_link_lab/rules.py`：v1/v2 读取、SQLite 校验、规范化和双向迁移。
- `entity_link_lab/evidence.py`：候选生成、精确/近似路径、NumPy 距离衰减和证据打分。
- `entity_link_lab/chains.py`：冲突仲裁、并查集、链身份、trace 和会话注册表。
- `entity_link_lab/cache.py`：内容寻址缓存键与依赖哈希。
- `entity_link_lab/incremental.py`：delta 应用、失效域、定向重算和闭包复用。
- `entity_link_lab/checkpoint.py`：四阶段检查点、原子提交、`flock` 并发锁和恢复判定。
- `entity_link_lab/pipeline.py`：检查点驱动的完整管线。
- `entity_link_lab/replay.py`：证据/组件/缓存键独立重放与陈旧链接判定。
- `entity_link_lab/selfcheck.py`：跨模块一致性自检。
- `entity_link_lab/acceptance.py`：一键验收和实际指标落盘。

更详细的状态机、迁移和恢复说明见 `docs/TECHNICAL.md`。
