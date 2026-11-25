# 实际验证记录

以下结果来自本工作区的本地运行，不依赖网络、Docker 或远程服务。

## 环境

```text
Python 3.10.12
NumPy 1.26.4
SQLite 3.37.2
```

## 样本生成

```bash
python3 tools/generate_samples.py --output .
```

结果：

```text
32 documents
4 shards
19 glossary entries
87 context observations
2 rule snapshots: rules-v1, rules-v2
```

两次独立生成的 `config.json`、术语表、上下文、规则和全部分片逐字节一致。

## 主程序

```bash
python3 main.py run --config config.json
```

结果：

```text
doc_count: 32
candidate_count: 93
index_entry_count: 24
index_occurrence_count: 93
selected_path: exact
snapshot_versions: [1, 2, 3, 4, 5]
selfcheck.ok: true
```

## 自检

```bash
python3 main.py verify --config config.json
```

结果：

```text
document_count: 32
candidate_count: 93
disambiguation_count: 93
tradeoff_count: 32
index_entry_count: 24
index_occurrence_count: 93
```

自检覆盖：

- 每个索引术语至少有一条消歧记录。
- 每个文档候选集合与阶段 2 快照和 SQLite 账本一致。
- 每个候选分数贡献之和等于记录分数。
- 每个消歧义项分数可由先验和规则贡献重算。
- 索引出现次数、首次文档和置信度摘要可由账本逐文档明细重算。

## 精确/近似路径

```bash
python3 main.py compare --config config.json --strict
```

默认样本结果：

```text
adjudication: comparable
index_stats_comparable: true
coverage_recall_estimate: 1.0
max_candidate_confidence_delta: 0.0
difference_classes: []
```

测试还构造了两种不可比场景：

- 近似路径只保留一个候选：产生 `candidate_pruning_omissions`。
- 近似路径只保留一个上下文特征：术语集合不变，但产生 `context_comparison_changes`。

两种场景严格比较均返回 `E_PATH_DIVERGENCE`，且贡献重算明细可通过。

## 规则版本定向失效

从 `rules-v1` 切换到 `rules-v2` 后重启：

```text
scope: targeted_documents
invalidated_doc_count: 27
reused_doc_count: 5
snapshot_versions: [1, 2, 3, 4, 5]
```

阶段 1-3 保留，阶段 4-5 以更高版本整体替换；无多义候选的 5 个文档复用旧消歧结果。

## 强制终止恢复

```bash
python3 tools/recovery_check.py --config config.json --stage 3
```

测试在阶段 1-5 的每个提交点后向子进程发送 `SIGKILL`。恢复结果：

```text
index_byte_identical: true
ledger_logical_checksum_identical: true
snapshot_versions: [1, 2, 3, 4, 5]
```

恢复后的 `out/term_index.json` 与连续运行逐字节一致。

## 预算降批

预算规划测试将总比较预算设置为前三份文档的成本之和：

```text
degraded: true
batch_sizes: [3]
degradation.category: COMPARE_BUDGET
degradation.trigger_budget: total_compare_budget
```

第四份及后续被拒文档均保留候选数、特征数、比较次数估计、比较成本和驻留字节估计。比较预算与内存同时为 0 时，优先级测试返回 `COMPARE_BUDGET`；仅内存不足时返回 `RESOURCE_LIMIT`。

## 错误注入

```bash
python3 main.py inject-all --root .
```

结果：

```text
14 / 14 passed
```

覆盖：

```text
E_DUP_DOC
E_CHAR_LEN_MISMATCH
E_TERM_ENTRY_INVALID
E_CONTEXT_RANGE_INVALID
E_WEIGHT_NEGATIVE
E_FEATURE_UNKNOWN
E_SENSE_OUT_OF_RANGE
E_RULE_VERSION_UNKNOWN
E_COMPARE_BUDGET
E_RESOURCE_LIMIT
E_PATH_DIVERGENCE
E_SNAPSHOT_ROLLBACK
E_SNAPSHOT_CORRUPT
E_INDEX_INCONSISTENT
```

## 测试套件

```bash
python3 -m unittest discover -s tests -v
```

结果：

```text
Ran 13 tests
OK
```

测试覆盖样本确定性、最长匹配、同边界裁决、码点偏移、分数重算、同分义项裁决、自适应选择、预算降批、跨路径两类差异、规则定向失效、快照整体替换、回退/损坏检测、SIGKILL 恢复和全部业务错误码。

