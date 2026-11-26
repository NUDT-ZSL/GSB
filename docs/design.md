# 设计说明

## 1. 目标与边界

组件面向本地确定性诊断，不实现 GPU kernel，也不试图替代生产 FlashAttention。它用 NumPy 在 float64 中复现分块 online-softmax 的关键状态：

- 输入校验与 Q/KV 分块计划；
- 每个 score tile 的局部最大值、指数和、输出分子；
- 跨 KV block 的在线归一化统计；
- Q block 输出写回和整段拼接。

组件重点验证增量修订的失效边界、缓存复用、账本原子性和重放一致性。

## 2. 任务与计划

`seq_len`、`head_dim`、`num_heads` 对应 Q/K/V 三维数组 `(heads, seq_len, head_dim)`。Q block 和 KV block 都从序列起点按固定长度切分，最后一块可以短于配置值。

causal 计划只保留 `kv_start <= q_end` 的 `(Q block, KV block)` 对。对每个可见对生成 score 节点。非 causal 计划保留所有 `(Q block, KV block)` 对。

每个 head/Q block 的 plan 记录写入 SQLite `block_plans`，带快照版本、Q 范围、KV 范围和计划校验和。JSON 形式的全局结构计划写入 `plan` artifact。

## 3. 内容寻址与哈希域

artifact 存储在：

```text
<store>/<node_type>/<key[:2]>/<key>/manifest.json
<store>/<node_type>/<key[:2]>/<key>/data/<array>.npy
```

`manifest.json` 保存节点类型、元数据、数组描述和数组哈希。`content_sha256` 是以下内容的规范 JSON 哈希：

```text
{schema_version, node_type, meta, arrays}
```

它有意不包含 artifact 自己的 `cache_key`。这使“内容相同但缓存路径不同”的上游节点能产生稳定的语义内容哈希，例如内容未变的 plan 在 validation 键变化后仍可作为 score 的稳定上游。

缓存键组成：

| 节点 | 键 |
| --- | --- |
| validation | `case_id, q_block_len, kv_block_len, dtype, causal, seed, task, QKV 哈希, shape` |
| plan | 上述 scope、validation 缓存键、结构计划哈希 |
| score | scope、plan 内容哈希、head、Q/KV block 索引和范围、相关 Q/K/V block 内容哈希 |
| norm | scope、head/Q block、按 KV 顺序排列的 score 缓存键和内容哈希 |
| writeback | scope、head/Q block、norm 缓存键和内容哈希 |

score 键只使用相关 block 的内容哈希，而不是整个 Q/K/V 数组哈希，这是实现“只失效依赖该 K/V 块的 tile”的关键。

## 4. 增量失效

旧 validation artifact 保存每个 head 的 Q/K/V 哈希以及每个 Q/KV block 的内容哈希。

变化检测：

```text
changed_heads = heads where old_qkv_head_hash != new_qkv_head_hash
changed_kv_blocks[head] = blocks where old_k_or_v_block_hash differs
affected_q_blocks[head] = planned Q blocks that use any changed KV block,
                          plus Q blocks whose own Q content changed
```

causal 情况下，位于某个 Q block 结尾之后的 K/V 修订不会影响该 Q block；非 causal 情况下，变化 KV block 会被所有 Q block 使用。

节点失效自然沿键链传播：

```text
score tile 变化
  -> 包含该 score 的 norm 键变化
  -> 包含该 norm 的 writeback 键变化
```

因此不需要根据业务语义手工删除节点。validation/plan 可以重算，但未变的 score/norm/writeback 仍按相同内容键命中。

## 5. 数值与次正规数保护

score tile 的原始 score 和中间量全部 float64。对每个 Q row：

```text
m = max_j S[i, j]
e = exp(max(S[i, j] - m, log(float64_tiny)))
l = sum_j e
p = e @ V_block
```

causal mask 位置是 `-inf`，指数结果显式置零，不能与受保护的小正数混淆。`l` 也会被限制到至少 `float64_tiny`。

对于多个 KV block：

```text
new_m = max(old_m, block_m)
old_scale = exp(old_m - new_m)
block_scale = exp(block_m - new_m)
new_l = old_l * old_scale + block_l * block_scale
new_p = old_p * old_scale + block_p * block_scale
```

最终 `output = p / max(l, float64_tiny)`。该递推与整段 softmax 数学等价；直接整段参考使用同一指数保护策略，仅矩阵乘法累加顺序可能不同，所以整段对照使用容差。

增量输出对全量分块重算执行零容差比较。全量分块重算使用相同的逐 Q block 计算路径，不读取缓存，也不修改缓存。

## 6. 账本事务

`snapshots` 的每个 case 最多有一个 `is_active=1` 行，由部分唯一索引保证。每次提交：

1. `BEGIN IMMEDIATE` 获取 SQLite 写锁；失败立即映射为 `E_STAGE_BUSY`。
2. 重读活动快照，校验调用方构建时看到的 `expected_active_snapshot_id`。
3. 校验版本不下降、weight revision 不下降。
4. 写入完整的新 snapshot 行、全部节点行、全部 block plan 行。
5. 停用旧活动快照，激活新快照。
6. 提交事务。

旧快照节点不被更新或删除。新提交是完整快照，不是旧节点的增量修补。相同版本和相同快照校验和可以幂等返回；相同版本不同校验和返回 `E_INPUT_INVALID`。

快照校验和包含：

- case、版本、weight revision、输入/输出哈希、前驱快照；
- 每个 plan 的 head/Q block/范围/KV 范围/计划哈希；
- 每个节点的节点 ID、类型、版本、缓存键、内容哈希和依赖哈希。

时间戳不参与内容校验和，因此逻辑内容相同不因时间不同而改变校验和。

## 7. 自检与重放

`verify_case` 从账本执行：

1. 重算快照校验和并检查节点依赖哈希。
2. 从 validation artifact 读取已固化的 Q/K/V，确认输入哈希等于提交时输入，不依赖可能被覆盖的源文件。
3. 重新推导 validation、plan、每个 score、norm、writeback 的缓存键。
4. 校验每个 artifact 语义内容哈希和数组文件哈希。
5. 由 score 产物重新执行 norm merge，与提交 norm 比较。
6. 由 norm 产物重新归一化，与提交 writeback 比较。
7. 拼接 Q block 输出，校验输出哈希和导出文件。
8. 与全量分块重算零容差比较，并与整段参考在容差内比较。

任何内容或账本不一致都会在对应步骤返回 `E_LEDGER_INTEGRITY` 或 `E_CACHE_CORRUPT`；数值偏差返回 `E_TOLERANCE_EXCEEDED` 和首个差异位置。

## 8. 资源控制

每个 Q block 工作集的保守估计包含：

- Q block、K block、V block；
- score、指数权重；
- 局部输出分子；
- 合并结果、mask 和统计量。

先做缓存查找，只对实际 miss 的 Q block 分组执行重算。工作线程数受 `max_parallel_q_blocks` 和 `max_resident_bytes` 共同约束。超限时默认串行降级并记录原因，否则返回 `E_RESOURCE_LIMIT`。

## 9. 并发与原子性边界

artifact 写入采用临时目录加原子 rename，manifest 最后发布。相同缓存键的并发 artifact 写入只会留下一个内容一致的目录。

SQLite 写锁只覆盖快照激活事务。昂贵的注意力计算在事务外执行，避免长事务阻塞读取。两个基于同一旧活动快照构建的提交中，只有第一个能通过 `expected_active_snapshot_id` 校验；后到者返回 `E_STAGE_BUSY`。
