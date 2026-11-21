# SlotLine 技术设计

## 1. 数据与确定性边界

会话对象由 `Session`、`Turn`、`Candidate`、`ClarificationRequest` 构成。用户轮携带 `key/value/source_turn/confidence/extractor_version`，助手轮携带槽位澄清请求。`source_turn` 使用稳定帧序号，撤回不会重编号，因此“移除轮次重建”与“原地撤回”能产生相同的来源语义。

所有持久化产物经 `canonical_json_bytes()` 序列化：UTF-8、对象键排序、无多余空格、禁止 NaN。分数由 NumPy 以固定精度计算后转换为 Python `float`。因此确定性由以下输入共同约束：

- 固定会话种子与生成器版本；
- 抽取器版本权重；
- 分数小数位；
- 澄清门限；
- 规范化序列化规则。

## 2. DAG

```text
user turn ──> score:tN ───────────────┐
                                      v
previous merge:slot ───────────> merge:tN:slot ──> pending ──> summary
                                      ^
assistant clarification requests ─────┘
```

节点类型：

- `score:<turn_id>`：解析用户轮候选并打分。
- `merge:<turn_id>:<slot>`：逐槽位累积覆盖历史，保留累计 `covered`。
- `pending`：根据活跃澄清请求、最终合并状态和门限生成待澄清集合。
- `summary`：输出最终状态；仅含获胜候选，不含被覆盖候选。

`merge` 节点只依赖同一槽位的前一个合并节点与当前打分节点。无关槽位在撤回后仍可命中旧快照。

## 3. 打分与裁决

默认权重：

```json
{"v1": 1.0, "v2": 1.05, "v3": 1.12}
```

```text
score = round(confidence * weight[version], 6)
```

展示排序为：

```text
score DESC, source_turn ASC, key ASC, value ASC, version ASC, confidence DESC
```

槽位合并优先级：

```text
source_turn DESC, score DESC, value ASC
```

第三级使用字典序较小值，并记录 `equal_score_value_tiebreak`。同来源轮的多个不同值还会记录 `same_source_candidates`。冲突标记在合并链中累积，最终槽位状态保留 `conflict` 和原因集合。

## 4. 缓存键与条目

缓存键材料：

```json
{
  "schema_version": 1,
  "stage": "merge",
  "node_id": "merge:u3:budget",
  "input_digest": "<sha256>",
  "extractor_versions": {"v1": 1.0, "v2": 1.05, "v3": 1.12},
  "clarification_threshold": 0.65,
  "config_fingerprint": "<sha256>",
  "upstream": [{"node": "score:u3", "key": "<sha256>"}]
}
```

条目保存：

- 节点输入摘要；
- 抽取器版本、门限、配置指纹；
- 上游节点哈希；
- `payload_hash`；
- `entry_checksum = sha256(canonical(entry without entry_checksum))`。

读取按键定位文件，并同时核对键、元数据、载荷哈希和整体校验和。缓存目录结构：

```text
.runtime/
  cache/
    <key>.json
    .locks/<key>.lock
  snapshots/
    snapshot-00000001.json
    current.json
```

写入流程先以 `O_CREAT|O_EXCL` 获取非阻塞锁，再写临时文件、`fsync` 并 `os.replace`。锁已被持有时立即抛出 `E_STAGE_BUSY`，不进入等待队列。

## 5. 撤回失效传播

撤回轮次 `T` 时：

1. 受影响槽位为 `T` 的候选键集合；若撤回助手轮，则为请求槽位集合。
2. 从 `T` 起删除其节点，并对这些槽位重放后续合并链。
3. 若某槽位后续合并节点的前驱发生变化，则重算该节点；其他槽位节点继续复用缓存。
4. 待澄清节点在请求槽位上游变化时重算；摘要节点总是重算。
5. 输出 `affected_slots`、`recomputed_nodes`、`reused_nodes`、`removed_nodes` 与 `affected_graph_nodes`。

测试将撤回后的 `state_bytes` 与不含该轮的会话在新运行时全量构建的字节比较，确保完全一致。

## 6. 追加与批处理顺序

追加只允许尾部轮次，索引必须为 `max(index)+1`。已有轮次的打分输入不变，因此其 `score` 节点全部命中；新轮产生新打分节点，其槽位产生新合并节点。

同时撤回与追加时，唯一合法顺序为：

```text
retract → append
```

`order_log` 记录实际执行序列。任何 `append` 出现在 `retract` 之前都返回 `E_OPERATION_ORDER`，且验证发生在状态变更前。

## 7. 追溯重放

最终槽位记录：

```json
{
  "merge_node": "merge:u4:budget",
  "merge_cache_key": "<sha256>",
  "merge_chain": [{"node": "...", "key": "..."}],
  "score_node": "score:u4",
  "score_cache_key": "<sha256>",
  "source_turn": 7,
  "extractor_version": "v2",
  "config_fingerprint": "<sha256>"
}
```

重放依次验证合并链前后键连续、所有条目校验和、最终 winner、累计 covered 集合、候选来源打分条目及重新计算的分数。若缓存缺失或内容不能复现，返回 `E_LINEAGE_BROKEN`；若当前配置指纹、版本集合或门限与记录不一致，返回 `E_LINEAGE_STALE`。

## 8. 快照整体替换

快照对象包含：

- `version`：严格单调递增；
- `config` 与 `config_fingerprint`；
- `cache_manifest`：当前所有活跃缓存键及其条目/载荷哈希；
- `cache_entries`：完整的活跃缓存条目副本，随快照整体替换；
- `checksum`：快照正文内容哈希。

提交时生成完整新文件并原子替换 `current.json`。不修改旧快照或增量补丁。读取时同时核对指针版本、快照校验和、配置指纹与清单内所有缓存条目。版本回退返回 `E_SNAPSHOT_ROLLBACK`；任何损坏返回 `E_CACHE_CORRUPT`。

## 9. 本地诊断应用

`slotline.server` 使用标准库 `ThreadingHTTPServer`，只绑定 `127.0.0.1`。内嵌 HTML/CSS/JavaScript 不访问外部资源，展示：

- 四阶段 DAG 拓扑及节点本次“重算/复用”状态；
- 撤回/追加的受影响槽位、重算节点、复用与移除节点；
- 最终槽位获胜候选、冲突状态、缓存键与追溯路径；
- 当前快照版本、校验和、缓存规模；
- 最近业务错误码；
- 撤回、追加、重置和追溯重放交互。
