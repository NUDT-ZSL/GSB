# 技术说明：在线 softmax、失效传播与调度

## 1. 分块在线注意力内核

内核 `kernel.attention_block` 对输出块 `b` 按 K/V 块顺序流式累加。对每个 K/V 块：

1. `scores = Q_b · K_k^T * scale`，并应用因果掩码 `j <= i`。
2. 取块内行最大值 `m_k`，与历史最大值合并为 `m_new = max(m_old, m_k)`。
3. 稳定指数 `e = exp(scores - m_new)`，块指数和 `l_k`。
4. 用 `exp(m_old - m_new)` 重缩放历史指数和与输出累加器，再把 `e · V_k` 累加进去。
5. 全部 K/V 块消费完后输出 `O = acc / l`。

累加器、运行最大值与指数和均为 float64，观测输出转回 float32。块内加权求和显式按
“查询行 → 键位置 → 通道”的固定顺序循环，非整除尾部通过半开区间 `[start, end)` 精确
裁剪，因此同一输入在任意并发度下逐位一致。

## 2. 依赖图与精确失效

因果注意力下输出块 `b` 读取 K/V 块区间 `[0, b]`（尾部块区间按真实长度收缩）。
`DependencyGraph.invalidate_interval(lo, hi)` 先求读取该 K/V 区间的输出块集合，再取
其因果下游闭包（即最早受影响块及其之后的全部块）。未失效节点保留
`output_fingerprint` 且 `valid=True`，引擎在报告的 `reused` 清单中返回，且不会为其
调用内核。

## 3. 单次重算预算

每批 `run_batch` 对待重算队列按块序号升序切片 `[:budget]`：

- 被计划的块进入 `planned`，成功块进入 `completed`，异常块进入 `failures` 并留在队首。
- 未计划块以 `E_RECOMPUTE_PENDING` 保留在 `pending`，下一批继续处理。
- 预算扣减序列、完成/失败/复用清单都按块序号排序输出。
- 预算为 0 时只推进全局版本与快照，不执行任何块，随后批次可正常收敛。

## 4. 快照与恢复

`snapshot.write_snapshot` 在同目录建临时文件，`fsync` 后 `os.replace` 原子替换；
崩溃只会留下不被加载的 `.tmp` 文件。快照元数据含全局版本、配置指纹、输入指纹、
已完成块列表与恢复点，载荷另带 SHA-256 校验和。`restore` 依次验证校验和、格式版本、
配置指纹与输入指纹，任一不匹配抛出 `E_SNAPSHOT_STALE`。

## 5. 并发与确定性

线程池仅执行互不共享写入的块任务。所有任务以块序号为稳定排序键提交，`future.result()`
按提交顺序回收，并在落报告前再次按块序号排序。NumPy 矩阵乘法不引入随机状态，
Python 侧累加顺序固定，因此串行与并发结果字节一致（见 `test_determinism.py`）。

## 6. 精度审计与归因

`reference.py` 提供四条 float64 串行路径：

- `full`：全阶段 float64 真值。
- `max32`：行最大值先转 float32，模拟最大值选择舍入。
- `sum32`：指数与指数和转 float32，模拟指数和累加舍入。
- `weighted32`：softmax 概率转 float32 后再加权 V，模拟加权求和舍入。

审计逐块比较输出、行最大值与指数和的最大绝对/相对误差，并以引擎输出与三个变体的
距离确定主导误差阶段。等价性检查 `verify_equivalence` 直接比较增量状态与一次性全量
重算的块指纹和原始字节。

## 7. 异常注入与失败隔离

`injection.InjectionConfig` 的每个样例带稳定编号。阶段异常通过 `KernelInjector`
在 `score/normalize/weighted` 钩子抛出，输入类异常（NaN/Inf、长度不匹配）在内核
入口抛出。引擎在块任务内捕获异常，记录块序号、阶段、业务码、异常类型与注入编号，
异常块标记为无效且保留在待重算队列，其他块继续执行，异常结果不会进入复用集合。

## 验证结果（本机实际执行）

- `python3 -m unittest discover -s tests`：27 个用例全部通过。
- 三份示例配置（串行 budget=2、4 线程 budget=3、紧预算 budget=1）：
  重分块前后增量结果均与一次性全量计算等价，float64 审计全部在 `atol=rtol=1e-4` 内。
- `run_injections.py`：8 个注入项全部命中并写入报告。
- `demo_recovery.py`：中途丢弃引擎后仅依据完整快照恢复，最终全块等价。
