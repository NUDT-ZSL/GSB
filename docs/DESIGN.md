# 技术设计

## 1. 分层

| 层 | 模块 | 职责 |
|---|---|---|
| 输入 | `config.py`, `data.py` | 配置校验、JSONL 解析、固定排序与编码 |
| 预处理 | `standardization.py` | z-score/min-max、one-hot、统计量版本 |
| 数值 | `precision.py` | float64/float32/定点共享算子 |
| 决策 | `planner.py` | 分块边界、worker 降级、内存和比较估算 |
| 执行 | `parallel.py`, `solver.py` | spawn 进程池、块梯度、FISTA、预算 |
| 持久化 | `snapshot.py` | 整份快照、原子替换、校验和、提交锁 |
| 自检 | `validation.py` | 单调性、目标重算、版本一致性 |
| 编排 | `engine.py` | 四阶段状态机、恢复与汇总 |

## 2. 数学契约

输入标签映射为 `y_i in {-1,+1}`。标准化矩阵为 `X`，系数为 `w`，截距为 `b`。

```text
J(w,b) = 1/(2n) ||Xw + b*1 - y||^2
       + lambda*l1_ratio*||w||_1
       + 0.5*lambda*(1-l1_ratio)*||w||^2
```

L2 是 `l1_ratio=0` 的特例。截距永远不罚。

令 `A=[X,1]`，基础步长：

```text
L = lambda_max(A^T A / n)
step = lipschitz_safety / L
```

近端梯度步骤：

```text
v = y_k - step * grad J(y_k)
w_{k+1} = soft_threshold(v, step*lambda*l1_ratio)
          / (1 + step*lambda*(1-l1_ratio))
b_{k+1} = b_k - step*grad_b J(y_k)
```

FISTA 动量使用固定公式。若重启判据
`dot(y_k - w_{k+1}, w_{k+1} - w_k) > 0`，重置动量。所有比较按数组元素
确定性执行。

## 3. 全局归一化与块归约

块任务只计算本块的和：

```text
gradient_block = X_block^T (X_block*w + b - y_block) / n_global
intercept_block = sum(X_block*w + b - y_block) / n_global
sse_block = ||X_block*w + b - y_block||^2
```

主进程按 `block_index=0..B-1` 顺序：

```text
gradient = sum(gradient_block)
intercept_gradient = sum(intercept_block)
sse = sum(sse_block)
```

因此串行和并行使用完全相同的数学归约。worker 完成顺序不会进入结果。

## 4. 定点算术

定点表示：

```text
X_q = round(X*S), w_q = round(w*S), b_q = round(b*S), y_q = ±S
logit_q = round((X_q @ w_q) / S) + b_q
r_q = logit_q - y_q
```

梯度表示成参数 q 空间：

```text
g_w_q = round(X_q^T r_q / (n*S))
g_b_q = round(sum(r_q) / n)
```

更新：

```text
v_q = w_q - round(step*g_w_q)
threshold_q = step*lambda*l1_ratio*S
w_q_next = round(sign(v_q)*max(|v_q|-threshold_q,0)
                 / (1 + step*lambda*(1-l1_ratio)))
b_q_next = b_q - round(step*g_b_q)
```

舍入函数由 `nearest_even`、`nearest_away` 或 `truncate` 明确决定。整数点积和梯度
累加在超出安全 int64 范围前返回 `E_RESOURCE_LIMIT`。

## 5. 分块决策

估算内存：

```text
global_arrays  = n * (p+2) * 8
worker_arrays  = workers * n * (p+2) * bytes_per_value
worker_workspace = workers * block_size * (p+8) * bytes_per_value
resident = global_arrays + worker_arrays + worker_workspace
```

估算比较：

```text
per_iteration = n*(p+2) + block_count*p + (p+1)
total = sum(max_iterations) * per_iteration
      + path_size * n * (p+2)
```

其中 `(p+1)` 是确定性重启/收敛比较的保守估计。运行期由
`ComparisonBudget.consume()` 实际扣减，任何阶段超限都返回
`E_RESOURCE_LIMIT`，不会静默截断路径或迭代。

规划器显式接收 `comparisons_already_used`，以
`comparison_budget - comparisons_already_used` 作为当前剩余预算做准入检查，
并把已用值和剩余值写入决策依据。

决策顺序：

1. 路径数、每档迭代上限先做硬检查；
2. 估算比较预算，初始超限直接失败；
3. 驻留内存超限时先降 worker；
4. 仍超限则二分缩小分块到 `min_block_size`；
5. 仍超限失败。

分块边界由 `range(0,n,block_size)` 生成，末块截断。输出中同时写入边界和覆盖
证明。

## 6. 快照事务

快照文件是不可变 JSON envelope：

```text
payload_checksum = SHA256(canonical_json(payload))
envelope_checksum = SHA256(canonical_json(metadata + payload_checksum))
```

`manifest.json` 保存当前版本、阶段、文件名和两份校验和。提交顺序：

```text
acquire flock(non-blocking)
read + verify current
check expected version and checksum
write new snapshot temp -> fsync -> rename
write manifest temp -> fsync -> rename
release lock
```

仅 manifest 指向的快照生效。即使旧文件仍在目录中，也不会覆盖新版本。

引擎不维护第二套可变缓存目录。标准化统计量、分块决策、路径前缀和最终结果都
放在同一份原子快照里，因此缓存命中、校验和验证和恢复使用同一个权威对象。

四个阶段写入的 load 状态都包含完整累计路径，不做增量 patch。每个 `path_solve`
状态的 `path` 只追加已经完整求解的档位，`phase_complete` 只在最后一个档位为
true。

## 7. 恢复算法

```text
load + verify manifest and snapshot
validate run_id/config/data fingerprints
validate stats content hash and prior path prefix
if phase == path_summary:
    replay stored result_document
elif phase == path_validation:
    rebuild summary
else:
    rebuild X from committed stats without refitting stats
    reuse committed chunk plan
    warm start from last committed coefficient
    solve only missing strengths
```

档内迭代状态从未落盘，因此中断恢复的唯一可用起点就是最后一个已提交档系数。
比较预算记录也只包含已提交档位，未提交档的迭代和比较被丢弃。

## 8. 失败归因

`EngineError` 的稳定字段：

```text
ok=false, code, message, stage, version, strength, strength_index,
block_index, chunk_index, details
```

worker 返回：

```json
{
  "ok": false,
  "error": {
    "code": "E_WORKER",
    "stage": "PATH_SOLVE",
    "strength_index": 7,
    "strength": 0.1,
    "block_index": 2,
    "block_start": 96,
    "block_end": 144,
    "worker_pid": 1234,
    "exception_type": "RuntimeError"
  }
}
```

主进程再包装为同码异常，保留 `worker_error` 原文。

## 9. 确定性边界

保证同一机器、同一 Python/NumPy 环境、同一配置和数据下逐字节一致：

- 输入行按 `row_id` 稳定排序；
- 类别按 UTF-8 字节序排序；
- 不依赖进程完成顺序、时钟、随机数或 hash 随机化；
- JSON 使用 canonical key 排序和紧凑分隔符；
- 每个 worker 的 BLAS 线程数默认限制为 1；
- 所有浮点输出由同一确定性归约路径产生。

不承诺跨 CPU 架构、BLAS 版本或 Python/NumPy 大版本逐字节一致。

