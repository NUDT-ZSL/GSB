# 技术设计说明

## 1. 摄入与展开

`load_feature_dictionary` 解析并校验 `feature_index`、`dimension`、`type`、`cardinality` 和可选 `dictionary_version`。字典内容按 `feature_index` 排序后计算 SHA-256，因此文件行序不影响字典校验和。

`load_source_samples` 按输入行校验样本，完成后按 `sample_id` 升序排序。重复标识在完整摄入前即返回 `E_DUP_SAMPLE`，避免后续阶段出现歧义。

每个特征分量使用稳定桶：

```text
bucket = int(blake2b(f"{kind}:{feature_index}:{part}", digest_size=8)) % feature_dimension
```

处理顺序固定为用户特征升序、物品特征升序、交叉槽位升序。交叉值定义为左右特征向量的点积。同一桶的贡献按该顺序累加；最终只保留非零值，并按桶索引升序输出。

规范化展开行只包含：

```json
{"indices":[...],"label":0,"sample_id":"...","values":[...]}
```

JSON 使用 `sort_keys=True`、紧凑分隔符、禁止 NaN/Inf，因此同输入同版本的展开文件可逐字节比较。

## 2. 分块训练与归约

逻辑回归参数长度为 `feature_dimension + 1`，最后一维是 bias。单样本：

```text
z = bias + sum(w[j] * x[j])
p = sigmoid(z)
loss = logaddexp(0, z) - y*z
grad_bias += p-y
grad[j] += (p-y)*x[j]
```

分块输出 `loss_sum` 与未平均梯度。主进程按 `(batch_index, chunk_index)` 排序后：

```text
total_loss = sum(chunk.loss_sum)
total_grad = sum(chunk.grad)
loss = total_loss / n
grad_mean = total_grad / n
w = w - learning_rate * (grad_mean + l2*w)
```

整段对照路径不使用分块中间量，而是按 `sample_id` 顺序逐样本累加。两路径从相同零参数出发，因浮点累加顺序不同允许微小差异；验收阈值要求最终最大参数绝对偏差小于 `1e-12`。

每个分块键为全局 `chunk_id`，同时携带 `batch_index` 与批内 `chunk_index`。worker 异常被捕获为：

```json
{
  "code":"E_WORKER",
  "stage":"chunk_gradient",
  "details":{"batch_index":0,"chunk_index":1,"sample_ids":["..."]}
}
```

所有任务先全部派发并收集；任何一块失败都不会清除其他块结果，但该轮不会用缺失块做归约，训练结果标记为 `partial`。

## 3. 预算模型

当前估算器对第 `r` 轮使用以下近似：

```text
sample_macs = 2 * nnz + 4 * sample_count
reduction_macs = max(0, chunk_count - 1) * (feature_dimension + 2)
update_macs = feature_dimension + 1
worker_macs = workers * worker_overhead_macs_per_round
total_macs = rounds * (sample + reduction + update + worker)
```

驻留内存：

```text
sample_memory = sample_count * (sample_base_bytes + score_bytes)
               + nnz * nnz_bytes
parameter_memory = (feature_dimension + 1) * 16
worker_memory = workers * worker_memory_bytes
chunk_memory = chunk_count * chunk_overhead_bytes + chunk_size * 64
```

预算器枚举请求分块大小的二分级候选和 `1..workers` 的 worker 候选。每个候选按样本前缀逐块累计，超过任一预算即停止。候选排序键为：

```text
(admitted_sample_count, chunk_size, workers,
 -estimated_memory, -estimated_macs)
```

这会先最大化准入覆盖，再在覆盖面相同的情况下选择更低归约开销和更高并行度。由于候选顺序固定，同一观测下结果可复现。

默认不自动缩小 `feature_dimension`。报告中的 `feature_projection.applied=false` 与理由明确记录“保持交叉项容量”的裁决。

## 4. 独立一致性验证

验证器不调用训练器内部指标函数：

- AUC 使用正负样本两两比较，平局计 `0.5`。
- LogLoss 从逐样本 `score/label` 直接重算。
- 从 `initial_parameters` 开始，按轮次和分块顺序累加 `loss_sum`、梯度，重建每轮参数与最终系数。
- 阶段集合满足：
  - `ingested == expanded`；
  - `admitted` 与 `rejected` 不交叠；
  - `admitted ∪ rejected == ingested`；
  - 正常模式下 `scored == admitted == summary.trained_sample_ids`；
  - 被拒样本不得出现在分数明细、分块集合或训练摘要。

失败时返回：

```json
{
  "code":"E_INCONSISTENT",
  "stage":"validation",
  "details":{
    "category":"gradient_reduction_mismatch",
    "first_deviation":{
      "stage":"chunk_reduction",
      "position":{"round":1,"parameter_index":3}
    }
  }
}
```

## 5. 快照与并发

快照文件是规范化 JSON 信封：

```json
{
  "checksum":"...",
  "object_name":"training_summary",
  "payload":{},
  "schema_version":1,
  "version":2
}
```

校验和覆盖 `object_name + schema_version + version + payload`。`publish` 要求版本单调增加；内容改变但版本不变返回 `E_CONFIG_INVALID`。写入采用同目录临时文件、`fsync`、`os.replace`，避免暴露半写文件。

`SnapshotCache.reconcile` 在版本或校验和变化时整体替换内存对象，不做字段级合并。

提交门闩使用 `fcntl.flock(..., LOCK_EX | LOCK_NB)`。Linux 上不同 `open` 描述符之间互斥，因此线程或进程的第二个提交会立即得到 `E_STAGE_BUSY`。

账本所有读改写均在 `threading.Lock` 内完成。事务标识已存在时扣减幂等返回 `applied=false`；释放不存在的标识返回零金额且不改变余额。

## 6. 测试覆盖

`tests/` 覆盖：

- 同种子逐字节生成；
- 字典/曝光日志乱序后的展开一致性；
- 重复标签、非法维度、重复样本；
- 1/3 worker 确定性；
- 分块/整段偏差谱与收敛轮次；
- worker 故障定位和其他块保留；
- 计算/内存受限模式及严格模式；
- AUC、归约、被拒样本篡改检测；
- 快照版本、校验和损坏、整体替换；
- 非阻塞提交冲突与并发账本。
