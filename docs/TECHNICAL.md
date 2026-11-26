# 技术说明

## 1. 确定性边界

系统内所有稳定 ID、哈希、排序和分数均不使用墙钟时间、进程 ID、字典遍历顺序或网络状态。

- JSON 规范字节：`sort_keys=True`、分隔符 `(",", ":")`、`ensure_ascii=False`、拒绝 NaN。
- 分数统一量化为基点整数，贡献项使用 `floor(weight_bps * raw_bps / 10000)`。
- NumPy 仅用于确定性的指数衰减和文档 mention 计数统计；没有随机数进入推理阶段。
- 固定种子 `20261127` 只负责生成样本，生成后的数据和规则文件本身是稳定输入。

## 2. 候选与双路径

候选 pair key 为有序的 `<mention_a>|<mention_b>`，mention 按 `(doc_id, sent_id, start, end, order_index)` 排序。

精确路径在 mention 边界增量枚举同文档、同类型且句距不超过规则窗口的 pair，并比较：

- 共享实体类型和类型签名；
- 名称 token Jaccard、字符 bigram Jaccard/覆盖系数；
- 外围上下文 token Jaccard；
- 句距、码点距离和双重衰减；
- 文档规模；
- 规则 ID、generation、algebra generation、墓碑和代数偏置；
- 零权重低区分度辅助特征。

近似路径先按 `(doc_id, type:<ENTITY_TYPE>)` 分桶，再应用相同句窗。它保留聚合同分度，但跳过 `mention_order_parity`、`sentence_id_parity`、`start_offset_mod_8`、`end_offset_mod_16` 以及 bigram/context 交集明细。剪枝项均为零评分权重，因此两条路径共用同一分数、阈值、仲裁和平局口径。

## 3. 冲突仲裁和链

候选按以下顺序排序：

1. `score.total_bps` 降序；
2. `rule_generation` 升序；
3. 首个 mention 的句子内 `start` 升序；
4. `shared_entity_type` 字典序；
5. `pair_key` 升序。

对每个后续 mention `b`，取其所有合格候选中的第一名作为唯一主父边。其他高分候选保留在 `rejected_conflict_candidates`，并记录总分差和代数差。所有主父边进入并查集；多成员连通分量成为链接链，单例 mention 单独列出。链置信度定义为最弱主边分数 `min(edge.total_bps)`。

## 4. 缓存与增量

证据缓存键格式：

```text
ckv3:<rule-and-algorithm-hash-prefix>:<evidence-hash-prefix>
```

证据哈希只覆盖核心证据与分数，不包含文件路径、运行时间或缓存命中状态。链闭包键包含缓存键版本、规则内部哈希和确定性 chain ID。

Delta 支持：

- `add_sentences`：加入新的 `(doc_id, sent_id)` 句子；
- `revise_sentences`：整体替换同一句子行及其 mention。

新增句子会改变同文档规模因子，修订句长度可能改变后续码点全局偏移，因此失效域是变更文档内的全部 mention 和候选 pair；若规则代数或规则内容变化，使用该规则的所有 pair 也进入重算集合。其他文档的证据和链闭包直接从缓存读取，并比较 chain checksum/结构是否保持不变。

`rule_generation_jump` 记录前后规则哈希、最大 generation、变化规则 ID、时间线和墓碑。测试还覆盖把 `ctx-org` 从 generation 2 提升到 3 后，仅 ORG 规则依赖 pair 被定向失效。

## 5. v1/v2 规则迁移

内部规范结构包含 `snapshot_id`、`origin_effective_at_ms`、排序后的规则列表和单调时间线。

### v1 -> v2

- `relative_effective_seconds` 经固定 `V1_UPGRADE_ORIGIN_MS` 转换为绝对毫秒；
- 缺失的 `generation` 和 `algebra_generation` 补为 1；
- 缺失的 `tombstone` 补为 `false`；
- 写入分片 JSON、manifest 和 SQLite `snapshot`/`rule_meta` 表；
- 重读 v2 后与 v1 内部规范字节比较。

### v2 -> v1

- generation 归一为 1，绝对毫秒转回相对秒；
- `mode=fold` 保留代数偏置到 v1 可读取的扁平字段，迁移记录写明 fold 目标；
- `mode=discard` 将代数偏置置零，并逐项记录丢弃值；
- 墓碑以扁平 `tombstone` 字段保留；
- 按所选策略构造期望内部表示，再与 v1 重读结果逐字节比较。

未知 `schema_version` 返回 `E_VERSION_UNSUPPORTED`；缺字段、权重总和错误、SQLite 哈希不符或时间戳倒退返回 `E_FORMAT_CORRUPT`；迁移策略后期望/实际规范字节不一致返回 `E_SEMANTIC_DRIFT`。

## 6. 重放和陈旧链接

每条 trace 记录：

- `chain_id`、`pair_key`；
- `rule_id`、`rule_generation`、`algebra_generation`；
- `evidence_hash`、`exact_evidence_hash`、`approx_evidence_hash`；
- 每个评分组件的 `component_hashes`；
- `cache_key` 和 `cache_key_version`；
- 近似路径的完整剪枝项和名称列表。

重放器不信任报告中的总分，而是用 mention 和规则快照重新计算证据、组件哈希、缓存键、链集合和最弱边置信度。若 trace 明确表示仍由旧缓存键提供结果，且缓存键版本或依赖哈希已变化，立即返回 `E_STALE_LINK`。

## 7. 检查点状态机

四个阶段严格按顺序推进：

1. `candidate_generation`
2. `path_selection`
3. `evidence_aggregation`
4. `invalidation_propagation`

每个 mention 处理完后提交：

- 全局单调 `version`；
- 当前 `stage` 和 `stage_index`；
- 已处理 mention ID 集合；
- 当前阶段已完成 `doc_id` 集合；
- 累积状态；
- 规范状态的 `content_checksum`。

提交时先写不可变 `checkpoint-NNNNNN.json` 临时文件并原子替换，再原子替换 `latest.json`。`fcntl.flock(LOCK_EX | LOCK_NB)` 保证同一时刻只有一个提交进入临界区；锁被占用时后来者立即得到 `E_STAGE_BUSY`。

恢复分类：

- `handleable_signal`：检查点或 `run_status.json` 含 `signal:SIGTERM`/`signal_stopped`，在 mention 边界优雅停止；
- `forced_termination`：状态仍为 `running` 或残留临时文件，但最近检查点有效；丢弃临时写入并从最近版本继续；
- `dirty_checkpoint`：`latest.json`/检查点 JSON 损坏、校验和不符或文件名版本不一致；返回 `E_CHECKPOINT_DIRTY`，可显式归档脏文件后回退到上一有效检查点。

恢复时已在当前阶段检查点中的 mention ID 会直接跳过，不会重复生成证据或重复计数。

## 8. 自检

`run_self_checks` 校验：

- 精确/近似候选键集合、分数、链成员、边和置信度完全一致；
- 每条链的每条边都能找到证据和 trace；
- 增量受影响 mention 全部有传播覆盖；
- 会话注册表中的 chain ID 唯一。

任一偏差抛出 `E_INCONSISTENT`，详情包含首个可定位的 pair、chain 或 mention ID。
