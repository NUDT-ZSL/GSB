# QConv Budget Lab 技术说明

## 1. 总体架构

应用把一次批量执行拆成七个可独立验证的层次：

1. `validation.py`：JSONL 任务与运行配置校验；
2. `scheduler.py`：确定性准入、预算账本、分块与 worker 决策；
3. `executor.py`：本地 `ProcessPoolExecutor` 分块执行；
4. `quant_conv.py`：量化、int32 分段卷积、参考值计算与 chunk 覆盖；
5. `diagnostics.py`：四级误差谱与元素级容差裁决；
6. `consistency.py`：跨层不变量自检；
7. `snapshots.py`：版本化整体替换与并发 stage 锁。

`service.py` 只负责编排，不隐藏预算或精度规则；自动化测试可直接调用任一层次。

## 2. 任务模型与校验

任务字段：

```text
job_id, input_shape[N,C,H,W], kernel_shape[K,Ck,Kh,Kw],
stride, padding, quant_scale, zero_point, acc_dtype, seed, priority
```

约束：

- `job_id` 非空且不可重复；
- 形状必须恰好四维且为正整数；
- `Ck == C`，卷积输出尺寸必须为正；
- `stride > 0`，`padding >= 0`；
- `quant_scale > 0` 且有限；
- `zero_point ∈ [-128,127]`；
- `acc_dtype` 支持集合为 `{"int32"}`；
- `seed` 非负，`priority` 为整数；
- `max_workers_per_task <= worker_slots`。

业务码：

| 场景 | 码 |
|---|---|
| 重复 ID | `E_DUPLICATE_JOB_ID` |
| 输入形状错误 | `E_INVALID_INPUT_SHAPE` |
| 卷积核形状/通道错误 | `E_INVALID_KERNEL_SHAPE` |
| stride/padding 错误 | `E_INVALID_STRIDE_PADDING` |
| scale 非正/非有限 | `E_INVALID_QUANT_SCALE` |
| zero_point 越界 | `E_INVALID_ZERO_POINT` |
| 累加类型不支持 | `E_UNSUPPORTED_ACC_DTYPE` |
| 其他任务字段错误 | `E_INVALID_JOB_FIELD` |
| worker 配置非法 | `E_INVALID_WORKER_CONFIG` |
| 预算参数非法 | `E_INVALID_BUDGET` |

校验失败会返回全部问题，且不会进入调度或执行阶段。

## 3. 确定性数组与量化

`np.random.default_rng(seed)` 先按 `input_shape` 生成 float64 特征图，再按 `kernel_shape` 生成 float64 卷积核。激活与权重使用同一任务的 per-tensor scale/zero-point：

```text
q = clip(rint(x/s)+zp, -128, 127)
xd = (q-zp)*s
```

数组保存为 `samples/arrays/<job_id>.npz`；manifest 同时记录每个数组的 dtype、shape 和内容校验和。`verify-samples` 从 seed 重新生成并逐元素比较。

## 4. 分块低精度卷积

输出形状：

```text
OH = floor((H + 2*ph - Kh)/sh) + 1
OW = floor((W + 2*pw - Kw)/sw) + 1
```

chunk 是输出坐标上的笛卡尔子块，包含 `[n_start,n_stop)`、`[k_start,k_stop)`、`[oh_start,oh_stop)`、`[ow_start,ow_stop)`。覆盖测试用 int16 计数体素，要求每个输出位置恰好覆盖一次。

每个 chunk：

1. 由 seed 重建 int8 激活/权重；
2. 中心化到 `[-255,255]`；
3. 按零填充后的 patch 做定点乘加；
4. 归约项按配置上限切段，每段使用 int32 累加器；
5. 各段结果提升到 int64 合并；
6. 乘以 `scale^2`；精确路径输出 float64，粗量化低内存路径输出 float32。

默认 int32 安全分段上限为：

```text
floor(INT32_MAX / (255 * 255)) = 33025 terms
```

开启保护时，用户配置的分段上限还会被 clamp 到该安全值。关闭保护时，以 int64 影子累加检测段内越界，一旦检测到即返回 `E_ACCUMULATOR_OVERFLOW`，阶段为 `accumulate_reduce`。

## 5. 调度与预算

### 5.1 稳定顺序

```text
(-priority, quant_rank, job_id)
quant_rank = {fine:0, balanced:1, coarse:2}
```

量化档由 scale 决定：

```text
fine:     s <= 0.02
balanced: 0.02 < s <= 0.06
coarse:   s > 0.06
```

### 5.2 准入

每个任务先要求一个 mandatory worker。检查顺序固定为 MAC、worker slot、内存。若全部通过，才扣减预算并进入候选准入集。

候选准入完成后，剩余 worker 槽位按准入顺序分配给“增加 worker 后预计轮数确实下降”的任务。升级 worker 时重新选择 tile，并重新核算驻留内存；若内存不够则保留旧计划。最终账本从最终计划重算，而不是累加增量，避免并发或升级路径留下残差。

### 5.3 内存模型

```text
full_output = N*K*OH*OW*output_bytes
per_worker  = input_bytes + weight_bytes
              + tile_input_accumulator_bytes
              + tile_output_bytes
resident    = full_output + workers * per_worker
```

