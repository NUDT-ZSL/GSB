# SLI / SLO 错误预算工作台 — 技术说明

本工作台在本地用 Flask 提供交互查询、用 SQLite 持久化全部状态，在**可注入逻辑时钟**下完成滚动窗口切分、SLI 聚合、错误预算与燃尽率计算，并保证缓存与精确重算逐位一致、异常不污染已提交窗口、强制终止后可恢复。

## 1. 目录与模块

| 模块 | 职责 |
| --- | --- |
| `slo_workbench/models.py` | SLO / 事件 / 时钟配置的数据类与严格校验 |
| `slo_workbench/decimal_math.py` | 38 位定点 Decimal 算术、误差上界、R-7 分位数 |
| `slo_workbench/aggregation.py` | 纯函数：窗口切分、SLI、预算、燃尽、告警状态机（缓存与重算共用同一份代码） |
| `slo_workbench/storage.py` | SQLite schema、WAL、`flock` 文件锁、确定性 JSON、事务 |
| `slo_workbench/repository.py` | 仓储：版本、事件、窗口、告警、水位、迟到记录、缓存 |
| `slo_workbench/engine.py` | 时钟驱动推进、迟到判定、幂等提交、并行窗口范围、恢复 |
| `slo_workbench/query.py` | 缓存键、窗口/燃尽查询、精确重算与逐字段比对 |
| `slo_workbench/loader.py` | 摘要（SHA-256 manifest）校验、整体事务加载 |
| `slo_workbench/web.py` | Flask 查询接口 |
| `slo_workbench/cli.py` | `load/aggregate/query/resume/status/verify` 子命令 |
| `scripts/generate_samples.py` | 固定种子 LCG 确定性样本生成 |

## 2. 校验与错误码（业务要求 1、8）

非法输入在**写入前**完成解析校验，持久化阶段是单条 `BEGIN IMMEDIATE` 事务，因此任何 `E_BAD_SLO` / `E_BAD_EVENT` 都不会留下部分状态。

| 错误码 | 触发条件 | 诊断字段 |
| --- | --- | --- |
| `E_BAD_SLO` | SLI 枚举非法、目标值不在开区间 `(0,100)`、步长>窗长、重名等 | `slo_name`、`field`、`value` |
| `E_BAD_EVENT` | 序号非严格递增、时钟为负、布尔标记非 bool、未知服务 | `event_seq`、`service` |
| `E_EMPTY_WINDOW` | 窗口内合法请求数为 0 | `slo_name`、`window_no` |
| `E_CLOCK_ROLLBACK` | 注入时钟回拨超过容差 | `slo_name`、`tick_ms`、`last_committed_clock`、`tolerance_ms` |
| `E_SAMPLE_MISMATCH` | 样本 SHA-256 与 manifest 不符 | `file`、`expected_sha256`、`actual_sha256` |
| `E_CACHE_MISMATCH` | 缓存与精确重算首个差异字段 | `first_diff_field`、`cached_value`、`recomputed_value`、双侧 digest |
| `E_NOT_FOUND` / `E_STATE` / `E_BAD_INPUT` / `E_LOCK` | 查询缺失、状态冲突、入参缺失、锁冲突 | 上下文 |

单个异常事件只写入 `error_log` 并继续处理其他 SLO，不中断整体推进。

## 3. 可注入时钟与窗口归属（业务要求 2）

* 所有边界只依赖注入的逻辑时钟毫秒值，代码路径不读取任何墙钟。
* 窗口为半开步进桶：窗口 `k` 拥有满足 `k*step <= clock < (k+1)*step` 的事件，因此**每个事件唯一归属一个窗口**；窗口“接纳寿命”由 `window_length_ms` 决定。
* 到达模型（确定性水位线）：每个 tick 按事件序号顺序消费 `clock_ms <= tick` 的事件，遇到第一个未来事件即停止；流尾的旧时钟事件因此只会在晚些 tick 到达。
* 迟到判定：事件到达时其归属窗口的接纳 horizon 已过（`tick > start + window_length`），只写 `late_event`（含原因），**绝不修改已关闭窗口**，且不推进接纳水位。

## 4. 精确聚合与误差上界（业务要求 3）

* 全程使用 `Decimal(prec=38, ROUND_HALF_EVEN)`，绝不用二进制 float；数字经 `str()` 入 Decimal，序列化用规范化字符串，保证同输入同字节。
* 每次加法/除法/乘法/减法都通过 `Tracked` 记录最坏前向误差上界（单位舍入 `u = 0.5*10^(1-38)`），随结果持久化。
* 求和严格按事件序号从左到右；比率只做一次固定顺序除法。
* 延迟分位数采用 R-7（等价 NumPy `linear`）线性插值，升序排序、并列保持序号顺序，并输出分位值与是否达标。
* 预算：`总量=(1-目标)*合法数`、`已消耗=错误率*合法数`、`剩余=总量-已消耗`、`燃尽率=错误率/(1-目标)`，运算顺序固定。

## 5. 合规周期与告警状态机（业务要求 4）

* 合规周期按 `compliance_period_ms / step` 取窗口数，窗口预算按窗口号升序左到右累加，得到累计燃尽率。
* 告警状态 `ok -> firing -> ok` 完全由持久化的连续计数裁决：连续 `burn_consecutive_windows` 个窗口燃尽率超阈值触发；连续 `release_consecutive_windows` 个未超阈值解除。
* 状态、streak、迁移事件全部落库，重启后只依据持久化行重新裁决，无内存残留。

## 6. 持久化、版本与恢复（业务要求 5）

* 一个 tick 一个 `BEGIN IMMEDIATE` 事务，窗口摘要（含 SHA-256 digest）、告警状态、水位、接纳事件在同一事务原子提交，读者只能看到完整版本。
* 重新 `load` 会整体版本号 +1、旧版本 `is_current=0`、结果指针（`meta.current_slo_version`）整体切换，并清空旧缓存与旧水位，杜绝旧缓存配新水位。
* `resume` 只重放**超过持久化时钟水位**的 tick；已提交窗口靠 digest 幂等跳过，累计值由窗口行推导而非增量累加，因此预算不会重复累计。
* 强制终止用 `tests/_crash_worker.py` 以 `os._exit(137)` 模拟 SIGKILL 验证。

## 7. 缓存一致性与精确失效（业务要求 6）

* 缓存键 = `种类:SLO名:v版本:w事件水位:win窗口号`，版本或水位变化即天然换键；版本切换时还会主动清空缓存表。
* `verify=1` 时用**同一份纯函数聚合逻辑**从持久化的“已接纳事件”精确重算，深度优先逐字段比对，首个差异即返回 `E_CACHE_MISMATCH` 及两侧中间值与 digest。

## 8. 并发与幂等（业务要求 7）

* 写路径先取跨进程 `flock` 文件锁，再开 SQLite 立即事务；双保险保证同一窗口同一版本只提交一次。
* `commit_window` 按 `(slo,version,window_no)` 唯一约束 + digest 比对：相同结论重复提交幂等返回，不同结论抛 `E_STATE`。
* `process_window_range` 支持多实例并行处理不同窗口范围，测试用 3 进程重叠范围验证无重复行。

## 9. 复现实验

```bash
pip install -r requirements.txt
python scripts/generate_samples.py samples/default   # 确定性样本 + manifest
python -m pytest -q                                   # 45 个测试
```
