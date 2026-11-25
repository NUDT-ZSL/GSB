# OpVerify 本地算子验证服务设计

## 1. 范围与确定性

本工程不依赖网络、GPU、Docker 或真实模型。样本由 `np.random.default_rng(seed)` 和
`np.random.default_rng(weight_seed)` 按作业独立生成，矩阵以小端原始字节写入：

- `inputs/<job_id>.bin`
- `weights/<job_id>.bin`
- `tasks_v1.jsonl`
- `update_block_policy.jsonl`
- `update_geometry.jsonl`
- `update_shape.jsonl`
- `invalid_cases.jsonl`

统一契约使用 NCHW 四维形状；二维或三维 `input_shape` 会分别补成 `1,1,H,W` 与 `1,C,H,W`。
卷积核为 `[kH, kW]`，在通道间共享；支持 `float32` 与 `float64` 输入，参考路径始终使用
`float64` 累加。

## 2. 模块边界

- `opverify/contracts.py`：字段规范化、形状推导和错误码映射。
- `opverify/samples.py`：固定种子样本、JSONL 和矩阵二进制生成/加载。
- `opverify/reference.py`：卷积、nearest、bilinear、bicubic 的独立逐元素 float64 参考实现。
- `opverify/planner.py`：卷积/插值共用的输出分块、输入跨度和越界索引规则。
- `opverify/chunked.py`：统一分块执行路径，只依赖 planner，不导入 reference。
- `opverify/verify.py`：容差裁决和首个 C-order 偏差位置。
- `opverify/cache.py`：SHA-256 CAS 数组制品和缓存键。
- `opverify/checkpoint.py`：阶段检查点、作业集合、校验和与原子替换。
- `opverify/ledger.py`：SQLite 账本、阶段提交和非阻塞 `flock` 提交锁。
- `opverify/snapshots.py`：ledger/outputs/parameters 三类版本化整体快照。
- `opverify/service.py`：变更分类、依赖传播、分阶段执行、恢复和提交。
- `opverify/consistency.py`：跨模块自检与首个差异点输出。
- `opverify/verification.py`：全量/增量/SIGKILL/并发/回退的一键验收。

## 3. 错误契约

所有结构化错误均包含 `code`、`job_id`、`field` 和 `details`：

- `E_SHAPE_INVALID`：维度、kernel 或 scale 非法，或卷积输出为空。
- `E_DTYPE_UNSUPPORTED`：dtype 不在 float32/float64 契约内。
- `E_PADDING_INVALID`：padding 类型/长度错误或小于 0；声明合法范围为非负整数对。
- `E_STRIDE_INVALID`：stride 类型错误或任一方向小于 1。
- `E_REVISION_ROLLBACK`：提交 revision 小于账本 head revision。
- `E_TOLERANCE_EXCEEDED`：分块结果超出 `rtol=1e-9`、`atol=1e-10`，详情含首个偏差坐标。
- `E_STAGE_BUSY`：另一个增量账本提交持有非阻塞锁，后到者立即失败。
- `E_SNAPSHOT_ROLLBACK`：旧快照版本或同版本异校验和试图覆盖当前快照。
- `E_INCONSISTENT`：自检发现账本、复算、集合覆盖或版本不一致。

## 4. 分块与边界规则

`block_policy.candidates` 给出 `[block_h, block_w]` 候选；`selection` 支持
`balanced`、`largest_fit`、`min_waste`、`min_oob`。`build_plan` 记录每个候选的块数、
边缘空洞、越界索引单元和元素预算，并写入选中原因。

卷积和插值都通过 `iter_tile_ranges` 切分输出空间，通过 `required_input_span` 计算块所需
输入区间，通过 `resolve_indices` 处理越界：

- `zero`：越界位置映射到 0 号元素但掩码为 false，按 0 参与计算。
- `replicate`：clip 到边缘索引。
- `reflect`：使用周期 `2*(size-1)` 的无重复边缘反射。

