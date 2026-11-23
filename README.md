# 事件共指链增量合并与冲突仲裁管线

这是一个完全本地、确定性的搜索推荐事件前链路样例项目。它从受控实体词典、事件类型表和规则配置生成事件 JSONL，按批次做事件解析、候选共指证据生成、增量链合并、链间/链内仲裁，并用一次全量重建和独立复核工具验证增量结果。

项目不依赖外部数据或网络服务。算法本身只使用 Python 标准库；`numpy` 按题面环境要求列入依赖。

## 快速开始

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

python generate_samples.py --out samples --force
python run_pipeline.py run `
  --sample-dir samples `
  --config samples\coref_rules.json `
  --output output\report.json `
  --cache-dir .state\cache `
  --snapshot-dir .state\snapshots

python verify_results.py --report output\report.json `
  --config samples\coref_rules.json `
  --output output\verification.json

pytest
```

Linux/macOS 下把续行符换成 `\`，虚拟环境激活命令改为 `source .venv/bin/activate`。`run` 也可用 `python -m coref_pipeline.cli run ...` 调用。

`--batch-order 1,2,3` 用于指定参与运行的批次。入口会去重、校验并始终按批次号升序执行，以保证题面要求的确定性摄入顺序。

## 项目结构

| 路径 | 作用 |
|---|---|
| `coref_pipeline/generator.py` | 固定种子样本、受控词典、类型表、规则配置生成 |
| `coref_pipeline/ingest.py` | JSONL 读取、字段校验、规范化和内容摘要 |
| `coref_pipeline/evidence.py` | 实体/时间/触发词/可信度分项打分与候选过滤 |
| `coref_pipeline/chains.py` | 增量链、链间冲突、链内论元裁决、复用账本 |
| `coref_pipeline/pipeline.py` | 批次编排、全量重建、一致性自检、报告 |
| `coref_pipeline/cache.py` | 事件/证据/链状态的内容寻址缓存 |
| `coref_pipeline/snapshot.py` | 版本化整体快照与非阻塞提交锁 |
| `coref_pipeline/verify.py` | 不信任链摘要的独立报告复核 |
| `coref_pipeline/cli.py` | `run`、`generate`、`verify` 三个子命令 |
| `tests/` | 28 项本地测试，覆盖题面八类要求 |

## 确定性数据契约

事件 JSONL 每行是一个对象，核心字段为：

```json
{
  "event_id": "ev-0001",
  "doc_id": "doc-01-001",
  "batch_no": 1,
  "event_type": "PRODUCT_LAUNCH",
  "entities": [{"name": "Nova Robotics", "role": "ORG"}],
  "time_expr": "2026-11-20",
  "mentions": [{"trigger": "发布", "start": 0, "end": 2}],
  "source_trust": 0.95
}
```

所有 JSON 产物使用同一规范化规则：UTF-8、键排序、无多余空白、拒绝 NaN/Inf。事件按 `(batch_no, event_id)` 排序；实体按 `(role, name)`、mention 按 `(start, end, trigger)` 排序。因此同输入、同配置下的规范化记录和证据输入摘要逐字节稳定。

校验失败不会跳过记录，而是抛出带阶段、事件和字段的业务错误：

| 业务码 | 阶段 | 含义 |
|---|---|---|
| `E_DUP_EVENT` | `ingest` | `event_id` 重复 |
| `E_TYPE_INVALID` | `ingest` | 事件类型不在受控表 |
| `E_OFFSET_INVALID` | `ingest` | mention 码点偏移非法 |
| `E_ROLE_INVALID` | `ingest` | 实体角色不在受控表 |
| `E_EVIDENCE_CONFIG` | `evidence_config` | 权重、下限、时间规则或词形口径非法 |
| `E_LINK_CONFLICT` | `chain_merge` / `chain_validation` | 同一事件进入两条链或证据冲突 |
| `E_ORPHAN_EVENT` | `chain_merge` / `chain_validation` | 链包含悬挂事件或证据引用 |
| `E_ARBITRATION_UNDECIDED` | `argument_arbitration` | 论元冲突无法按显式指标裁决 |
| `E_INCONSISTENT` | `consistency` / `verification` | 增量、全量或独立重算不一致 |
| `E_RESOURCE_LIMIT` | `resource_limit` | 批次、比较次数或驻留内存超限 |
| `E_SNAPSHOT_ROLLBACK` | `snapshot` | 尝试用旧版本覆盖新版本 |
| `E_SNAPSHOT_CORRUPT` | `snapshot` | 版本、校验和或内容损坏 |
| `E_STAGE_BUSY` | `commit` | 已有增量提交占用阶段锁 |

## 证据算法

事件对先按 `event_id` 排序，分数统一量化到 6 位小数。总分为四个分项贡献之和：

```text
entity       = w_entity * min(1, Σ(命中实体角色权重) / max(两侧实体数))
time         = w_time   * 1[时间表达式等价]
trigger      = w_trigger * max_mention_pair(触发词词形相似度)
source_trust = w_source * sqrt(trust_a * trust_b)
score        = q6(entity + time + trigger + source_trust)
```

证据记录保存每个分项的 `weight/raw/contribution`、命中实体、两个触发词及其码点位置、时间等价规则和配置版本。独立验证只用 `components` 即可重算总分和内容哈希。

仅 `score >= candidate_floor` 的事件对进入候选集合。触发词口径支持：

- `char_bigram_jaccard`：字符二元组集合 Jaccard；
- `sequence_matcher`：`difflib.SequenceMatcher(autojunk=False)`；
- `normalized_levenshtein`：归一化 Levenshtein 相似度；
- `exact`：规范化后完全相等。

## 增量链与仲裁

每个批次按事件号升序遍历。新事件只对“包含候选证据另一端点”的既有链提出并入方向：

1. 没有候选链：建立单例链；
2. 只有一个候选链：并入；
3. 多个候选链：按配置的 `chain_priority` 逐项比较：
   - `evidence_total`：现有证据总量与本次提案证据总量之和，大者优先；
   - `chain_size`：链规模大者优先；
   - `first_seen`：首次出现批次与事件号更早者优先。

被拒绝方向写入 `rejected_directions`，包含候选链、支持证据、比较指标和首个不同指标。未被新增事件触及的链不执行 `_finalize_chain`，其 `state_hash` 原样复用；报告逐批输出 `touched/recomputed/reused/untouched` 链集合和复用状态哈希。

同一链内同一角色出现不同实体值时，候选值按以下顺序裁决：

1. `role_weight * max(source_trust)`；
2. 支持记录的 `sum(source_trust)`。

胜者进入 `resolved_arguments`，落败值、来源事件、可信度和分项贡献完整保留在 `losers`。若两项指标仍完全相同，返回 `E_ARBITRATION_UNDECIDED` 并列出冲突证据。

## 全量重建与独立复核

每次运行都会从空状态按同一批事件和有效下限再做一次全量重建，逐链比较：

- 链成员集合；
- 链内证据集合；
- 论元裁决结果；
- 全部链间和链内仲裁记录。

首次偏差写入 `E_INCONSISTENT.details`，包含链、事件和证据。`verify` 工具不信任链摘要，独立重算：

- 每个证据的内容哈希、分项乘积、总分和下限；
- 事件是否恰好出现一次；
- 链内证据总量、连通性和状态哈希；
- 每个角色的论元胜者与落败证据；
- 链冲突的优先级、胜者和拒绝理由；
- 增量与全量的成员及仲裁记录。

## 缓存、失效与快照

缓存目录按三类内容寻址落盘：

- `normalized_event`：规范化事件正文 + 受控表哈希；
- `evidence`：两端事件哈希、规则总哈希、权重版本、时间规则版本、触发词版本、原始下限、有效下限和上游哈希；
- `chain_state`：链 ID、成员事件哈希、证据哈希、配置和规则版本。

修正一个事件的实体角色时，事件缓存只失效该事件；证据缓存只失效涉及它的事件对；链状态缓存按成员/证据哈希失效。无关事件、证据和链直接命中。调整下限、权重版本、时间规则版本或词形口径会使相应下游缓存失效，并始终重新执行增量/全量一致性检查。

`--cache-trace` 会把命中/未命中键以及 `affected/reused` 事件、证据对、链集合写入报告。默认报告不包含运行时缓存计数，因此冷缓存与热缓存两次运行的报告仍逐字节一致。

快照目录包含索引和每个版本的完整 JSON 文件。每个快照携带单调递增版本号和内容校验和；更新时写完整新文件再原子替换索引，不修改旧文件。版本回退返回 `E_SNAPSHOT_ROLLBACK`，文件或校验和损坏返回 `E_SNAPSHOT_CORRUPT`。提交阶段用 `O_CREAT|O_EXCL` 锁，后到者立即得到 `E_STAGE_BUSY`，不阻塞等待。

## 资源上限与降级

`resource_limits` 支持：

- `max_batch_events`：超限返回 `E_RESOURCE_LIMIT`；
- `max_pair_comparisons`：`resource_limit_mode=error` 时返回 `E_RESOURCE_LIMIT`；`degrade` 时把有效下限提升到 `degraded_candidate_floor`，只接受更高证据候选，并在报告标记 `higher_evidence_floor`；
- `max_resident_chain_mb`：按链状态规范化 JSON 的字节数估算，超限返回 `E_RESOURCE_LIMIT`。

报告顶层包含 `resource`、`degradation`、`errors`、`business_codes`；CLI 失败时也会写出同结构的错误报告和 `.sha256` 侧文件，不会静默丢弃事件。

## 样本与验证记录

固定种子 `20261124` 生成 14 个事件、4 个批次、18 条候选证据、5 条共指链。样本刻意覆盖：

- 同一产品发布跨 4 个批次；
- 时间别名与触发词变体；
- 事件 `ev-0009`、`ev-0014` 同时获得两条链的候选，按证据总量仲裁并保留被拒绝方向；
- `PRODUCT` 与 `PERSON` 角色冲突，落败证据保留；
- 多个与新增事件无关的链在中间批次状态哈希不变；
- 全部链的增量和全量结果逐链一致。

实际验证环境为 Linux 上的 Python 3.10.12（兼容实现，代码仅使用 3.10+ 语法并避免平台相关路径写法）；题面建议的 Windows Python 3.11/3.12 未在当前容器中可用。最终执行结果：

```text
pytest: 28 passed
run:    status=ok, consistency=true, verification=true
sample: 14 events / 4 batches / 18 evidence / 5 chains
```

可归档报告位于 `validation/report.json`、`validation/verification.json`，样本与清单位于 `samples/`。
