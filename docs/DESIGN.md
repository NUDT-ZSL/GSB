# 设计说明

## 1. 目标与不变量

组件的核心不是单次矩阵乘，而是让“矩阵随批次演进”的过程可审计、可复用、可恢复。系统维护以下不变量：

1. 已接受写入版本全局严格递增。
2. 账本行归属、内存行状态、结果块三者来自同一提交版本。
3. 写区间与固定行块有交集时，该块整体重算；无交集的块保留原对象和原字节。
4. 重算区间与复用区间互斥，并覆盖全部行。
5. 账本快照与结果快照各自带版本和 SHA-256；服务只在两者都完整构建后原子换入新状态。
6. 检查点只包含已提交状态；恢复不重算已持久化的结果块。

## 2. 模块结构

| 模块 | 职责 |
| --- | --- |
| `codes.py`, `errors.py` | 稳定业务码、CLI 退出码、结构化错误 |
| `canonical.py` | 规范化 JSON、SHA-256、无损 float64 数组编码 |
| `csr.py` | CSR 严格校验、SciPy/稠密转换 |
| `case.py` | JSONL 案例解析与形状映射 |
| `batches.py` | 写入批规范化、校验和、版本与行区间校验 |
| `models.py` | 不可变 `RowState`、`RowBlock`、`Ledger`、`RuntimeState`、事件 |
| `compute.py` | 行块乘法、并行/分块重算、稠密参考与偏差 |
| `snapshots.py` | 账本/结果快照封装与校验 |
| `state_codec.py` | 快照编解码与检查点 bundle 校验 |
| `checkpoint.py` | 临时文件、fsync、原子替换、未提交残留检测 |
| `service.py` | 提交锁、状态机、自检、导入/导出、报告 |
| `operators.py` | `spmm`、`spspmm`、`execute_case` |
| `cli.py` | 命令行、拒绝写入演示、资源/并发/恢复演示 |

## 3. 样本与矩阵语义

`shape_mnk` 固定解释为 `[M, N, K]`：

- `A`: `M x K` CSR
- `B`: `K x N` CSR 存储
- SpMM：将 `B` 转为稠密 `float64` 后计算 `A @ B`
- SpMSpM：保留 `B` 的稀疏结构计算，再转稠密行块用于统一增量组合
- 真值：`A_dense.astype(float64) @ B_dense.astype(float64)`

增量结果以行块稠密 float64 存储，原因是：

- 可以直接逐字节比较；
- 行块结果与全局稠密参考的偏差可精确定位；
- 账本/检查点可以无损编码 float64；
- SpMM 与 SpMSpM 共享同一套区间复用协议。

独立 `spspmm` API 仍返回 SciPy CSR，以满足稀疏输出集成需求。

## 4. CSR 校验

校验顺序与业务码：

1. `shape_mnk` 维数、类型、正数性：`E_SHAPE_INVALID`。
2. dtype 仅允许 `float64`：`E_DTYPE_UNSUPPORTED`。
3. `indptr` 长度、起点、范围、单调性：`E_CSR_STRUCT`。
4. 列索引范围：`E_CSR_STRUCT`。
5. `indices`/`values` 长度与声明 `nnz`、`indptr[-1]` 一致性：`E_NNZ_MISMATCH`。

所有错误都带 `case_id` 和 `csr_a.*` / `csr_b.*` 段名。

## 5. 写入批次协议

批次字段：

```json
{
  "batch_id": "spmm_w01",
  "write_version": 1,
  "row_start": 0,
  "row_end": 4,
  "mode": "replace",
  "rows": [
    {"row": 0, "col_indices": [1, 4], "values": [0.5, -1.2]}
  ],
  "checksum": "sha256:..."
}
```

校验和覆盖规范化后的完整内容。接受顺序：

1. 校验内容结构与校验和。
2. 检查行区间、列索引和行完整性。
3. 获取非阻塞提交锁；失败立即 `E_WRITE_BUSY`。
4. 检查全局版本严格递增。
5. 检查 `batch_id` 未重复。
6. 复制行状态并应用覆盖/失效。
7. 计算受影响块并生成新结果。
8. 构建完整账本快照与完整结果快照。
9. 先原子写检查点，再原子换入内存状态。

### 覆盖规则

`LAST_ACCEPTED_WRITER_WINS_BY_STRICTLY_INCREASING_VERSION`：

- 同一行被多个批次覆盖时，版本更高且已接受的批次生效。
- 任何不高于当前账本版本的批次整体拒绝。
- `invalidate` 将区间内行置为空行，并推进这些行的版本。
- 完全重复内容仍推进账本版本，但结果块摘要不变，事件报告 `result_changed=false`。

