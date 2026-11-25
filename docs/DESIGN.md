# 设计说明

## 1. 模块边界

| 模块 | 职责 |
| --- | --- |
| `term_pipeline/ingest.py` | JSONL 读取、重复文档检测、NFC 规范化、码点长度校验 |
| `term_pipeline/glossary.py` | 术语表格式校验与索引 |
| `term_pipeline/extraction.py` | 最长优先、同边界 `entry_no` 裁决、候选 ID 和偏移 |
| `term_pipeline/features.py` | 特征目录/观测校验和窗口查询 |
| `term_pipeline/scoring.py` | 三项置信贡献和可重算明细 |
| `term_pipeline/rules.py` | 规则快照发现、版本校验、特征/义项引用校验 |
| `term_pipeline/disambiguation.py` | 两套规则口径、确定性义项选择、路径限制 |
| `term_pipeline/adaptive.py` | 精确/受限近似选择器和候选截断 |
| `term_pipeline/budget.py` | 比较预算和驻留内存双重准入 |
| `term_pipeline/comparison.py` | 跨路径差异分类和不可比判定 |
| `term_pipeline/snapshots.py` | 五阶段原子快照和回退/损坏检测 |
| `term_pipeline/ledger.py` | SQLite 账本和逻辑校验和 |
| `term_pipeline/indexer.py` | 术语索引聚合和从账本重算 |
| `term_pipeline/verification.py` | 跨阶段一致性自检和首个偏差定位 |
| `term_pipeline/pipeline.py` | 阶段编排、恢复、定向失效、产物导出 |
| `term_pipeline/injection.py` | 14 类业务错误注入 |

## 2. 数据契约

### 文档

每条 JSONL 行必须包含 `shard_id`、`doc_id`、`para_no`、`text`、`char_len`。`doc_id` 全局唯一。读取时先按配置执行 NFC 规范化，再以规范化文本的 `len()` 作为实际码点数。

### 术语表

术语条目允许同名词条，但 `entry_no` 必须唯一。候选扫描按：

```text
(-len(term), entry_no)
```

排序后在同一码点位置取第一条匹配。匹配成功后游标跳过该词长度，因此候选不重叠。候选 ID 由文档、起止偏移、条目编号、术语 ID、命中规则版本和术语表版本共同哈希，保证稳定。

### 上下文特征

观测范围采用码点 `[start, end)`，必须满足：

```text
0 <= start <= end <= doc.char_len
```

负特征权重在目录和观测两处都检查。规则引用不存在的特征时返回 `E_FEATURE_UNKNOWN`。

## 3. 置信打分

候选上下文窗口：

```text
window = [max(0, start - R), min(char_len, end + R)]
```

其中 `R = scoring.context_window_chars`。

窗口覆盖度：

```text
weighted_overlap = sum(overlap(observation, window) * observation.weight)
coverage = min(1.0, weighted_overlap / len(window))
```

最终分数：

```text
score = round(
    length_weight[tier]
  + source_weight * source_weight_scale
  + coverage * coverage_weight,
  round_digits
)
```

每一项都保存在 `score_breakdown.components`。验证器只依赖这份明细重算，不依赖候选提取阶段的内存状态。

## 4. 消歧

规则快照按 `term_id` 和 `sense_id` 分组。每个义项分数：

```text
sense_score = prior + sum(rule_contribution)
```

`weighted_evidence` 口径：

```text
rule_contribution = rule_weight * min(3, evidence_count)
```

`binary_presence` 口径：

```text
rule_contribution = rule_weight * catalog_weight * (1 if evidence_count > 0 else 0)
```

选择键为：

```text
(-sense_score, sense_id)
```

因此同分自动选择较小义项编号。

## 5. 精确/近似路径

自适应观测包含分片数、文档数、最大/平均候选数、平均上下文特征数和驻留内存余量。只要任一精确阈值超限即选 `approximate`，并记录原因码。

近似限制：

- 候选按 `(-confidence, start, end, candidate_id)` 排序后保留前 N 个。
- 特征按 `(-catalog_weight, feature_id)` 排序后保留前 M 个。
- 文档本身不截断，覆盖集合始终等于全部输入文档。

