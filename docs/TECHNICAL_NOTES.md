# 技术说明

## 1. 统一执行模型

每个 job 被规范化为：

```text
job_id, op_kind, matrix_shapes{A,B,C}, dtype, layout, seed,
block_size, alpha, beta, revision, tolerance
```

GEMM 使用二维 `A[M,K] @ B[K,N]`，BMM 使用三维 `A[B,M,K] @ B[B,K,N]`。内部统一增加 GEMM 的批次维 `1`，因此核心输出均为 `[B,M,N]`，落盘 GEMM 结果时压缩首维。

支持的存储 dtype 为 `float16`、`float32`、`float64`；所有乘加中间量立即转为 `float64`。`row_major`、`col_major` 以及 `row/col/C/F` 别名会规范化。

固定样本使用：

```python
np.random.SeedSequence([seed, content_seed, 0x57424D42])
```

为 A/B/C 分别派生独立 RNG。默认 `content_seed=0`，因此仅提升 revision 不会改变矩阵；需要演示“revision 提升且矩阵内容变化”时显式提升 `content_seed`。

## 2. 分块乘加与数值保护

显式循环顺序为：

```text
batch -> m_block -> n_block -> k_block -> k_inner
```

每个输出块按递增 `k` 执行 Neumaier 补偿求和：

```text
term = (A_ik * B_kj) * alpha
updated = sum + term
correction += selection_between((sum-updated)+term, (term-updated)+sum)
sum = updated
result = (sum + correction) + subnormal_guard
```

绝对值小于 `np.finfo(float64).tiny` 的项进入按元素维护的 `math.fsum` 次正规保护通道，并在结果中记录 `subnormal_terms_protected`。最小次正规数 `5e-324` 的专项用例可无损保留。

参考实现为先将输入转 float64，再执行 `alpha * np.matmul(A64, B64) + beta * C64`。默认容差为 `rtol=1e-8, atol=1e-10`。越限时抛出 `E_TOLERANCE_EXCEEDED`，`details.first_deviation.index` 使用 `[batch,m,n]` 全图位置。

## 3. 内容寻址缓存

三级缓存节点：

1. `validation_plan`
2. `block_accumulate`
3. `aggregate_validate`

键公式：

```text
key = sha256(canonical_json({cache_schema, node, parts}))
```

键因子包括：

- A/B/C 数组的 shape、dtype、原始字节 SHA-256；
- `op_kind`、`matrix_shapes`、`dtype`、`layout`；
- 请求与实际生效的 `block_size`；
- `alpha`、`beta`、容差；
- 上游制品哈希：计划哈希或累加结果哈希。

NumPy 完整输出保存在 `cache/blobs/<digest>.npy`，JSON 节点载荷只保存摘要、块清单、偏差谱和 blob 指针。`wb query-cache --job-id ...` 与 `/api/cache?job_id=...` 返回完整 `key_composition`。

## 4. 增量失效传播

提交新批次时先与 `committed-state.json` 合并，再比较旧节点键：

| 变更 | validation_plan | block_accumulate | aggregate_validate |
| --- | --- | --- | --- |
| 新 job | 重算 | 重算 | 重算 |
| 矩阵内容/op/形状/dtype/layout/block_size | 重算 | 重算 | 重算 |
| alpha/beta/tolerance | 复用 | 重算 | 重算 |

`incremental-report.json` 输出：

- `recomputed_nodes`
- `reused_nodes`
- `affected_jobs`
- `recomputed_jobs`
- `fully_reused_jobs`
- `coverage.intersection_empty`
- `coverage.union_covers_all_graph_nodes`

节点全集为当前所有 job × 三个缓存节点。自检会再次验证重算/复用集合不相交且并集等于该全集。

演示中 `bmm_alpha` 与 `gemm_beta` 的计划节点命中，其余受影响后缀重算。`results/*.json` 使用排序键、固定分隔符和 LF 换行，不含墙钟时间或绝对路径，因此增量与从零全量运行逐字节一致。

## 5. 双版本账本

### v1

路径：`ledgers/v1/ledger-v1.json`。

每行扁平记录仅含：

```json
{"relative_commit_seconds": 0, "job_id": "...", "output_digest": "..."}
```

