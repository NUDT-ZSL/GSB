# sensor_stream_agg

面向传感器事件流的本地时序窗口聚合组件。核心只依赖 Python 3.10+ 标准库；NumPy 不是运行时依赖。窗口结果、Neumaier 补偿累加状态、水位线、迟到丢弃日志均持久化到 SQLite。

## 快速运行

```bash
# 可选；核心与演示均不需要 NumPy
python -m pip install numpy

python -m unittest discover -s tests -v
python -m sensor_stream_agg.demo
```

演示会清除并重建 `demo_output/`，使用固定本地种子生成事件。完整确定性输出已保存到 `artifacts/demo_run.txt`。连续两次运行的标准输出 SHA-256 相同。

## API 示例

```python
from sensor_stream_agg import Aggregator, AggregatorConfig, Event

config = AggregatorConfig(
    strategy="fixed",             # 或 "sliding"
    window_length_us=100_000,
    allowed_lateness_us=25_000,
    # strategy="sliding" 时必须提供 0 < slide_us <= window_length_us
    slide_us=None,
)

with Aggregator("sensor.sqlite3", config) as aggregator:
    result = aggregator.ingest(
        Event(
            event_time_us=1_360_000,
            device_id=9,
            sequence=900,
            observation=12.5,
            weight=1.0,
        )
    )
    if result.code == "OK":
        for step in result.steps:  # 本次事件触及窗口的最新误差界
            print(step.sum_value, step.sum_error_bound)

    aggregator.flush()  # 显式结束流：水位线推进到所有开放窗口的最大 end
    for window in aggregator.list_windows("closed"):
        report = aggregator.error_report(window.window_id)
        print(window.sum_value, window.rms_value, window.weighted_mean)
        print(report.sum_deviation, report.code)
```

主要公开接口：

- `Aggregator.ingest(event | mapping) -> IngestResult`
- `Aggregator.flush(watermark_us=None) -> FlushResult`
- `Aggregator.get_window(window_id) -> WindowResult | None`
- `Aggregator.list_windows(state=None) -> list[WindowResult]`
- `Aggregator.step_errors(window_id) -> tuple[WindowStep, ...]`
- `Aggregator.error_report(window_id) -> ErrorReport`
- `Aggregator.transition_window(window_id, target_state) -> IngestResult`
- `Aggregator.summary() -> dict`

## 窗口与水位线语义

- 固定窗口：`[floor(t/L)*L, floor(t/L)*L + L)`。
- 滑动窗口：起点为 `step` 的整数倍，事件属于所有满足 `start <= t < start + L` 的窗口；因此一个事件可同时更新多个窗口。
- 水位线：正常情况下为 `max_seen_event_time - allowed_lateness_us`，且只允许前进。
- 当 `watermark >= window.end` 时，窗口在一个 SQLite 事务内由 `open` 迁移为 `closed`。
- `flush()` 是显式流结束操作，将水位线推进到所有开放窗口的最大 `end` 并关闭它们。它不是普通心跳。
- `transition_window` 不允许 `closed -> open`，也不允许在水位线未到达窗口末尾时手工 `open -> closed`；非法迁移返回 `E_WINDOW_STATE`。

迟到事件按目标窗口分别判断：

- 所有目标窗口仍为 `open`：以补偿算法并入，返回 `OK` 且 `status=ACCEPTED_LATE`。
- 部分滑动窗口已关闭、部分仍开放：事件只进入开放窗口，关闭窗口写丢弃日志，返回 `OK` 且 `status=PARTIAL_LATE`。
- 所有目标窗口均已关闭：不进入聚合，写 `late_drop_log`，返回 `E_LATE_DROP`。

## 可复现的数值核心

浮点补偿求和本身仍可能受加法顺序影响，因此组件不只依赖“更好的求和算法”。每个窗口的成员按唯一规范键排序：

```text
(event_time_us, device_id, sequence)
```

每次成员集合变化后，按规范顺序重放 Neumaier 补偿求和，并把 `total`、`compensation`、`absolute_sum`、`count` 原子落库。这样相同已接受事件集合对应的最终值、补偿状态和误差界都逐位一致，而不只是近似相等。

误差界使用二进制64舍入单位：

```text
u = sys.float_info.epsilon / 2
gamma(k) = k*u / (1 - k*u)
```

对一个含 `n` 项、绝对量级和为 `A` 的累加器，报告保守上界：

```text
B = (gamma(n + 2) + input_relative_error/(1-input_relative_error))
    * nextafter(A, +inf) + u*|result|
```

其中 `x*x` 与 `w*x` 的输入舍入使用 `input_relative_error=u`，普通观测值和权重使用 `0`。RMS 的误差界由平方和界除以 `n` 后经开方传播，并计入最终开方舍入；加权均值使用 `wh/ws` 的分子、分母误差传播公式。所有界只由观测值量级、项数、`u` 和浮点传播规则推导，没有调参或硬编码的误差常数。

`error_report()` 使用 `Decimal` 对二进制64输入做 100 位精度参考计算，比较：

- 求和真值
- RMS 真值
- 加权均值真值

任一实测偏差超过对应上界时报告 `E_ERRBOUND`；演示会在任一窗口失败时直接以非零状态退出。

## SQLite 持久化与恢复

主要表：

