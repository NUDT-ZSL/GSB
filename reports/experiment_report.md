# 查询理解与意图解析可复现实验报告

本报告由 `python -m query_understanding run` 确定性生成。所有指标来自本地固定样本，不包含外部网络数据。

## 验收结论

- 总体通过：`true`
- 小规模精确路径意图一致率：`1.0`
- 槽位偏移差异数：`0`
- 最大置信偏差：`0.0`
- 小规模 NDCG@3 / MRR：`0.917021` / `1.0`
- 大规模选中路径：`pruned_approx`，NDCG@3 / MRR：`0.917021` / `1.0`
- 规模取舍：小规模 `exact`，大规模 `pruned_approx`

## 数据与环境

- Python：`3.11.15`
- NumPy：`1.26.4`
- 随机种子：`20261122`
- 小/大规模查询数：`96` / `576`
- 词典条目数：`228`
- 数据清单校验：`true`

## 两路径可比性

- 精确路径与近似路径是否可比：`true`
- 意图一致率：`1.0`
- 槽位值差异数：`0`
- 指标差值（近似 - 精确）：NDCG@3 `0.0`，MRR `0.0`
- 逐查询明细包含 top-k 意图、相关性等级、DCG 与 IDCG，可独立重算。

## 规模取舍与预算

- 小规模观测：{"avg_tokens":3.34375,"dictionary_entry_count":228,"memory_available_mb":432.0,"memory_limit_mb":512.0,"memory_reserve_mb":32.0,"query_count":96,"resident_memory_mb":48.0,"resident_memory_source":"fixed_config_for_reproducibility","total_tokens":321}
- 小规模决策：`exact`，原因：[]
- 大规模观测：{"avg_tokens":3.34375,"dictionary_entry_count":228,"memory_available_mb":432.0,"memory_limit_mb":512.0,"memory_reserve_mb":32.0,"query_count":576,"resident_memory_mb":48.0,"resident_memory_source":"fixed_config_for_reproducibility","total_tokens":1926}
- 大规模决策：`pruned_approx`，原因：["QUERY_COUNT_OVER_EXACT_LIMIT"]
- 预算优先级：`['precision', 'memory', 'progress']`
- 正常准入：`accepted`
- 比较预算降批：`downbatch`，batch_size=`10`
- 禁止降批：`E_MATCH_BUDGET`
- 内存不足：`E_RESOURCE_LIMIT`

## 版本快照

- v1 条目数：`228`，v2 条目数：`227`
- 整体替换而非增量合并：`true`
- 回退尝试：`E_SNAPSHOT_ROLLBACK`
- 旧内容覆盖尝试：`E_SNAPSHOT_ROLLBACK`
- 损坏快照：`E_SNAPSHOT_CORRUPT`

## 异常注入与自检

- `normalize`：code=`E_INJECT_NORMALIZE`，降级=`normalize:raw_text_passthrough`，自检=`True`
- `tokenize`：code=`E_INJECT_TOKENIZE`，降级=`tokenize:char_fallback`，自检=`True`
- `slot`：code=`E_INJECT_SLOT`，降级=`slot:slots_dropped`，自检=`True`
- `intent`：code=`E_INJECT_INTENT`，降级=`intent:intent_default_unknown`，自检=`True`
- 自检不一致：`E_INCONSISTENT` / category=`token_text_mismatch`

## 压力场景

- 剪枝上限压到 1 后：`E_PATH_INCOMPARABLE`，首个不可比查询 `q000001`
- 差异归因计数：{"candidate_pruning_miss":16,"confidence_scale":80,"disambiguation_policy":0,"metric_delta":49,"slot_candidate_or_overlap_policy":34}
- 保持候选不变、关闭近似路径点击混合后：`E_PATH_INCOMPARABLE`，首个不可比查询 `q000002`
- 消歧口径压力归因：{"candidate_pruning_miss":0,"confidence_scale":16,"disambiguation_policy":3,"metric_delta":3,"slot_candidate_or_overlap_policy":0}

## 复现

- 创建环境：`python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt`
- 生成样本：`.venv/bin/python scripts/generate_data.py`
- 运行实验：`.venv/bin/python -m query_understanding run`
- 运行测试：`.venv/bin/python -m unittest discover -s tests -v`
- 小规模确定性结果校验和：`bb6464032a7a15cc486e42daf6e1979482f768420a2e705f4dd8d705e7fd160f`
- 报告核心校验和：`eb256713002e6aa3fb7e29286a5308ad1a8462e7124c302829d356d4ee967327`

完整结构化结果见 `reports/experiment_report.json`。
