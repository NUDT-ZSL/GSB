# 容量评估诊断引擎技术说明

## 设计原则

- 不读取系统墙钟；窗口、阈值生效时间、水位和补丁顺序均使用注入逻辑时钟。
- 纯计算内核与 SQLite 状态分离；相同干净输入生成逐字节一致的 JSON 报告。
- 定点通路维护乘除余量账本，60 位 Decimal 高精度通路作为对照真值。
- SQLite upsert 全部幂等；工作单元按 `running -> committed` 做崩溃恢复。
- 多进程通过 `fcntl.flock(engine.lock)` 串行化，SQLite 使用 WAL、`busy_timeout` 与 `BEGIN IMMEDIATE`。

## 代码地图

- `capacity_engine/seed.py`：固定种子 `20261127` 样本与异常注入清单。
- `capacity_engine/validation.py`：解析、枚举/数值/哈希/代数校验和错误码。
- `capacity_engine/windows.py`：注入时钟窗口、唯一归属和请求集合摘要。
- `capacity_engine/precision.py`：Fraction 精确中间值、定点舍入、余量账本、Decimal 真值。
- `capacity_engine/thresholds.py`：v1 相对秒换算、v1/v2 裁决和依赖边。
- `capacity_engine/evaluator.py`：指标、阈值结论、预算、拐点和精度敏感分析。
- `capacity_engine/invalidation.py`：依赖闭包、补丁应用、重算/复用摘要校验。
- `capacity_engine/storage.py`：SQLite schema、检查点、幂等写入和锁。
- `capacity_engine/engine.py`：编排、崩溃注入、恢复和原子报告。
- `scripts/acceptance.py`：双跑、幂等、崩溃恢复、并发和精度验收。

## 窗口语义

重叠滑动窗口会使一个请求进入多个聚合窗口，与“唯一归属一个窗口、不重不漏”冲突。引擎区分：

- 唯一归属槽：`slot = floor((logical_clock_ms - origin_ms) / slide_ms)`。
- 长度区间：`[origin + slot*slide, origin + slot*slide + window_length)`。

当 `window_length_ms = slide_ms` 时，归属集合就是聚合集合；长度大于步长时允许回看聚合，但归属摘要仍严格不重不漏。分配后会比较全部 request sequence，并输出每实例窗口的序号区间、有序序号、计数和 `request_set_digest`。

## 定点与 Decimal 对照

四个指标是 CPU 利用率、内存占用率、nearest-rank P95 延迟和吞吐达标率。中间值使用 `Fraction`，最终按指标 `scale` 做受控舍入。

每次乘法和舍入除法均登记序号、操作数、分子分母、floor 商、`discarded_remainder`、余数分数、舍入模式/方向和误差上界。half-up/half-even 的单操作误差上界是 0.5 个定点单位，floor/ceil 类保守为 1 个定点单位。

报告字段：

- `evaluation.error_chains`：每个 obs/metric 的请求区间、标度和账本条目。
- `evaluation.window_error_summaries`：按窗口累计的同指标物理误差上界和定点单位上界。
- `evaluation.decimal_full_recompute`：60 位 Decimal 全量重算摘要。
- 每个 value 含 `decimal_value`、`fixed_physical_value`、`absolute_deviation`、`relative_deviation`、`error_upper_bound_physical` 和 `ledger_seqs`。

偏差超过误差上界时抛出 `CAP-2001-PRECISION-BUDGET-EXCEEDED`，错误详情保留账本序号。

## 阈值合并

v1 相对秒换算为：

```text
effective_at_ms = logical_clock_origin_ms + effective_after_seconds * 1000
```

换算记录包含 `scale_ms_per_second=1000`、舍入方向 `exact`、舍入前值和零余量。

同一 `<service_level>.<metric>` 的候选按以下元组裁决：

```text
(logical_clock_ms, source_format_priority, generation, candidate_id)
```

v2 来源优先级为 2，v1 为 1。裁决顺序为时钟更新、同时钟时 v2 来源格式优先、同一来源内代数更高，以及完全同版本时的确定性 ID 兜底。每个键保留候选列表、胜出来源、原始时间表示和裁决原因。

## 失效闭包

- 阈值更新/墓碑：目标等级和指标直接失效；反向服务等级依赖闭包中的调用等级传播失效。敏感对仅在同指标且端点结论失效时重算。
- 依赖变更：变更等级及其反向依赖闭包的全部指标结论和相关敏感对失效。
- 指标参数变更：该指标在所有窗口/实例上的数值、账本、结论和敏感记录失效。

`invalidation.recompute` 和 `invalidation.reuse` 精确列出键。提交前逐摘要比较复用的数值、结论、敏感记录和未变更依赖边。

## 拐点与敏感对

阈值结论输出 `stable_with_margin`、`capacity_inflection_band`、`capacity_threshold_crossed` 或 `threshold_tombstoned`，并包含阈值证据、候选结论、选中项和被否决原因。

敏感对包括同窗口跨服务和同实例跨窗口。差值小于 `pair_tolerance` 且顺序/关键端点依赖该差值时标记 `sensitive=true`。记录当前标度顺序、Decimal 顺序、更高标度顺序、合并误差上界、账本序号和 `would_flip_at_higher_scale`。

## SQLite 恢复

主要表：`work_units`、`window_assignments`、`window_watermarks`、`window_summaries`、`ledger_entries`、`metric_values`、`precision_comparisons`、`threshold_view`、`merge_decisions`、`dependency_edges`、`conclusions`、`sensitivity_records`、`invalidation_records`、`logical_clock_watermark`、`revisions` 和 `revision_reports`。

基线有 `window_aggregation`、`fixedpoint_conversion`、`threshold_merge` 三个检查点。阶段开始提交 `running`，阶段结果和 `committed` 在同一事务完成。补丁报告、闭包、敏感记录、水位和补丁工作单元在同一事务提交。

`--crash-at` 使用 `os._exit(86)` 模拟强制终止。恢复时已提交单元跳过，`running` 单元重放；确定性主键和 `(revision_id, seq)` 唯一约束保证账本不重复累计。

## 错误码

| 错误码 | 含义 |
| --- | --- |
| `CAP-1000-INPUT-NOT-FOUND` | 输入文件或 v2 元数据库缺失 |
| `CAP-1001-VALIDATION-SCENARIO` | 服务清单或期望速率非法 |
| `CAP-1002-VALIDATION-TRACE` | 轨迹序号、时钟、耗时或标记非法 |
| `CAP-1003-VALIDATION-METRIC-DEFINITION` | 窗口、标度或容差非法 |
| `CAP-1004-VALIDATION-THRESHOLD-V1` | v1 相对秒或阈值非法 |
| `CAP-1005-VALIDATION-V2-SHARD-HASH` | v2 分片哈希不匹配 |
| `CAP-1006-VALIDATION-V2-GENERATION-MONOTONIC` | v2 代数回退 |
| `CAP-1007-VALIDATION-V2-METADATA` | v2 SQLite 元数据不一致 |
| `CAP-1008-VALIDATION-PATCH` | 补丁非法 |
| `CAP-2001-PRECISION-BUDGET-EXCEEDED` | 偏差超过误差上界 |
| `CAP-9999-INTERNAL` | 内部不变量或复用摘要失败 |

## 环境备注

验证环境为 Python 3.10.12、Python `sqlite3` 模块 3.37.2、NumPy 2.0.0。当前容器没有 `sqlite3` shell 且无免密 sudo，因此验收脚本使用标准库 SQLite；在 WSL Ubuntu 可执行 `sudo apt-get install -y sqlite3` 获得 shell CLI。
