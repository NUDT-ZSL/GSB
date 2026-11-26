# 状态演进与恢复一致性诊断组件 — 技术设计

## 1. 目标与边界

组件把一次状态演进建模为有向无环计算图（DAG），五个节点按固定顺序执行：

```
初始状态校验 -> 分块状态变换 -> 跨块归约 -> 状态快照提交 -> 恢复裁决
```

全部输入由固定种子脚本 `scripts/generate_samples.py` 确定性生成，运行期不
访问网络、外部服务或 GPU；`sqlite3` 使用 Python 标准库，唯一第三方依赖是
`numpy`。可注入逻辑时钟 `statediag/clock.py` 不读取系统时间，保证时间戳
同样可复现。

## 2. 模块映射

| 业务要求 | 实现位置 |
| --- | --- |
| 1 确定性摄入与初始校验 | `statediag/ingestion.py`、`statediag/transforms.py` |
| 2 分块变换与跨块归约 | `statediag/transforms.py`、`statediag/model.py` |
| 3 三档精度执行层 | `statediag/precision.py` |
| 4 误差账本与归因 | `statediag/ledger.py` |
| 5 定向失效传播 | `statediag/cache.py`、`statediag/invalidation.py` |
| 6 两代配置快照 | `statediag/snapshots.py` |
| 7 阶段状态机与恢复 | `statediag/checkpoints.py`、`statediag/pipeline.py` |
| 8 并发控制与自检 | `statediag/concurrency.py`、`statediag/consistency.py` |
| 内存预算 | `statediag/memory.py` |

## 3. 确定性算子语义

状态向量由 `numpy.random.default_rng(state_seed).standard_normal(dim)` 生成，
固定种子下逐字节稳定。变换采用显式 Jacobi 迭代，单步对每个元素执行：

```
x'[i] = ALPHA*x[i] + BETA*x[i-1] + GAMMA*x[i+1] + DELTA*(step+1)
```

系数 `ALPHA/BETA/GAMMA/DELTA` 在 `statediag/model.py` 固定，块边界缺失邻居
按 0 处理。块内用 Kahan 补偿求和；分块串行与整段一次执行调用完全相同的
逐元素算子，因此 float64 下两条路径逐元素位级一致（见
`tests/test_transforms.py`）。分块串行每步对新状态做完整拷贝，保证显式
Jacobi 读取的是旧状态快照，避免块切片视图污染。

## 4. 三档精度

- `float64`：原生 float64 补偿求和，作为参考真值，账本误差为 0。
- `float32`：同一算子、同一顺序，仅把数组与系数降为 float32。
- `fixed`：状态以 int64 表示，缩放因子 `quant_scale` 与舍入模式
  （`half-even/half-up/truncate`）由配置给出，整数运算无累加舍入。

切换精度档不改变分块边界、变换顺序或归约口径；差异只来自数值精度。

## 5. 误差账本

以 float64 整段路径为参考真值，对四级（初始校验、分块变换、跨块归约、
定点量化）记录最大绝对偏差、最大相对偏差与误差放大比，并给出主导来源、
首个偏差步骤与定点量化边界（缩放因子、舍入模式、整数/浮点极值）。任一级
超过配置容差即返回 `E_TOLERANCE_EXCEEDED`，账本仍完整落盘。

## 6. 内容寻址与失效闭包

缓存键 `statediag/cache.py::content_key` 对任务全字段与代数配置做规范化
SHA-256。三类变更：

- 仅 `revision` 提升：首个修订点及其后缀进入失效闭包。
- 仅配置代数提升：新代数下全部任务失效（键随代数改变）。
- `block_size` 变化：变化点及其后缀进入闭包；内容键未变且已提交者复用。

重算集与复用集保证无交集、并集覆盖全部任务，由一致性自检强制校验。

## 7. 配置快照

- v1：单文件 JSON，相对生效秒 + 扁平参数，无代数/墓碑。
- v2：参数与墓碑分片 JSON + SQLite 元数据库，记录绝对毫秒、代数、墓碑。

v1→v2 逐项记录补全来源；v2→v1 对无法表达的墓碑按显式规则丢弃并登记。
版本号或校验和回退一律返回 `E_SNAPSHOT_ROLLBACK`；同版本校验和变化按
整体替换处理。

## 8. 检查点与恢复

检查点在阶段边界原子写入（临时文件 + `os.replace` + `fsync`），带单调
递增版本号、已完成 task_id 集合与内容校验和。未提交中间产物写入
`scratch/`，提交成功即作废。重启后 `RecoveryArbiter` 从最近检查点继续，
已提交任务不重算、不重复计入，恢复后的账本与无中断运行逐字节一致。

## 9. 并发与预算

`CommitGate` 用非阻塞 `Lock.acquire(blocking=False)` 实现，后到者立即得到
`E_STAGE_BUSY` 且不等待。`MemoryBudget` 以作用域方式对显式申报的数组字节
记账，超出固定预算立即返回 `E_BUDGET_EXCEEDED`，并记录高水位。
