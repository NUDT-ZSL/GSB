# 中文技术术语索引原型

这是一个本地可运行、确定性可复现的领域术语抽取与同名异义消歧原型。输入由固定种子脚本生成，不访问网络；运行结果包含 JSON 术语索引、SQLite 账本、五阶段快照、诊断报告和精确/近似路径对照报告。

## 运行环境

- Linux / WSL Ubuntu
- Python 3.10+
- NumPy
- Python 标准库 `sqlite3`

本工作区实际验证环境：

```text
Python 3.10.12
NumPy 1.26.4
SQLite 3.37.2
```

安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

## 快速开始

```bash
# 1. 生成固定种子样本与配置
python3 tools/generate_samples.py --output .

# 2. 运行五阶段流水线
python3 main.py run --config config.json

# 3. 自检索引、SQLite 账本和逐文档明细
python3 main.py verify --config config.json

# 4. 严格比较精确路径与受限近似路径
python3 main.py compare --config config.json --strict

# 5. 运行完整测试
python3 -m unittest discover -s tests -v
```

也可以使用：

```bash
make generate
make run
make verify
make compare
make test
```

## 输出文件

| 文件 | 内容 |
| --- | --- |
| `out/term_index.json` | 按术语名和义项编号聚合的术语索引、首次出现文档、出现次数、置信度摘要 |
| `out/ledger.sqlite3` | 每个文档的候选、消歧结果、取舍记录、预算准入记录 |
| `out/diagnostics.json` | 规模观测、路径选择、预算计划、规则定向失效、阶段快照元数据、自检结果 |
| `out/path_comparison.json` | 精确/近似路径的术语集合差异、义项置信度偏差、覆盖召回和贡献重算明细 |
| `out/budget_plan.json` | 预算降批演示：准入批次、拒绝文档、降级类别和触发预算项 |
| `snapshots/01_validate_normalize.json` ... `05_export_index.json` | 五阶段原子快照 |

样本规模：32 个文档、4 个分片、19 个术语表条目、87 条上下文观测、两套规则快照。当前运行产出 93 个候选术语实例、24 个索引条目、93 条消歧记录。

## 输入格式

`config.json` 指定数据路径、术语表版本、上下文版本、规则快照版本、打分参数、自适应阈值和双重预算。

文档分片为 JSONL，每行：

```json
{"shard_id":"shard-000","doc_id":"doc-0001","para_no":1,"text":"...","char_len":38}
```

`char_len` 必须等于规范化文本的 Unicode 码点数。候选偏移也是码点偏移，`start` 含、`end` 不含。

术语表 `data/glossary.json` 的条目包含 `entry_no`、`term_id`、`term`、`source`、`source_weight`、`length_tier`、`rule_version` 和 `senses`。抽取采用最长优先、非重叠扫描；同一起点、同一边界时选择 `entry_no` 最小的条目。

`data/context_features.json` 包含特征目录和逐文档码点范围观测，例如：

```json
{"doc_id":"doc-0001","para_no":1,"feature_id":"F_NET","start":10,"end":14,"value":"网络协议"}
```

`data/rules/rules-v1.json` 和 `rules-v2.json` 是两套不同口径的消歧规则：

- `rules-v1`：加权证据口径，同一特征最多累计三次。
- `rules-v2`：二值存在口径，并叠加特征目录权重。

`config.json` 的 `rule_version` 决定使用哪一套。

## 算法与确定性

候选置信分数由三项可审计贡献相加：

```text
score =
  length_weight[length_tier]
  + glossary_source_weight * source_weight_scale
  + coverage_weight * context_coverage
```

`context_coverage` 为候选前后 `context_window_chars` 窗口内特征观测的加权重叠占比，上限 1.0。每个候选保存 `score_breakdown.components`，可独立重算。

消歧时，对每个义项累加规则贡献和义项先验，取得分最高者；同分选择义项编号较小者。每条规则贡献都记录特征是否存在、证据次数、规则权重和最终贡献。

自适应选择器观察分片数、单文档最大候选数、平均上下文特征数、平均候选数和驻留内存余量，选择：

- `exact`：比较全部候选和全部上下文特征。
- `approximate`：按置信度对候选截断，并按 `(-特征权重, feature_id)` 对特征截断；仍覆盖全部文档。

比较次数与内存准入同时受 `total_compare_budget` 和 `max_resident_cache_mb` 约束。比较预算优先检查；超限时严格模式返回 `E_COMPARE_BUDGET`，否则返回 `E_RESOURCE_LIMIT`（若比较预算未超）。报告模式会给出拒绝文档的规模观测、降批大小、降级类别和触发预算项。

## 阶段快照与恢复

五个阶段：

1. `validate_normalize`
2. `extract_candidates`
3. `score_candidates`
4. `disambiguate_terms`
5. `export_index`

