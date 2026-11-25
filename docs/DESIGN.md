# 设计与集成说明

## 1. 输入与确定性生成

JSONL 每行是一个 case，字段固定为：

`case_id, token_len, hidden_dim, num_experts, top_k, capacity_factor, router_revision, block_size, dtype, seed`

`moe_router.weights` 使用 NumPy `PCG64` 和 `SeedSequence`，以 case seed 加固定 tag 分离随机流：

- token 流：`[seed, TOKEN_TAG]`
- 门控权重流：`[seed, GATE_TAG]`
- 专家权重流：`[seed, EXPERT_TAG, expert_id, revision]`

因此同一 case、seed 和专家修订号会生成完全相同的数组。token 内容、门控权重和整个专家库都有独立 SHA-256 摘要。输入 `dtype` 目前作为输入契约校验和元数据保留，实际审计算术始终使用 float64。

## 2. 门控与容量

对每个 token：

1. 用确定性点积计算 `hidden_dim` 维 token 对全部专家的原始分数。
2. 对全部专家执行数值稳定 softmax，得到完整门控分布。
3. 按 `(-score, expert_id)` 排序，稳定选择 top-k；rank 越大，score 越低。
4. 对选中 top-k 的概率重新归一化，作为专家加权系数。

每个块单独分配容量：

```text
capacity_per_expert = max(1, ceil(capacity_factor * block_token_len / num_experts))
```

候选分配按 `(-gate_score, token_in_block, top_k_rank)` 排序。超出容量的记录会进入 `overflow`，包含：

```json
{
  "block_index": 0,
  "token_index": 5,
  "token_in_block": 5,
  "expert_id": 2,
  "top_k_rank": 1,
  "gate_score": 0.413
}
```

块间不共享容量余量。最后一个块使用实际块长度。

## 3. 分块与全量一致性

分块路径在 `RoutingSession` 中逐块计算并提交。块输出、门控、分配和块元数据分别写入缓存；调用方可在只完成第 0 块时查询第 0 块。

全量参考路径 `compute_full_reference`：

- 一次性计算全序列门控矩阵。
- 按照相同块边界和相同块内容量规则生成分配。
- 在一次连续的全序列 pass 中调用同一个专家算术内核。

共享内核保证边界不会改变专家顺序或浮点累加顺序：

- 每个 token 的专家按 `expert_id` 升序执行。
- 每个专家输出维度用 `math.fsum` 计算点积。
- 最终输出坐标按同一专家顺序，用 `math.fsum` 累加门控加权贡献。

一致性裁决 `compare_routes` 逐 token 比较：

- 完整门控分布；
- 每个 top-k rank 的专家加权贡献；
- 最终路由输出；
- 选中/接受分配集合。

返回最大绝对偏差、最大相对偏差、首个差异 token。超差时抛出 `E_BLOCK_MISMATCH`，`details.first_diff_details` 包含该 token 的完整门控分布、专家 ID、接受状态、门控系数、贡献向量和输出向量。

## 4. 内容寻址缓存布局

每个 case 的路由产生四类独立产物：

- `gate_distribution`
- `expert_assignment`
- `block_output`
- `block_metadata`

磁盘布局：

```text
cache/
  objects/aa/bb/<sha256>.json       # 规范化产物内容，路径名即内容哈希
  entries/cc/dd/<cache_key>.json    # 缓存键、依赖、内容哈希、复用来源
  index/<case_hash>/block-00000000/<kind>.json
```

缓存键是规范化 JSON 的 SHA-256，包含：

```text
token_content_hash, gate_weights_digest, expert_revisions,
expert_bank_digest, router_revision, top_k, capacity_factor,
block_size, block_index, token_start, token_end, hidden_dim,
num_experts, dtype, format_version
```

`load_checked_artifact` 会重新计算当前依赖键。如果最新索引仍指向旧键，抛出 `E_STALE_RESULT`，并携带 `case_id`、`block_index`、`kind`、实际键和期望键。写入对象时使用临时文件、`fsync` 和原子 `replace`。

## 5. 定向失效

`InvalidationEngine.apply` 比较旧/新 bundle：