精确归约路径中 `output_bytes=8`，粗量化低内存路径中为 4。

### 5.4 Tile 与循环顺序

候选 tile 尺寸使用 1、2 的幂、`ceil(dim/n)` 和完整维度，避免对大输出做全枚举。候选必须满足：

- 驻留内存不超过当前剩余预算；
- chunk 数不少于 worker 数。

评分优先最大化 tile 体积，再最小化 chunk 数；平局按 OW、OH、K 降序确定。`tile_k < K` 时使用 `n_oh_ow_k`，否则使用 `n_k_oh_ow`。

### 5.5 取舍记录

每个计划写入 `decision_basis` 与 `tradeoffs`：

```text
BLOCK_SIZE
LOOP_ORDER
WORKER_ALLOCATION
SEGMENTED_ACCUMULATION
COARSE_QUANT_REDUCED_REDUCTION
```

`plan_digest` 对配置、决策与准入计划做 SHA-256。相同输入/配置下规范 JSON 逐字节一致。

## 6. 进程池执行

主进程按 `(plan order, chunk_id)` 生成任务列表。worker 函数是模块级可 pickle 函数；每个分块返回：

```text
ok, job_id, chunk_id, output, stats
```

失败返回：

```text
ok=false, job_id, chunk_id, stage, code, message, detail
```

主进程按 `as_completed` 收集，但按计划的 job 顺序和 `chunk_id` 升序归并，因此完成顺序不影响结果。一个 job 任意 chunk 失败时丢弃该 job 的部分输出，其他 job 的输出和摘要不受影响。执行摘要记录每个 job 的归并顺序、输出校验和和失败归因。

## 7. 误差谱

对每个成功 job，分别计算：

| 阶段 | 参考 | 实际 |
|---|---|---|
| `quantized_input` | float64 反量化卷积 | float64 原始浮点卷积 |
| `fixed_mac` | float64 反量化卷积 | 一次性 int64 定点累加乘 `scale^2` |
| `accumulator_reduction` | 一次性 int64 定点结果 | 分块/分路径合并结果 |
| `dequantized_output` | float64 反量化卷积 | 最终执行输出 |

每阶段输出绝对值与相对偏差的 max/mean、P50/P90/P99、Top 位置，以及相对上一级、相对量化级的放大比。主导来源从 `quantized_input`、`fixed_mac`、`accumulator_reduction` 中选择阶段内最大绝对偏差，平局按阶段顺序确定。

最终容差：

```text
abs(actual - reference) <= atol + rtol*abs(reference)
```

越界元素记录 `[n,k,oh,ow]`、参考值、实际值、绝对/相对偏差、允许值。超过 `max_violations` 时仍记录总数和截断标志。

## 8. 版本化快照与并发

快照格式：

```json
{
  "format": "qconv-snapshot-v1",
  "generation": 2,
  "previous_generation": 1,
  "components": {
    "plan": {"version":2, "checksum":"...", "payload":{}},
    "budget_ledger": {"version":2, "checksum":"...", "payload":{}},
    "result_summary": {"version":2, "checksum":"...", "payload":{}}
  },
  "snapshot_checksum": "..."
}
```

校验层次：

1. 每个组件的 `sha256(canonical(payload))`；
2. 每个组件版本等于快照 generation；
3. 快照 body 的 SHA-256。

提交锁是 `mkdir` 原子目录锁，在 Windows/Linux 都可用且不阻塞。写入先落到同目录临时文件，`fsync` 后用 `os.replace` 整体替换。读取或提交发现损坏均返回 `E_SNAPSHOT_CORRUPT`；锁已存在立即返回 `E_STAGE_BUSY`。

Web 层另外用非阻塞 `threading.Lock` 防止同一交互应用同时运行两次演示；服务层快照锁负责跨线程/进程提交排他。

## 9. 一致性自检

提交快照前强制检查：

- 决策覆盖所有输入 job 且无重复；
- 每个准入 job 的 chunk 重新计算后恰好覆盖一次；
- 账本已提交 ID 与准入 ID 完全一致；
- MAC/内存/worker 账本求和等于 usage，且不超限；
- 被拒任务三类扣减均为 0；
- 执行摘要的 job 并集等于准入集合，成功/失败划分完整；
- 每个失败 job 有 job_id + chunk_id + stage；
- 误差谱恰好覆盖成功 job，四阶段完整；
- 主导阶段有可追溯的 Top 偏差位置；
- 每个容差越界项属于对应 job 且带四维位置。

任一不满足返回 `E_INCONSISTENT`，并且不提交快照。

## 10. 设计边界

- 使用 per-tensor scale/zero-point，权重和激活共享任务级量化参数；
- 参考实现是本地 CPU 的 float64 NumPy 卷积，不涉及 GPU 内核差异；
- `float32_chunk_reduce` 只在 `coarse` 档且精确路径无法满足内存时启用；
- 进程池并行度受 `worker_slots` 约束；任务数少于槽位时允许单任务多 chunk 并行；
- 快照只保存摘要、校验和与谱指标，不保存大数组，避免把快照变成结果数据仓库。