参考实现内部有自己的边界映射，不调用分块路径；分块路径也不调用参考实现。

## 5. 缓存键

缓存键载荷包含：

```text
op_kind / interp_mode
input_hash
weight_hash
parameter_digest
block_policy
revision
upstream_artifact_hashes[]
```

数组制品以规范化小端 float64 字节存入 CAS，制品清单记录 shape、dtype 和原始字节摘要。

## 6. 增量失效规则

服务比较新任务与 SQLite `job_heads.task`：

| 变更 | 根作业重算阶段 | 下游 |
| --- | --- | --- |
| 新作业 / `input_shape` / 输入内容 | `input,reference,plan,chunk` | `chunk` 并绑定新 upstream hash |
| stride / padding / 其他数值参数 | `reference,plan,chunk` | `chunk` |
| 仅 `block_policy` | `plan,chunk` | 输出摘要不变，下游全部复用 |
| 仅 revision 绑定变化 | `chunk` | 按 upstream hash 决定 |

每次运行返回 `affected_jobs`、`recompute`、`reused`、`recompute_jobs`、`reused_jobs`、
`fully_reused_jobs`、`propagation` 和 `trigger_revision`。

## 7. 检查点、强杀恢复和账本

阶段顺序为 `input -> reference -> plan -> chunk -> commit`。阶段内每个作业完成后先向
SQLite `stage_commits` 做持久提交；只有阶段内全部作业处理成功，才原子替换
`checkpoints/<batch>-<stage>.json` 和 `checkpoints/latest.json`。

重启同一 `batch_id` 时：

1. 删除 `tmp/<batch_id>/`，丢弃未提交的分块临时文件。
2. 读取最近检查点。
3. 已有匹配 `stage_commits` 的作业直接跳过，不重新计算。
4. 缺少阶段提交的作业从该阶段重建。
5. SQLite 版本提交是账本线性化点；若快照 current 指针落后，启动时从不可变版本快照提升。

验收中有真实 `SIGKILL` 子进程（subprocess 返回 `-9`），恢复后跳过 3 个已提交 chunk 作业，
且 ledger/outputs/parameters 校验和与一次性计算完全相同。

## 8. 快照与并发

三类快照信封均为：

```json
{"snapshot_type": "...", "version": 4, "parent_version": 3, "entries": ..., "checksum": "..."}
```

每次都构造完整新信封，写不可变 `*-v00000004.json`，再 `os.replace` 当前文件；不做 JSON
增量 patch。旧版本覆盖返回 `E_SNAPSHOT_ROLLBACK`。

最终提交使用 `ledger.commit.lock` 上的 `flock(LOCK_EX | LOCK_NB)`。锁被持有时，第二个提交者
立即收到 `E_STAGE_BUSY`，没有等待或重试；SQLite 同时使用 `BEGIN IMMEDIATE` 和
`busy_timeout=0`。

## 9. 自检

`opverify check` 会：

- 校验三类快照版本和 checksum；
- 校验 SQLite 版本记录与快照 checksum；
- 校验 commit 检查点版本等于账本版本；
- 从记录参数重新执行参考和分块路径并比对 output/reference digest；
- 重建 cache key 并检查 CAS entry；
- 校验 affected 集合被 recompute/reused 集合覆盖。

任何失败均返回 `E_INCONSISTENT`，`details.first_difference.path` 指向首个差异模块/作业/字段。

## 10. 常用命令

```bash
python3 -m opverify.cli generate --out data/seed-sample
python3 -m opverify.cli validate-sample --sample data/seed-sample
python3 -m opverify.cli run --root runs/demo --manifest data/seed-sample/tasks_v1.jsonl --batch-id full
python3 -m opverify.cli check --root runs/demo --sample data/seed-sample
python3 -m opverify.cli race --root runs/race
python3 -m opverify.cli verify --workspace runs/verification
python3 -m unittest discover -s tests -v
```