- 专家库变化：受影响专家是修订号或权重摘要变化的专家。
- 门控权重、token、`top_k`、`capacity_factor`、`block_size`、专家数量变化：全局失效。
- 旧块中含受修订专家被选中时，该块受影响。

支持两种重算策略：

- `suffix`（默认）：复用首个受影响块之前的已提交前缀，重算受影响块到序列末尾。
- `targeted`：只重算实际依赖变化的块，复用其余未受影响块。

复用块不会盲目信任旧索引：先根据旧块的选择集合证明它没有使用变化专家，再将相同内容对象重新寻址到新依赖键下，并写入 `reused_from` 元数据。报告包含：

```text
changed_experts, affected_experts,
exact_affected_token_blocks, recompute_blocks,
reused_blocks, recompute_suffix_from
```

重算块较多时，`parallel_blocks` 控制工作线程。若请求值超过 `max_parallel_recompute_blocks`，按块索引声明顺序串行重算，并在报告中标记 `serialized_due_to_limit=true`。

## 6. 版本化快照

`SnapshotStore` 管理一个当前快照和历史归档。每个快照整体包含：

- 单调递增版本号；
- 路由策略；
- 专家参数修订和权重摘要；
- 调度/资源限制；
- 对前三部分规范化内容计算的 SHA-256 校验和。

提交流程：

1. 非阻塞获取进程内线程锁和 Linux `flock` 文件锁。
2. 再次读取当前快照。
3. 拒绝旧版本覆盖、过期 `expected_version`、同版本不同内容。
4. 写入完整历史快照，再原子替换 `current.json`。

并发提交争抢锁时立即返回 `E_STAGE_BUSY`。同一版本同一校验和是幂等提交；同一版本不同校验和也返回 `E_STAGE_BUSY`。

## 7. 业务错误码

| 错误码 | 场景 | 关键字段 |
| --- | --- | --- |
| `E_INVALID_INPUT` | 字段类型/范围错误 | 对应字段 |
| `E_DUPLICATE_CASE_ID` | JSONL 重复 case | `case_id` |
| `E_TOKEN_DIMENSION_MISMATCH` | token 张量维度不符 | `token_len` |
| `E_GATE_DIMENSION_MISMATCH` | 门控权重维度不符 | `gate_weights` |
| `E_EXPERT_COUNT_MISMATCH` | 专家权重数量不符 | `num_experts` |
| `E_EXPERT_DIMENSION_MISMATCH` | 单个专家矩阵维度不符 | `expert_weights[i]` |
| `E_INVALID_BLOCK_SIZE` | `block_size <= 0` | `block_size` |
| `E_REVISION_REGRESSION` | `router_revision` 回退 | `router_revision` |
| `E_BLOCK_NOT_READY` | 查询尚未计算的块 | `block_index` |
| `E_BLOCK_MISMATCH` | 分块与全量超差 | `block_index` |
| `E_STALE_RESULT` | 依赖变化但索引仍指向旧键 | `case_id`, `block_index` |
| `E_STAGE_BUSY` | 快照并发提交/版本冲突 | `version` |
| `E_SNAPSHOT_ROLLBACK` | 旧快照覆盖新快照 | `version` |
| `E_SNAPSHOT_CORRUPT` | 快照校验和或 JSON 损坏 | `version` |
| `E_RESOURCE_LIMIT` | 驻留内存超限 | `max_case_resident_bytes` |
| `E_CACHE_CORRUPT` | CAS 对象或键内容损坏 | `content_hash` |

## 8. 资源与实现边界

- `max_case_resident_bytes` 统计单 case 已驻留的块输出、门控、分配和审计贡献估算；预测超限时在计算前返回 `E_RESOURCE_LIMIT`。
- `max_parallel_recompute_blocks` 控制失效重算的并行 worker 上限。
- 当前实现为了逐字节确定性使用 Python/NumPy 的显式标量累加，不以 GPU 或大规模 BLAS 性能为目标。替换专家内核时必须保持同一排序和累加契约，否则一致性审计会暴露差异。
- `suffix` 策略按需求保留“受影响后缀”语义；需要最大化复用时显式使用 `targeted`。