- `events`：事件唯一键 `UNIQUE(device_id, event_time_us, sequence_no)`，保留首条观测值。
- `windows`：窗口状态、规范顺序重放后的所有补偿中间量、结果、误差界、关闭水位线、参考值和实测偏差。
- `window_members`：窗口与事件的多对多关系，支持滑动窗口。
- `late_drop_log`：每个关闭目标窗口的丢弃记录和当时水位线。
- `meta`：配置指纹、最大事件时间、当前水位线、schema 版本。

每次 `ingest()` 或 `flush()` 都是一个 `BEGIN IMMEDIATE` 事务；SQLite 使用 WAL、`synchronous=FULL` 和 busy timeout。关闭窗口与水位线推进在同一事务提交，崩溃不会留下“水位线已推进但窗口未关闭”的半提交状态。

数据库触发器进一步保证不可变性：

- 任何 `closed` 窗口的 `UPDATE`/`DELETE` 被拒绝。
- 向已关闭窗口插入或删除成员被拒绝。

恢复时只读取 SQLite 中的水位线和窗口状态；构造函数会重新关闭“水位线已到期但仍标记 open”的窗口。已关闭窗口的位模式、误差界和参考偏差不会重新计算或改写。

## 错误码

| 错误码 | 含义 |
| --- | --- |
| `OK` | 接受并计入至少一个开放窗口 |
| `E_WINDOW_STATE` | 非法窗口迁移、未知窗口、水位线后退 |
| `E_DUPLICATE` | 唯一键已存在；首条观测保持不变 |
| `E_LATE_DROP` | 事件的全部目标窗口均已关闭 |
| `E_WINDOW_PARAM` | 窗口长度、滑动步长、迟到阈值等配置非法 |
| `E_TIME_REGRESS` | 启用严格时间顺序后检测到事件时间回退 |
| `E_DB_CONFLICT` | SQLite 写冲突或注入的事务冲突；整次调用回滚 |
| `E_ERRBOUND` | 高精度实测偏差超过报告上界 |
| `E_INVALID_EVENT` | 事件字段类型、有限性或溢出检查失败 |

异常注入方式：

- 迟到超阈值：减小 `allowed_lateness_us`，或先推进事件时间再投递旧窗口事件。
- 非法窗口长度：例如 `AggregatorConfig(window_length_us=0)`。
- 时间回退：`strict_event_time_order=True`；默认关闭，因为正常迟到事件的时间也会小于已观测最大值。
- 数据库写冲突：`inject_db_conflict_once=True` 会在事务提交前注入冲突并回滚整次调用。

## 确定性演示

`python -m sensor_stream_agg.demo` 执行：

1. 用固定本地 LCG 生成 16 条基础事件及迟到/重复事件。
2. 对固定窗口和滑动窗口分别启动子进程，处理前 8 条后直接 `os._exit(91)`，不执行清理。
3. 父进程从 SQLite 恢复，核对关闭窗口位模式，继续处理后 8 条。
4. 演示开放窗口迟到并入、关闭窗口丢弃、重复键、非法迁移、误差报告和 `flush()`。
5. 用三种到达顺序处理同一集合，比较窗口结果与全部累加中间量的 `float.hex()`。
6. 验证 `E_WINDOW_PARAM`、`E_TIME_REGRESS`、`E_DB_CONFLICT` 和回滚后可重试。

本次环境的实际关键输出：

```text
[fixed] restore before_closed=1 after_closed=1 before_open=1 after_open=1 closed_bits_unchanged=True
[fixed] late_open code=OK status=ACCEPTED_LATE late=True ...
[fixed] late_closed code=E_LATE_DROP status=LATE_DROPPED late=True ...
[fixed] final_summary events=18 accepted=17 late=1 partial_late=0 late_dropped=1 drop_logs=1 windows=4 closed=4 open=0 errbound_failures=0
[sliding] restore before_closed=3 after_closed=3 before_open=2 after_open=2 closed_bits_unchanged=True
[sliding] late_open code=OK status=ACCEPTED_LATE late=True ...
[sliding] final_summary events=18 accepted=17 late=1 partial_late=0 late_dropped=1 drop_logs=2 windows=9 closed=9 open=0 errbound_failures=0
[order] permutations=3 events=18 windows=4 bitwise_identical=True
[anomaly] invalid_window code=E_WINDOW_PARAM
[anomaly] time_regress code=E_TIME_REGRESS
[anomaly] db_conflict code=E_DB_CONFLICT events_after_rollback=0
[anomaly] db_conflict_retry code=OK
ALL_CHECKS_PASSED
```

自动化测试共 11 项，覆盖固定/滑动乱序逐位一致性、滑动窗口部分迟到、迟到并入与关闭不可变、并发唯一键、强杀后恢复、四类异常回滚、每一步误差界非负、`E_ERRBOUND` 持久化标记以及每个关闭窗口的 `deviation <= bound`。实际测试输出保存在 `artifacts/test_run.txt`，结果为 `OK`。

## 适用范围

该实现优先保证本地正确性、可恢复性和逐位可复现性。每次窗口成员变化会按规范顺序重放该窗口成员，复杂度为 `O(k log k)`；适合单机传感器批次、边缘网关和集成验证。若需要高吞吐服务，可在保持规范化顺序和持久化协议不变的前提下，替换窗口内部数据结构或增加已排序索引缓存。