每个阶段只在全部文档完成后写入固定文件名的快照，使用临时文件、`fsync` 和 `os.replace` 原子整体替换。快照包含单调递增版本号、已完成文档集合、SHA-256 内容校验和、基础输入指纹和规则指纹。

恢复规则：

- 输入、术语表、上下文或基础配置变化：清空旧快照，从阶段 1 重建。
- 仅规则快照变化：保留阶段 1-3，从阶段 4 定向重建；无多义候选的文档复用旧消歧结果，报告 `invalidated_doc_ids` 和 `reused_doc_ids`。
- 版本回退：`E_SNAPSHOT_ROLLBACK`。
- JSON 损坏或内容校验和不符：`E_SNAPSHOT_CORRUPT`。

强制终止恢复验证：

```bash
# 在隔离临时目录中运行连续基线、SIGKILL 中断、恢复，并比较索引与账本
python3 tools/recovery_check.py --config config.json --stage 3
```

也可以直接观察强制终止：

```bash
python3 main.py run --config config.json --crash-after-stage 3
python3 main.py run --config config.json
```

第二条命令从已提交的阶段 4 继续。为避免覆盖当前工作区快照，建议先使用 `tools/recovery_check.py` 的隔离目录模式。

## 错误注入

运行全部错误注入：

```bash
python3 main.py inject-all --root .
```

单独运行：

```bash
python3 main.py inject-error --case dup_doc --root .
python3 main.py inject-error --case char_len --root .
python3 main.py inject-error --case term_entry_invalid --root .
python3 main.py inject-error --case context_range --root .
python3 main.py inject-error --case weight_negative --root .
python3 main.py inject-error --case feature_unknown --root .
python3 main.py inject-error --case sense_out_of_range --root .
python3 main.py inject-error --case rule_version_unknown --root .
python3 main.py inject-error --case compare_budget --root .
python3 main.py inject-error --case resource_limit --root .
python3 main.py inject-error --case path_divergence --root .
python3 main.py inject-error --case snapshot_rollback --root .
python3 main.py inject-error --case snapshot_corrupt --root .
python3 main.py inject-error --case index_inconsistent --root .
```

| 业务错误 | 触发条件 |
| --- | --- |
| `E_DUP_DOC` | 多个分片行使用同一 `doc_id` |
| `E_CHAR_LEN_MISMATCH` | `char_len` 与规范化文本码点数不符 |
| `E_TERM_ENTRY_INVALID` | 术语表条目缺字段、类型错误、重复 `entry_no`、无效义项等 |
| `E_CONTEXT_RANGE_INVALID` | 特征范围越界或文档引用无效 |
| `E_WEIGHT_NEGATIVE` | 特征、规则或先验权重为负 |
| `E_FEATURE_UNKNOWN` | 规则或观测引用未知上下文特征 |
| `E_SENSE_OUT_OF_RANGE` | 规则义项编号不在术语条目中 |
| `E_RULE_VERSION_UNKNOWN` | 配置选择不存在的规则快照版本 |
| `E_COMPARE_BUDGET` | 比较预算不足（优先判定） |
| `E_RESOURCE_LIMIT` | 驻留缓存预算不足 |
| `E_PATH_DIVERGENCE` | 精确/近似路径导致术语索引统计不可比 |
| `E_SNAPSHOT_ROLLBACK` | 尝试写入不大于当前版本的快照 |
| `E_SNAPSHOT_CORRUPT` | 快照 JSON 损坏、缺字段或校验和不符 |
| `E_INDEX_INCONSISTENT` | 索引/账本/逐文档统计不一致 |

## 预算降批示例

```bash
python3 main.py budget-plan \
  --policy report \
  --total-compare-budget 50 \
  --max-cache-mb 128
```

输出写入 `out/budget_plan.json`，包含 `batch_sizes`、`degradation.category`、`degradation.trigger_budget`、被拒文档和每个文档的规模观测。

## 当前验证记录

- `python3 -m unittest discover -s tests -v`：13 项测试全部通过。
- 主程序：32 文档、93 候选、24 索引条目、93 消歧记录。
- `verify`：候选集合、消歧记录、索引统计和逐项贡献重算全部一致。
- `compare --strict`：两条路径 `comparable`，覆盖召回 1.0。
- `inject-all`：14/14 业务错误码符合预期。
- SIGKILL 后恢复：阶段 1-5 的每个提交点中断后都恢复为与连续运行逐字节相同的 `term_index.json`，SQLite 逻辑校验和一致。
- 规则从 `rules-v1` 切换到 `rules-v2`：定向失效 27 个含多义候选的文档，复用 5 个无多义候选文档。

更详细的设计说明见 `docs/DESIGN.md`，实际验证记录见 `docs/VALIDATION.md`。

