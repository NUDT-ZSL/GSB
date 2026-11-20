# 技术说明

## 1. 确定性数据与编码

样本由 `mtstream.sample_data.make_sample_case(seed)` 生成：固定种子从受控源词表选择 14 个源 token，从对齐表生成 11 个目标 step，并刻意保留 3 个未被目标词引用的源位置。所有数据仅来自代码内置词表。

源词嵌入不使用 Python 内置随机哈希：

```text
u_i(token) = 2 * int64(SHA256(seed|source|token|i)[0:8]) / (2^64 - 1) - 1
embedding(token) = normalize([u_0, ..., u_{d-1}])
```

源编码使用位置权重：

```text
w_p = 1 / log2(p + 2)
pooled = sum(w_p * embedding(source[p])) / sum(w_p)
```

逐位置嵌入、位置权重、完整池化向量和完整编码摘要均保存在 `EncodedSource` 中。

## 2. 逐 step 解码

每个目标位置 `t` 按以下口径计算：

1. 以当前目标位置的对齐源位置为中心取上下文窗口 `[c-W, c+W]`。
2. 对候选目标词，仅使用对齐表明确指向的源位置。
3. 注意力 logit：

   ```text
   logit_j = dot(query, source_embedding[j]) / sqrt(d)
             + 0.15 * alignment_weight
   alpha = softmax(logit)
   projection = normalize(sum(alpha_j * source_embedding[j]))
   ```

4. 候选分数：

   ```text
   score(c) = 6.0 * has_current_alignment
            + 1.8 * sum(alignment_weights)
            + 1.1 * dot(target_embedding[c], projection)
            + 0.9 * dot(target_embedding[c], previous_projection)
            + 0.5 * dot(target_embedding[c], query)
   ```

5. 对全部候选做 softmax 得到置信度；最高分胜出，同分按目标词 token 字典序裁决。
6. 新状态继承并累加 `normalizer_sum` 与 `attention_norm_sum`，不因块边界清零。

所有输出浮点按声明精度 `10^-12` 量化；规范化 JSON 使用固定键排序、紧凑分隔符、禁用 NaN。语义哈希不包含任何计时字段。

## 3. 整段与分块统一

统一函数：

```text
decode_step(case, config, encoded, state, ..., block_index)
```

`block_index` 只作为记录元数据。整段路径与分块路径分别控制循环范围，但都调用该函数。分块路径从缓存的前缀状态开始，`state.step` 始终是全局位置。

块提交前的校验顺序：

1. 首块把实际 `decode_range` 循环计时与预算比较。
2. 独立的整段路径执行 `decode_whole`，使用单独 `TimingRecord(stage="whole_decode")`。
3. 将当前块记录与整段对应区间逐项比较：token、置信度、投影、归一化常数、上下文索引、状态哈希、候选分数/概率。
4. 任一不同返回 `E_CHUNK_MISMATCH`，载荷包含 `block_index`、`first_deviation_step`、`deviation_field`、期望值和实际值。

块解码观测计时和整段观测计时不会混用。

## 4. 前缀缓存

逐 step 缓存检查点在 root 和每个目标 step 后生成。缓存键元数据包含：

- `source_encoding_digest`：前缀作用域源编码摘要；
- `align_version` 与 `align_checksum`；
- `block_size` 与 `context_window`；
- `upstream_node_hash`：前一检查点的载荷哈希，root 使用固定 genesis 哈希；
- `boundary_step`；
- 影响计算的配置摘要；
- 词表/对齐表/块大小快照指纹。

前缀源编码摘要只包含已消费的对齐位置。未对齐源词变化不会改变任何前缀摘要，因此全部缓存可复用；修改对齐到目标 step `t` 的源位置时，boundary `<= t` 的检查点仍复用，只需重算 `[t, N)`。

每个条目保存：

```json
{
  "metadata": {"source_encoding_digest": "sha256"},
  "payload": {"state": {}, "record": {}},
  "payload_hash": "sha256(canonical(payload))",
  "key": "sha256(canonical(metadata))",
  "version": 1
}
```

读取时重新计算键、载荷哈希和状态哈希。损坏时返回 `E_CACHE_CORRUPT`，并携带 `entry_key`、`version` 和 `category`。

## 5. 失效传播

`analyze_invalidation(new_case, new_config)` 返回：

- `first_affected_step`；
- `affected_steps` 与 `reused_steps`；
- `affected_cache_entries` 与 `reused_cache_entries`；
- `set_check`：两组不相交且并集覆盖全部目标 step；
- 改动源位置、对齐依赖和复用载荷哈希明细。

策略：

| 改动 | 复用 | 重算 |
|---|---|---|
| 未对齐源词 | 全部检查点 | 无 |
| 对齐到最早目标 `t` 的源词 | boundary `<= t` | step `t..N-1` |
| 对齐表/版本变化 | 无 | 全部 |
| 块大小、上下文窗口、嵌入维度变化 | 无 | 全部 |

`apply_case_change` 保留未失效条目，删除受影响条目，更新快照并重置活动计划和就绪块。保留条目的键与载荷哈希逐字节不变。

输入切换后的实际执行也遵循该边界：完全位于首个受影响 step 之前的块由缓存记录重放，不执行候选打分；跨越边界的块仅从边界后的 step 开始真实解码，边界之前记录只做元数据归一化后拼接。这样报告中的复用集合与实际计算范围一致。

## 6. 快照

`SnapshotStore` 为 `vocabulary`、`alignment_table`、`block_size`、`decode_params`、`source_sentence` 保存：

- 单调递增 `version`；
- 内容校验和；
- 不可变对象及独立 `object_id`。

新版本整体替换旧对象。低版本或同版本不同内容返回 `E_SNAPSHOT_ROLLBACK`。读取时重算校验和；不符返回 `E_CACHE_CORRUPT`，携带 `entry_key=snapshot:<kind>` 和版本。

## 7. 并发与资源

- `StageCoordinator` 使用非阻塞锁保护计划提交。冲突立即返回 `E_STAGE_BUSY`，载荷标记 `waited:false`。
- 并发预览使用线程池，每个块从独立的整段状态流起点执行真实 `decode_range`，不提交块也不持有计划锁。
- 首块预算只统计 chunk 解码循环的 `perf_counter_ns` 实际处理时间；估算记录包含 step 数、候选打分次数和缓存规模。
- `max_source_tokens` 和 `max_cache_entries` 超限返回 `E_RESOURCE_LIMIT`。
- 单块内存估算超限时按明确策略缩小有效块大小，标记 `degraded=true` 和 `degradation_category="block_memory"`；不同块大小仍经过同一 step 函数，输出 token/置信度/投影不变。

## 8. 自检

`run_self_check` 执行：

1. 两次独立整段解码的语义字节一致；
2. 分块拼接与整段逐 step 一致；
3. 缓存键、载荷哈希和状态哈希互相可核对；
4. 每个块仅使用缓存前缀即可重放；
5. 未对齐/已对齐/对齐版本三类失效集合覆盖正确，未对齐修改输出一致；
6. 快照版本和校验和有效。

