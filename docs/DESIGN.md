# 设计说明

## 1. 目标与边界

本实现把“压测请求 → 窗口观测 → 定点换算 → 误差账本 → 梯度结论 → 容量拐点”固化为一条只依赖 JSON 场景、逻辑时钟和本地回环 HTTP 响应的确定性链路。

关键边界：

- 请求释放时间、窗口边界、梯度持续时间和逻辑预算只由 `samples/scene.json` 中的逻辑时钟表推导。
- 系统墙钟只用于多进程运行时的短暂等待/超时，不参与请求排序、窗口归属、成功率、分位数或容量结论。
- 请求延迟由测试服务响应中的 `simulated_latency_us` 给出，不在引擎中用 `time.perf_counter()` 测量。
- 所有持久化在场景校验通过后才发生。

## 2. 场景与调度

`capacity_engine.scene` 负责解析和校验：

- 服务名、租户名、批次号唯一；
- HTTP 地址必须是 `localhost`、`127.0.0.0/8` 或 `::1`；
- 并发梯度严格递增且为正整数；
- 持续逻辑毫秒、请求节奏、窗口宽度、定点标度、容差和预算为正；
- 每租户成功率下限在 `[0,1]`，P95 上限为正。

`capacity_engine.clock` 以纯函数方式构建 `Schedule`。每个梯度包含若干 wave，每个 wave 在逻辑时刻 `release_ms` 释放 `concurrency` 个请求。wave 的递增量来自 `clock.step_table` 的循环序列；若表为空则使用批次的 `request_pace_ms`。

窗口锚定在梯度起点：

```text
window_index = (release_ms - gradient_start_ms) // window_ms
```

因此每个请求只属于一个半开区间 `[start_ms, end_ms)`，同一计划在任意次数运行下得到相同的请求 ID、窗口键和逻辑释放时刻。

## 3. 定点换算与账本

实现位于 `capacity_engine.ledger` 和 `capacity_engine.stats`。

- 比率：`success_count / request_count * percent_scale`，按 half-up 舍入为整数。
- 延迟样本：原始毫秒 Decimal 乘以 `latency_scale`，按 half-up 舍入为整数。
- P95：对已标度化延迟做稳定排序，取 `(n-1)*95/100` 的位置，低位与高位之间做线性插值，再 half-up 舍入。

每条登记记录包含：

- `scope_key`、`local_seq`、报告中确定性的 `seq`；
- 标度、精确有理数、标度化结果；
- 被舍弃余量、舍入方向；
- 本次换算误差、累计误差上界；
- 来源路径与操作细节。

报告中的 `seq` 由 `(scope_key, local_seq)` 排序后重新编号，保证多进程并行提交时报告仍可逐位复现；回查 SQLite 时使用 `scope_key + local_seq`。

窗口与梯度都有成功率和 P95 的独立登记。P95 误差上界按所选低位/高位样本的换算误差加权，再加上插值后的舍入误差。每个窗口和梯度都会与 `precision.error_budget` 换算后的上界比较。

## 4. 容量状态机

一个梯度在所有窗口提交后，从 SQLite 中重新读取完整窗口摘要并重算：

- `PASS`：成功率和 P95 都满足 SLO；
- `DEGRADED_PASS`：至少一项未满足 SLO，但违规量不超过 `degraded_margin`；
- `ABORT`：违规量超过 `degraded_margin`。

`allow_early_termination=true` 时，某一梯度 `ABORT` 会使同一批次后续尚未认领的梯度变为 `SKIPPED_EARLY_TERMINATION`。容量结论取最后一个 `PASS/DEGRADED_PASS` 的并发数；第一个 `ABORT` 的并发数为拐点。预算不足时相应单元变为 `ABANDONED`，原因为 `GLOBAL_LOGICAL_BUDGET_EXHAUSTED`；并发上限不足时为 `TENANT_CONCURRENCY_LIMIT_EXHAUSTED`。

## 5. 精度敏感与高标度翻转

对相邻梯度以及同服务、同梯度位置的租户做比较。当某指标差值小于 `compare_tolerance`，且该指标在原始/更高标度之间改变通过类别或影响容量边界时，输出敏感标记。

标记包含：

- 左右单元、窗口号、并发数；
- 两侧指标值、差值、容差；
- 两侧指标登记序号和参与窗口的登记引用；
- 两侧误差上界及差值误差上界；
- 原始容量结论、更高标度容量结论；
- `higher_scale_flips` 与 `capacity_conclusion_flips`。

高标度分析使用 latency/percent 标度各放大 100 倍，从窗口 payload 中的原始延迟重新计算。

## 6. SQLite 原子性、恢复与并发

状态文件采用 WAL、`synchronous=FULL`、`BEGIN IMMEDIATE`，并在写事务外层加文件锁。

主要表：

- `meta`：场景哈希、预算水位、逻辑时钟高水位；
- `units`：单元水位、认领者、预算预留、梯度裁决；
- `windows`：完整窗口 payload、摘要、哈希、登记序号范围；
- `ledger`：误差账本登记序列；
- `request_errors`：请求级异常诊断；
- `tenant_leases`：每租户并发槽位；
- `conclusions`、`sensitivity_marks`：完整分析快照。

一个窗口的窗口摘要、账本、请求诊断在同一事务中提交。重复提交同一窗口同一版本时：

- payload hash 相同：幂等返回，不重复计入；
- payload hash 不同：抛出 `E_WINDOW_SUMMARY_MISMATCH`。

恢复时先清理已死亡进程的 lease，把其 `RUNNING` 单元改回 `PENDING`；已提交窗口按确定性窗口键跳过，只重放未提交窗口。预算预留保存在单元上，恢复不会重复占用。

多进程调度通过 SQLite 写事务和文件锁抢占单元。同一批次在启用提前终止时按梯度顺序串行推进；不同租户/不同批次可并行。全局逻辑预算与租户槽位在事务中检查，避免并发超用。

## 7. 校验工具

`verify` 只读数据库并重新执行：

- 场景快照哈希；
- 每个窗口的 payload hash、摘要计数、成功率/P95；
- 每个窗口在 Schedule 中的精确请求集合和窗口边界；
- 每个窗口的完整账本；
- 梯度指标与裁决；
- 持久化容量结论与敏感标记；
- 全局预算、租户 lease 和整体完成状态；
- 请求 ID 全局唯一性。

任何不一致都进入 `issues`，CLI 返回码为 4。

