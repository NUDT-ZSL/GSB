# 技术设计

## 1. 模块边界

```text
JSONL manifest
    │
    ▼
models.py ── 校验字段、唯一 job_id、同 tag 维度一致性
    │
    ▼
scheduler.py ── 资源估算、排序、准入/降级/拒绝、并行分组
    │              └── plan snapshot（整文件原子提交）
    ▼
executor.py ── spawn ProcessPoolExecutor，逐组并行、确定性归并
    │              └── worker 内每任务捕获异常，结果一一对应
    ▼
ledger.py ── 单锁预留/结算；失败释放；降级归还审计
    │              └── ledger snapshot
    ▼
results snapshot
    │
    ▼
validator.py ── 快照版本/校验和 + 账本 + 串行基线独立复核
```

## 2. 确定性计算协议

### 2.1 种子派生

不依赖 Python `hash()`、系统时间、进程 ID 或字典迭代顺序。所有种子由 SHA-256 派生：

```text
feature_seed = SHA256("features" | task.seed | batch | seq | dim) 的低 63 位
target_seed  = SHA256("targets"  | task.seed | batch | seq | dim) 的低 63 位
model_seed   = SHA256("model"    | model_tag | dim)               的低 63 位
```

同一 `model_tag` 和 `feature_dim` 总是还原同一组权重，支撑推理只读权重共享。

### 2.2 数值路径

数组统一为 `float64`。前向使用显式广播乘法和 `np.sum` 归约，不依赖平台相关 BLAS 矩阵乘法路径：

```text
logits = sum_f(features[b,s,f] * W[f,g]) + bias[g]
scores = tanh(logits)
loss   = mean((scores - targets)^2)
```

训练反向：

```text
d_scores = 2 * (scores - targets) / numel
d_logits = d_scores * (1 - scores^2)
grad_W   = sum_{b,s}(features[b,s,f] * d_logits[b,s,g])
grad_b   = sum_{b,s}(d_logits[b,s,g])
W        = W - learning_rate * grad_W
b        = b - learning_rate * grad_b
```

训练每个 step 记录一次 loss；全部更新后做一次前向，仅用于最终预测校验和。推理执行 `warmup` 次只读前向，不改权重，梯度范数固定为 `0.0`。

### 2.3 摘要字节一致

结果与摘要使用以下规范化 JSON：`sort_keys=True`、固定分隔符、UTF-8、禁止 NaN/Infinity。预测校验和为模型输出 `float64` 连续字节的 SHA-256；摘要和完整结果记录再分别生成内容校验和。

实际确定性边界是同一 Python/NumPy/平台环境。默认验证容差为 0，因此并行与串行结果要求逐字节一致。

## 3. 资源估算

```text
params       = F^2 + F
weight_bytes = params * S
activation   = B * L * F * S
forward_ops  = 2 * B * L * F^2 + B * L * F
backward_ops = 2 * forward_ops + 2 * params
```

其中 `B=batch_size`、`L=seq_len`、`F=feature_dim`、`S=bytes_per_scalar`。

```text
train_private_bytes = weight_bytes
                    + gradient_buffer_copies * weight_bytes
                    + activation_copies * activation
train_compute       = steps * (forward_ops + backward_ops) + forward_ops

infer_private_bytes = activation_copies * activation
infer_compute       = warmup * forward_ops
```

训练权重始终私有。推理任务若其 `model_tag` 已被准入，则该任务新增权重内存为 0；否则计入一次只读权重。

## 4. 准入算法

候选排序键：

```text
(-priority,
 -shared_infer_weight_benefit_bytes,
 parameter_count,
 job_id)
```

`shared_infer_weight_benefit_bytes` = 同 tag 其他推理任务数 × 本模型权重字节。该排序使同等优先级下能放大权重共享收益的任务优先，再以更小参数量提高预算利用率，最后用 `job_id` 消除平局。

逐个候选执行：

1. 先按全量参数估算。
2. 若槽位、计算、内存均满足，直接准入。
3. 若允许降级，按稳定顺序尝试半折参数。
4. 首个满足约束的降级组合准入，并记录原始值、生效值、类别和归还量。
5. 均不满足则拒绝，记录主要业务码、全部阻塞约束和缺口。

拒绝码优先级由配置 `rejection_code_precedence` 声明，默认：

```text
E_SLOT_BUDGET > E_COMPUTE_BUDGET > E_MEMORY_BUDGET
```

准入任务按准入顺序分块，每块最大 `max_slots` 个任务；块是执行顺序和进程池分块的唯一依据。

## 5. 预算账本

账本在初始化时一次预留全部准入任务的原始资源，并在同一临界区应用所有降级归还。条目保留四组可核对数值：

```text
original_*          原始估算
degrade_returned_*  降级归还
effective_*         原始 - 归还
consumed_* / released_*  成功消费 / 失败释放
```

所有查询通过 `BudgetLedger.snapshot()` 获取深拷贝。测试中的并发读者只能看到 `reserved` 或 `settled` 的完整状态，不会看到部分扣减。

必须成立：

```text
consumed + released = effective
sum(entry effective) = ledger totals effective
effective <= configured limits
```

## 6. 进程池执行

- `multiprocessing.get_context("spawn")`，兼容 Windows。
- 每个计划分组作为独立分块；组内提交到进程池并行执行，组完成后才进入下一组。
- `as_completed` 只影响完成通知，结果严格按计划组内 `job_ids` 顺序归并。
- worker 顶层函数接收纯字典载荷，避免依赖 fork 继承状态。
- 每任务在 `run_job` 内捕获 `BusinessError` 和普通异常，返回结构化错误记录。
- 基础设施异常只替换该 future 对应任务为 `E_WORKER_CRASH`，不重试、不重复归并。

## 7. 快照

每类快照文件形如：

```json
{
  "schema_version": 1,
  "category": "plan",
  "version": 7,
  "checksum": "<sha256>",
  "payload": {}
}
```

校验和覆盖 `schema_version + category + version + payload` 的规范 JSON。写入流程为同目录临时文件、`flush+fsync`、`os.replace`，最后重新读取完整文件验证。

加载时先完整解析与校验，再整体替换内存对象；任何解析或校验失败均抛 `E_SNAPSHOT_CORRUPT`，并尽可能从文件内容提取类别与版本。提交版本必须严格大于磁盘当前版本，旧版本会被 `E_STALE_VERSION` 拒绝。

## 8. 提交互斥

`StageGate` 使用 `O_CREAT|O_EXCL` 创建跨进程锁文件，竞争者不会阻塞等待，而是立即抛出 `E_STAGE_BUSY`。调度、执行、账本结算和三类快照提交都在持锁期间完成。

## 9. 验证器

验证器独立重算以下内容：

1. 三类快照封装校验和与版本是否一致。
2. `ledger.plan_checksum`、`results.plan_checksum`、`results.ledger_checksum` 是否闭合。
3. 账本条目集合是否等于准入集合，逐任务计算/内存估算是否一致。
4. 账本 effective、consumed、released 总数是否与计划和上限可核对。
5. 结果 `job_id` 是否唯一且与准入集合一一对应。
6. 对每个准入任务按 `job_id` 升序重跑串行基线；降级任务使用生效参数单独核对。
7. 首个不一致按确定性字段顺序返回 `job_id + field + actual + expected + deviation`。

即使账本业务检查失败但校验和仍有效，整体验证也不会误报通过。