### v2

路径：

- `ledgers/v2/shards/shard-<n>.json`
- `ledgers/v2/ledger-meta.sqlite`
- `ledgers/v2/ledger-v2-index.json`

记录包含 `absolute_logical_ms`、`algebra_generation`、`algebra_spec`、`input_digest`、`result_hash` 和 `completion_sources`。

v1→v2 时：

- 绝对逻辑毫秒由固定基准 `1767225600000` 与相对秒数推导；
- 代数补默认值 `1`，来源为 `default:1`；
- `input_digest` 优先取当前状态，否则补全零哈希；
- `result_hash` 优先映射 `v1.output_digest`，必要时才补零哈希；
- 每个字段的来源逐项写入 `completion_sources` 和迁移报告。

v2→v1 时保留相对秒、job_id、结果摘要；显式丢弃 `algebra_generation`、`algebra_spec`、`input_digest` 和 `completion_sources`，并在 `migration-report-v2-to-v1.json` 中逐项记录丢弃规则。未显式授权的 schema 版本倒退由 `guard_schema_transition` 抛出 `E_LEDGER_VERSION_REGRESSION`。

## 6. 四阶段快照与强制终止恢复

阶段：

1. `validation_plan`（校验计划）
2. `block_accumulate`（乘加累加）
3. `aggregate_verify`（聚合校验）
4. `report_export`（报告导出）

快照位于 `snapshots/snapshot-XXXXXX-<stage>.json`，包含：

- 全局单调递增 `version`；
- `run_id`、阶段名、逻辑毫秒；
- 排序后的 `completed_jobs`；
- 每 job 节点键与制品哈希；
- `state_checksum` 与 `snapshot_checksum`。

一个阶段的所有 job 完成后，才在同一把非阻塞提交锁内提升暂存缓存并原子替换快照文件、版本计数器和 run 索引。阶段中途写入的 chunk `.npy` 位于 `staging/<run_id>/<stage>/chunks/`，不属于已提交缓存。

恢复时：

1. 从 `pending-run.json` 读取请求；
2. 从 run 索引找最近提交阶段；
3. 删除未完成阶段的 staging；
4. 已提交阶段直接跳过并从缓存读取制品；
5. 从下一阶段继续；
6. 若第四阶段快照已存在但输出/账本尚未写完，则幂等补齐。

`recovery-demo` 会在第二个 chunk 写盘后发送 `SIGKILL`。实测子进程返回 `-9`，重启后结果与不间断控制组逐字节相同，快照版本仍为 `[1,2,3,4]`。

## 7. 并发与资源预算

`CommitLock` 对 `locks/commit.lock` 使用 `flock(LOCK_EX | LOCK_NB)`。后到者立即得到 `E_STAGE_BUSY`，不等待。阶段快照、缓存提升和增量报告提交均使用该锁。

可配置预算：

- `max_jobs_per_stage`
- `max_accum_buffer_bytes`
- `max_parallel_jobs`
- `resource_policy=degrade|error`

内存估算包含四个 tile float64 缓冲、完整输出和分块面板。超预算且策略为 degrade 时，block_size 反复减半直到满足预算或降到 1，并记录 `MEMORY_BUDGET_REDUCED_BLOCK`。请求并行数超过上限时默认钳制并记录 `PARALLELISM_CLAMPED`；error 策略或 job 数超限返回 `E_RESOURCE_LIMIT`。

## 8. 工作台与自检

启动：

```bash
python -m gemm_bmm_workbench.cli serve --state-dir runs/demo --port 8899
```

工作台展示四阶段 DAG、所选 job 的命中/重算状态、完整键组成、增量集合、v1/v2 账本、偏差直方图、最近业务码和自检结果。页面按钮可重新自检、运行探针和执行账本迁移。

自检覆盖：

- 四个快照存在且校验和有效；
- 各阶段 completed job 集合与当前状态一致；
- 缓存键可由 key composition 重算；
- JSON 载荷哈希、blob 哈希和结果文件哈希一致；
- 增量重算/复用集合不相交且覆盖三节点全集；
- v1/v2 同 job 摘要一致；
- 若存在 `byte-equivalence.json`，确认增量与从零全量逐字节相同。