被拒绝批次在复制状态前失败，因此不会改变账本、行状态或结果。

## 6. 行块与增量重算

行按 `row_block_size` 切成固定半开区间，例如 20 行、块大小 4：

```text
[0,4) [4,8) [8,12) [12,16) [16,20)
```

写入 `[3,7)` 命中前两个块，因此重算 `[0,4)` 与 `[4,8)`；后三个块原样复用。这个规则保证：

- 块内累加次序完全由该块当前行内容决定；
- 未命中的块不进行乘法、不重建数组、不改变摘要；
- 报告中的重算/复用区间是块边界上的精确分区。

每个 `RowBlock` 记录：

- 行区间；
- 结果 float64 数组；
- 结果摘要；
- 输入摘要（操作、B 校验和、行版本、行内容校验和）；
- 最近写入版本。

重算使用 `ThreadPoolExecutor`，每个块独立计算，结果按原块顺序合并，因此并行不影响确定性。`max_row_buffers` 控制一次驻留的块结果数量，`max_parallel_intervals` 控制并行 worker 上限。

## 7. 自检

`self_check()` 验证：

1. 块分区无空洞、无重叠，覆盖 `[0, M)`。
2. 每个增量事件的重算区间 + 复用区间无交集且覆盖全部行。
3. 每个块用当前行状态独立重算，结果字节、结果摘要、输入摘要一致。
4. 从未被写入触及的块（`last_write_version == 0`）与基线行结果逐字节一致。
5. 从基础 CSR 重放账本写入，得到的行状态与当前行状态完全一致。
6. 最终结果与 float64 稠密参考在 `abs + rel` 容差内一致。

任一项失败返回 `E_INCONSISTENT`，包含 `case_id`、区间和不一致类别，例如：

- `block_recompute_mismatch`
- `untouched_digest_changed`
- `interval_overlap`
- `interval_coverage`
- `ledger_mismatch`
- `reference_mismatch`

## 8. 快照

两类快照：

- `write_ledger`：版本、已接受批次、行归属摘要。
- `row_results`：版本、每个块的编码结果与摘要。

快照校验和覆盖：

```json
{"category": "...", "version": 5, "payload": {...}}
```

导入时：

- 校验和错误：`E_SNAPSHOT_CORRUPT`，并报告类别和版本。
- 版本更低：`E_SNAPSHOT_ROLLBACK`。
- 同版本但校验和不同：`E_SNAPSHOT_ROLLBACK`。
- 版本更高：整体替换账本、行状态、结果块、事件与下一批次索引，不做局部修补。

## 9. 检查点与强制终止

检查点 bundle 包含：

- case ID、操作、基础矩阵校验和、配置校验和；
- `next_write_index`；
- 完整账本快照；
- 完整结果快照；
- 增量事件历史；
- bundle SHA-256。

落盘协议：

1. 写 `.checkpoint.json.tmp`；
2. `flush + fsync`；
3. `os.replace` 原子覆盖；
4. `fsync` 目录。

恢复：

1. 若有 `checkpoint.json.pending`，记录后删除；
2. 解码并验证 bundle、账本快照、结果快照；
3. 从基础矩阵重放已接受批次，重建行状态并校验账本；
4. 校验每个结果块的输入摘要、结果摘要、行版本与分区；
5. 不执行乘法重算，直接装载已提交块；
6. 从 `next_write_index` 继续。

强制终止演示在已提交第 2 批后留下未提交 side file，再对自身发送 `SIGKILL`。重启后该文件被丢弃，结果与不间断执行逐字节一致。

## 10. 并发与原子查询

- 写者：非阻塞获取 `RLock`，失败即 `E_WRITE_BUSY`。
- 读者：短暂阻塞获取当前不可变 `RuntimeState` 引用，然后在锁外物化。
- 写者只在所有块重算、两个快照构建和检查点写入完成后替换 `_state`。
- 报告只用一次 `snapshot_view()` 的账本、块、事件和下一索引生成，避免混合新旧元数据。

## 11. 资源策略

| 参数 | 超限行为 |
| --- | --- |
| `max_resident_batches` | 立即 `E_RESOURCE_LIMIT` |
| `max_row_buffers` + `strict` | 立即 `E_RESOURCE_LIMIT` |
| `max_row_buffers` + `degrade` | 分块执行，标记 `row_buffer_limit_chunked` |
| `max_parallel_intervals` 超限 | 限制 worker，标记 `parallel_interval_limit_capped` |

所有事件与最终报告都含 `degraded` 和 `degradation_categories`。
