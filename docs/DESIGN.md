# 设计与算法说明

## 1. 分层

```text
sampling_ops/
  types.py      任务解析、dtype/平局口径规范化
  generator.py  固定 seed 的 NumPy logits 与 demo JSONL
  topk.py       三条精确 TopK 与候选一致性核对
  metrics.py    比较次数/驻留内存估计模型
  decision.py   选路决策、冲突理由、决策字节指纹
  sampling.py   温度、top-p、局部 PCG64 采样
  bias.py       float64 参考裁决与样本重放自检
  injection.py  四阶段无侵入注入
  snapshots.py  版本化 whole-snapshot 替换
  pipeline.py   统一四阶段入口与归因
  cli.py        generate/run/demo/selfcheck
```

## 2. 候选阶段

三条路径都只依赖原始 logits 和统一键：

```text
key(score, index) = (score descending, index ascending)
```

### full_sort

逐 batch row 使用稳定 `argsort`。稳定性保证同分时原始索引升序。比较次数按归并/稳定排序上界模型估计。

### heap_select

维护容量为 `k` 的最小堆，堆键为 `(score, -index)`：

- 分数较低者先被淘汰。
- 同分时较大索引对应的 `-index` 更小，因此先被淘汰。
- 最终再按 `(-score, index)` 排序。

### chunked_two_stage

1. 每个 chunk 做稳定排序，取本地前 `min(k, chunk_size)`。
2. 本地候选立即送入容量为 `k` 的全局堆，形成流式第二阶段。
3. 最终统一排序。

块大小 `chunk_size` 被强制不小于 `k`，否则本地阶段无法保留足够候选。块内稳定 TopK 加全局同键堆可证明与全排序 TopK 一致：任何全局前 `k` 元素必然位于其所属块的本地前 `k`。

## 3. 比较与内存估计

估计器是版本化决策模型，不是 wall-clock 采样：

```text
full_sort comparisons = b * n * ceil_log2(n)
heap comparisons       = b * (n + (n-k)*ceil_log2(k+1) + k*ceil_log2(k))
chunk comparisons      = b * (本地排序上界 + 流式合并堆 + 最终 k 排序)
```

内存估计包含：

```text
输入 logits + 选路候选输出 + 路径工作集
```

堆路径计入 Python tuple/object 开销；分块路径工作集为 chunk 缓冲加局部/全局候选块，不随 `n` 线性增长。该差异反映“分块降低驻留但增加块边界比较”的业务冲突。

同一观测、同一 schema、同一算法版本下，决策记录被规范化为排序键 JSON 并计算 SHA-256；不使用时间戳或进程随机量。

## 4. 决策优先级

决策分两类输入：

- 尺寸：`vocab_size`、`batch_size`、`k_value`。
- 资源：比较硬上限、内存硬上限、软比例、chunk size。

处理顺序：

1. 计算三条路径估计。
2. 强制 `k_mode` 优先；若超限，仍执行但标记越限。
3. `auto` 下先过滤硬不可行路径。
4. 比较压力优先选择堆选择。
5. 内存压力下，大 `k` 优先分块两阶段，否则优先堆选择。
6. 无压力且规模较小时全排序；否则堆选择或分块。
7. 全部不可行时，按 `max(compare/max_compare, memory/max_memory)` 最小选择，标记 `RESOURCE_BUDGET_OVERRUN`。

所选路径在候选执行前写入 decision record，执行完成不回写 selected route。

## 5. 采样

候选分数先除以 temperature，再在 float64 上做稳定 softmax。top-p 截断：

```text
cutoff = min prefix length where cumulative_probability >= top_p
```

等号包含边界 token。截断后的概率再次归一化用于采样。采样使用：

```text
rng = Generator(PCG64(SeedSequence([seed_low, stream, seed_high])))
u   = rng.random()
pos = searchsorted(cdf, u, side="right")
```

每个 batch row、每个 step 顺序抽取。RNG 只由 case seed 和固定 stream 常量决定，不依赖路径、dtype 或全局状态。

## 6. 偏差与自检

参考路径独立构造为 `topk(float64(logits), route=full_sort)`，再走同一采样函数。对每条路径检查：

- 候选索引序列完全相等。
- 候选分数逐元素绝对偏差不超过 tolerance。
- 样本序列逐元素相等。

偏差报告保留逐元素分数偏差，并给出首个越界元素。索引不一致不会被分数偏差为 0 掩盖。

自检再次用每条路径的候选和同一 seed 重放采样，并与产出样本比较；同时核对三路候选哈希。失败返回 `E_INCONSISTENT`。

## 7. 注入与归因

注入控制器只操作调用方传入的参数字典副本与 logits 副本，不修改 JSON 任务对象或源码。阶段固定为：

```text
candidate_filter -> temperature_scale -> top_p_filter -> sample
```

预期业务错误在阶段内捕获，写入：

- `stage_trace`：阶段状态、业务码、字段、是否注入。
- `errors`：结构化错误记录。
- `injections`：注入配置、是否到达/应用、最终业务码。

候选阶段失败时下游全部 skipped。温度、top-p、采样阶段失败时保留已完成阶段产物，例如温度失败仍保留 TopK 候选，seed 失败仍保留候选与 top-p 结果。

框架级未知异常由最外层兜底写入 `unattributed_errors`。

## 8. 快照

快照不是增量 patch。`SnapshotStore.put` 的行为：

1. 读取完整旧快照并校验 checksum。
2. 内容相同返回旧版本。
3. 内容变化且未指定版本时使用 `old_version + 1`。
4. 指定版本不得小于等于旧版本。
5. 以临时文件 + `os.replace` 原子替换完整快照。

配置快照固定策略、边界规则、默认资源；统计量快照记录 case/status/route/bias/self-check/injection 汇总。两者版本独立。

## 9. 已知边界

- bfloat16 无 NumPy 原生存储，明确返回 `E_DTYPE_UNSUPPORTED`，不静默降级为 float32。
- 资源估计是决策模型，不等于操作系统实测 RSS；它的目标是可复现取舍，而不是 profiler 替代品。
- top-p 截断属于采样策略，不会删除 TopK 输出候选，便于审计“应保留但未参与采样”的候选。
- 若所有路径都超过硬上限，组件按“越界最小”执行并显式告警；不会伪造“已满足预算”。

