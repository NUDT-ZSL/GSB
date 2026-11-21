# 技术设计说明

## 1. 处理链

```text
query_text
  -> normalize(mapping)
  -> tokenize(dictionary trie)
  -> evidence(exact | pruned approximate)
  -> intent stage 1 lexical ranking
  -> intent stage 2 click PRF rerank (ambiguous only)
  -> slot candidates + regex + deterministic overlap resolution
  -> cross-stage self-check
  -> offline evaluation or cross-path adjudication
```

归一化后的字符串是后续所有 `start/end` 的唯一坐标空间。Python `str` 索引即 Unicode 码点索引，因此 token 与槽位偏移不需要 UTF-8/UTF-16 转换。token 覆盖规范串中全部非空白码点，既便于自检，也避免词典外的 CJK、ASCII 和标点被静默丢弃。

## 2. 归一化与分词

全半角映射先于繁简映射执行。全角字符若缺失映射直接返回 `E_MAP_MISSING`；繁体声明集合 `traditional_sources` 中任一字符缺失映射也返回 `E_MAP_MISSING`。ASCII 在映射后统一小写，连续空白折叠为一个空格。

词典加载时校验版本、唯一 `entry_id`、字段类型和 payload 校验和。`max_entries` 小于真实条目数时默认返回 `E_DICT_TRUNCATED`，不会静默截断。词典 trie 按最长 surface 匹配；同一 surface 可挂多个 `entry_id`，从而表达品牌、类目、城市等跨意图歧义。

## 3. 槽位

槽位候选来源：

1. 当前选中意图 schema 内的词典 `slot_value` 证据；
2. schema 自带正则，例如日期、预算、订单号和集数。

候选按以下顺序确定：

1. 置信度降序；
2. 同置信度槽位名升序；
3. 同槽位名时 span 更长者优先；
4. 再按起点、终点与正规化值升序。

选择时贪婪拒绝重叠 span，且一个槽位名只保留一个值。方向性 schema 额外按 `到/至` 标记把城市映射到 `city_from/city_to` 或 `origin/destination`。偏移始终是规范串码点区间。

## 4. 意图与点击消歧

词典证据分两类：

- `intent_cue`：意图提示词，贡献 `0.60 * weight * similarity`；
- `slot_value`：槽位值，贡献 `0.45 * weight * similarity`。

每个意图还有 `0.20 * matched_schema_slots / schema_slots` 的覆盖奖励。stage 1 按词法分数排序；top-2 margin `<= ambiguity_margin` 视为歧义。

反馈点击只取 rank `<=2` 且 dwell `>=300ms` 的记录。点击权重为 `min(1,dwell/2000)/rank`，按意图聚合后归一化。歧义查询的最终分数：

```text
0.45 * normalized_lexical + 0.55 * normalized_click
```

最终置信为固定温度 `0.18` 的 softmax。无词法证据时回退到 `general/unknown_query`，置信为 `0`，不会因点击标签凭空猜测意图。

层级输出中 domain 置信取该 domain 内最高意图概率，因此 top-1 domain 始终与 top-1 intent 的 domain 对齐；`domain_ranking` 同步输出完整 domain 排序。

## 5. 离线指标

评估点击取 rank `3..5`，与反馈窗口隔离。每个意图的点击次数截断为 3：

```text
rel(intent) = min(3, eval_click_count)
DCG@k      = sum((2^rel - 1) / log2(rank + 1))
NDCG@k     = DCG@k / IDCG@k
MRR        = 1 / first_relevant_rank
```

聚合方式为“有评估点击查询”的宏平均。每个查询明细包含 top-k、相关性映射、DCG、IDCG 和首个相关位置，因此 `recompute_metrics` 不读取任何中间模型状态即可复核全部指标。

## 6. 规模取舍

观测字段：

```text
query_count, dictionary_entry_count, avg_tokens,
memory_limit_mb, resident_memory_mb, memory_reserve_mb,
memory_available_mb
```

默认精确路径阈值：查询数 `<=128`、词典 `<=256`、平均 token `<=12`、可用内存 `>=64MB`。任何一项不满足就切换到 `pruned_approx`。代价估计：

```text
candidate_count = dictionary_entries                  (exact)
                = min(dictionary_entries, approx_cap) (pruned_approx)
comparisons     = query_count * candidate_count * ceil(avg_tokens)
memory          = 4MB + dictionary_entries*0.0008MB
                  + query_count*(0.004MB + avg_tokens*0.00025MB)
```

决策记录保留规模观测、两条路径的代价、阈值失败原因、准入记录和 `decision_checksum`。相同观测和配置产生逐字节一致的决策 JSON。

## 7. 预算裁决

默认优先级为 `precision > memory > progress`：

1. **precision**：不为迎合预算缩小候选集合，只缩小 batch。
2. **memory**：优先在内存上限内降批，绝不超过驻留内存余量。
3. **progress**：至少处理 `minimum_batch_size` 条；无法做到才拒绝。

可降批时返回 `status=downbatch` 与稳定 `batch_size`；不可推进时：

- 比较容量不足返回 `E_MATCH_BUDGET`；
- 内存容量不足返回 `E_RESOURCE_LIMIT`。

## 8. 跨路径可比性

同一查询集分别跑精确和近似路径，比较：

- top-1 domain/intent；
- 槽位集合、偏移、正规化值；
- 每条查询置信偏差及分布；
- NDCG/MRR 差值。

报告给出 `candidate_pruning_miss`、`disambiguation_policy`、`slot_candidate_or_overlap_policy`、`confidence_scale` 和 `metric_delta` 的归因计数。默认策略要求意图完全一致、槽位完全一致、最大置信偏差 `<=0.10`、指标差 `<=0.05`。不满足时返回 `E_PATH_INCOMPARABLE`，并指出按输入顺序的首个不可比查询。

报告的 `stress_incomparable` 把受限候选压到 1，用于验证 `candidate_pruning_miss`；`stress_disambiguation_policy` 保持候选不变但仅关闭近似路径的点击混合，用于验证 `disambiguation_policy`。这样两类不可比来源可以分别复现，而不是只给一个笼统错误。

## 9. 快照与异常

快照文件结构：

```json
{
  "version": 1,
  "content_checksum": "sha256(payload)",
  "payload": {
    "taxonomy": {"version": 1, "checksum": "...", "...": "..."},
    "dictionary": {"version": 1, "checksum": "...", "...": "..."},
    "config": {"version": 1, "checksum": "...", "...": "..."},
    "mappings": {"version": 1, "checksum": "...", "...": "..."}
  }
}
```

激活高版本时整个 payload 替换，不做旧条目合并；低于当前版本直接拒绝。快照 wrapper 或任一组件校验和不匹配都返回 `E_SNAPSHOT_CORRUPT`。

四阶段注入的降级策略：

| stage | 默认降级类别 | 行为 |
| --- | --- | --- |
| normalize | `raw_text_passthrough` | 原串继续分词，保留可核对偏移 |
| tokenize | `char_fallback` | 每码点一个 token |
| slot | `slots_dropped` | 清空槽位但保留意图 |
| intent | `intent_default_unknown` | `general/unknown_query`，清空槽位，置信 0 |

自检核对 token 文本与偏移、非空白覆盖、槽位边界/重叠/schema、top-k 与 domain/intent 层级。首个问题作为 `E_INCONSISTENT.category` 返回。