跨路径报告将差异分为：

- `candidate_pruning`：近似路径缺少精确路径的候选。
- `context_comparison`：同一候选因特征截断产生义项或分数变化。
- `statistical_mismatch`：集合相同但聚合统计仍不一致。

不可比条件包括候选遗漏、术语/义项集合差异、各义项计数差异、覆盖召回低于阈值或候选置信度偏差超过容差。严格比较和主流水线导出前都会拒绝不可比结果，返回 `E_PATH_DIVERGENCE`。

## 6. 预算

每文档估算：

```text
comparisons = candidate_count * max(1, feature_count)
compare_cost = candidate_count * candidate_compare_cost
             + feature_count * feature_compare_cost
resident_bytes = fixed_doc_bytes
               + candidate_count * bytes_per_candidate
               + feature_count * bytes_per_feature
```

按 `doc_id` 排序逐个准入。每次先检查比较预算，再检查驻留缓存，因此二者同时超限时返回 `E_COMPARE_BUDGET`。拒绝文档保留完整规模观测。

## 7. 快照和失效

基础指纹覆盖：

- 文档分片原始字节哈希
- 术语表原始字节哈希
- 上下文特征原始字节哈希
- 规范化、打分、自适应和预算配置
- 引擎版本

规则指纹覆盖：

- 选中规则快照的规范化内容哈希
- 规则版本
- 消歧配置

基础指纹变化会清空全部快照。规则指纹变化只删除阶段 4-5；阶段 4 会检查每个文档是否包含多义候选：

- 含多义候选：失效并重算。
- 不含多义候选：复用旧阶段 4 结果，并更新规则版本标记。

快照写入固定文件名，内容校验和覆盖除 `checksum` 外的全部字段。版本必须严格递增。写入采用：

```text
temp file -> flush -> fsync -> os.replace -> fsync(directory)
```

## 8. SQLite 账本

账本表：

- `documents`：文档和选中路径。
- `candidates`：全部候选、基础置信分数明细、上下文窗口明细。
- `disambiguations`：选中路径的消歧结果、规则贡献和候选贡献明细。
- `tradeoffs`：每文档候选数、特征数、精确/近似比较估计、准入状态和降级信息。
- `meta`：模式、路径、术语表版本和规则版本。

索引可由 `disambiguations` 逐行重算。候选集合由 `candidates` 与阶段 2 快照逐文档比对。

## 9. 需求追踪

| 业务要求 | 实现 | 主要验证 |
| --- | --- | --- |
| 1 分片校验、规范化、最长优先抽取 | `ingest.py`, `glossary.py`, `extraction.py` | `test_extraction_longest_first_tie_and_offsets`, `dup_doc`, `char_len`, `term_entry_invalid` |
| 2 置信打分和上下文特征 | `features.py`, `scoring.py` | `test_scores_are_independently_recomputable`, `context_range`, `weight_negative` |
| 3 两套规则口径和同分裁决 | `rules.py`, `disambiguation.py` | `test_sense_tie_uses_lowest_sense_id`, `feature_unknown`, `sense_out_of_range`, `rule_version_unknown` |
| 4 自适应匹配取舍 | `adaptive.py` | `test_adaptive_selector_is_deterministic_and_covers_docs` |
| 5 跨路径一致性裁决 | `comparison.py` | `test_cross_path_reports_pruning_and_context_differences`, `path_divergence` |
| 6 双重预算和降批 | `budget.py`, `ledger.py` | `test_budget_degrades_with_explicit_trigger`, `compare_budget`, `resource_limit` |
| 7 五阶段快照和恢复 | `snapshots.py`, `pipeline.py` | `test_sigkill_recovery_matches_continuous_run`, `snapshot_rollback`, `snapshot_corrupt` |
| 8 索引导出、账本和自检 | `indexer.py`, `ledger.py`, `verification.py` | `test_all_required_business_errors` 的 `index_inconsistent`，`main.py verify` |

