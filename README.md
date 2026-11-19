# Flowguard

Flowguard 是一个完全本地、确定性的限流、滑动窗口统计、熔断、降级与恢复裁决组件。它只使用本地 JSON 样本、注入的逻辑时钟和 SQLite 持久化状态，不读取系统墙钟，不访问网络，也不依赖容器环境。

核心目标不是提供一个高吞吐网关，而是让同一份事件流在任意运行次数、重启、强制终止恢复和多进程重复提交下，都得到逐位一致的裁决序列、窗口归属和摘要版本。

## 快速开始

要求 Python 3.11+、SQLite、NumPy 1.26+、pytest。

```bash
uv venv --python 3.11 .venv
.venv/bin/python -m pip install -e ".[test]"

.venv/bin/python -m flowguard load --reset
.venv/bin/python -m flowguard run
.venv/bin/python -m flowguard status
.venv/bin/python -m flowguard explain --seq 14
.venv/bin/python -m flowguard verify
```

也可以直接使用安装后的命令：

```bash
flowguard load --reset
flowguard run
flowguard verify
```

默认全部使用项目内相对路径：

- SQLite：`data/flowguard.db`
- 拓扑：`samples/topology.json`
- 事件：`samples/events.json`
- 策略：`samples/policy.json`

安装阶段可以联网获取 NumPy/pytest；运行、验证和测试阶段不需要网络。所有命令只写本地相对路径或被显式指定的本地路径。

## 目录

```text
flowguard/
  models.py     输入解析、规范化、校验、策略合并
  errors.py     稳定业务错误码
  store.py      SQLite schema、WAL、文件锁、事务、恢复队列
  stats.py      精确统计、分层摘要、分位数误差上界
  engine.py     限流、熔断、降级、恢复与决定链
  verifier.py   结构校验、摘要校验、确定性重放
  cli.py        六个命令行子命令
samples/        固定确定性样本
tests/          pytest 集成与单元测试
artifacts/      实际验收输出
```

## 输入格式

### 拓扑

```json
{
  "version": 1,
  "services": [
    {
      "name": "gateway",
      "instances": ["gateway-1", "gateway-2"],
      "priority": 10
    }
  ],
  "dependencies": [
    {"from": "gateway", "to": "payments"}
  ]
}
```

- 服务名必须非空且唯一。
- 实例列表必须是非空、元素唯一的字符串数组。
- `priority` 必须是非负整数。
- 依赖边两端必须存在于服务清单中。
- 依赖边也接受 `source`/`target` 字段名。

### 事件流

```json
{
  "version": 1,
  "events": [
    {
      "seq": 1,
      "service": "gateway",
      "clock_ms": 0,
      "result": "success",
      "latency_ms": 8.0,
      "cost_weight": 1.0
    }
  ]
}
```

- `seq` 必须为正整数，文件内严格递增。
- `clock_ms` 必须为非负整数毫秒戳。
- `result` 接受 `success`、`ok`、`error`、`failed`、`failure`，内部规范化为 `success`/`error`。
- `latency_ms` 必须为有限且非负的数值。
- `cost_weight` 必须为有限正数。
- 事件服务必须存在于拓扑中。
- 兼容别名：`event_seq`、`timestamp_ms`/`ts_ms`、`result_mark`/`outcome`、`cost`。

### 治理策略

```json
{
  "version": 1,
  "default": {
    "qps_limit": 100,
    "token_bucket_capacity": 5,
    "window_length_ms": 100,
    "window_step_ms": 20,
    "breaker_error_rate_threshold": 0.5,
    "breaker_min_requests": 3,
    "breaker_consecutive_failures": 3,
    "breaker_open_ms": 120,
    "half_open_max_probes": 2,
    "half_open_successes_required": 2,
    "degrade_level": 2,
    "degrade_allow_ratio": 0.5,
    "degrade_error_rate_threshold": 0.5,
    "degrade_stable_windows": 1,
    "retention_limit": 6,
    "clock_tolerance_ms": 3,
    "summary_bins": 16,
    "summary_quantile": 0.95,
    "mode_restore_ratio": 0.75
  },
  "services": {
    "payments": {
      "degrade_level": 2
    }
  }
}
```

`default` 提供全局策略，`services` 按服务覆盖。也可以省略 `default`，把字段直接放在顶层。校验规则包括：

- `qps_limit`、`token_bucket_capacity`、`retention_limit`、窗口长度/步长必须为正整数。
- `window_step_ms <= window_length_ms`。
- 错误率阈值在 `(0, 1]`；降级比例、模式恢复比例在 `[0, 1]`。
- 半开所需成功探测数必须在 `[1, half_open_max_probes]`。
- `summary_bins >= 4`。
- 常用别名如 `qps`、`bucket_capacity`、`window_ms`、`step_ms`、`tolerance_ms` 也可解析。

## 裁决语义

### 逻辑时钟与窗口

- 所有窗口边界、令牌补充、熔断到期和恢复判定只使用事件中的注入时钟和持久化水位。
- 新事件时钟低于水位但差值不超过 `clock_tolerance_ms` 时，使用 `effective_clock = max(watermark, event_clock)`，避免内部时间倒退。
- 回拨超过容差立即返回 `FG-2001`，事务回滚，不修改事件、令牌、窗口或熔断状态。
- 分段窗口 ID 为 `effective_clock // window_step_ms`。
- 当前滚动窗口由最近 `ceil(window_length_ms / window_step_ms)` 个分段组成。
- 每个合法放行事件只写入一个分段；分层摘要合并分段摘要时不会重复计数。

### 令牌桶

- 令牌使用百万分之一令牌整数单位，避免浮点积累误差。
- 补充量为 `floor(delta_ms * qps * 1_000_000 / 1000)`，上限为桶容量。
- 单事件成本为 `ceil(cost_weight * 1_000_000)` 单位。
- 只有最终允许的事件才消耗令牌；限流拒绝、熔断拒绝和降级拒绝不消耗令牌。
- 半开成功序列完成、熔断闭合时，令牌余额重置为满容量，并在决定链中标记 `budget_recycled=true`。

### 熔断状态机

状态为 `closed`、`open`、`half_open`。

- `closed`：窗口合法请求数达到最小样本数且错误率超过阈值，或连续失败达到阈值时，进入 `open`。
- `open`：到期时间由 `breaker_open_ms` 和注入逻辑时钟计算；到期前拒绝。
- 到期后进入 `half_open`，最多允许 `half_open_max_probes` 个探测。
- 探测失败立即重新打开；成功累计到 `half_open_successes_required` 后闭合。
- 探测事件按普通允许事件写入窗口，因此其错误和耗时精确计入后续窗口结论。
- 所有迁移依据 SQLite 中已提交的分段/窗口快照、服务状态和逻辑时钟，不依赖进程内缓存。

### 降级

- 熔断打开或窗口错误率达到 `degrade_error_rate_threshold` 时激活降级。
- `degrade_level=0`：关闭降级。
- `degrade_level=1`：按 `degrade_allow_ratio` 确定性放行。
- `degrade_level>=2`：正常阶段全拒。
- 熔断通过半开成功序列闭合后进入恢复观察期；观察期允许按比例抽样少量请求，即使原级别为全拒，避免恢复死锁。
- 比例选择使用 `sha256(service:seq:recovery_epoch)`，不依赖随机数、进程或系统时间。
- 降级解除还要求连续 `degrade_stable_windows` 个窗口错误率低于阈值；瞬时窗口波动不会立即反复切换。
- 闭合熔断和解除降级都会保留持久化恢复证据，裁决输出包含恢复成功数、稳定窗口数和恢复代数。

### 精确统计与分层摘要

规则明确、无随机性：

1. 当前窗口合法事件数 `count <= retention_limit` 且不处于模式回滞区时使用 `exact`。
2. `count > retention_limit` 时切换为 `layered`，触发依据为 `window_count_exceeds_retention_limit`。
3. 分层模式下，只有 `count <= floor(retention_limit * mode_restore_ratio)`（至少 1）才恢复 `exact`，避免阈值附近抖动。
4. 模式切换本身不会清除熔断或降级状态，治理结论不会因为算法切换而倒退。

精确模式使用 NumPy 对原始耗时可放行事件计算线性分位数，误差上界为 0。

分层模式使用固定对数桶直方图：

- 分段保存计数、错误数、成本和直方图。
- 错误率由精确计数得到，上界误差为 0。
- 分位数估计取所在桶中点，保守误差上界为桶上界减桶下界。
- 每次裁决输出 `stats_mode`、`mode_switch.trigger/basis`、`error_upper_bound`、`latency_error_upper_bound_ms`。

原始事件仍保留在 SQLite 审计/幂等表中，但分层模式不会读取原始窗口事件来计算统计；恢复精确模式时可重新使用完整事件事实。

## 持久化与恢复

SQLite 使用 WAL、`synchronous=FULL`、外键和 `BEGIN IMMEDIATE`。主要表：

- `events`：事件及水位状态。
- `service_state`：令牌、熔断、降级、统计模式、当前窗口指针、逻辑时钟水位。
- `segment_snapshots` / `segment_heads`：不可变分段版本和当前版本指针。
- `window_snapshots` / `window_heads`：不可变滚动窗口版本和当前版本指针。
- `window_events`：实际计入窗口的放行事件，主键为事件序号。
- `decisions`：完整决定链 JSON 和摘要。
- `pending_jobs`：两阶段恢复队列。
- `recovery_audit`：熔断迁移审计。

每个事件分两阶段：

1. 在事务 A 中登记事件和 `pending_job`。
2. 在事务 B 中完成裁决、窗口快照、当前指针、事件水位、令牌、熔断和降级状态、决定记录的一次性提交。

如果进程在阶段 1 后被强制终止，`pending_job` 会保留；`resume` 只补算所有尚未有决定的已装载/已暂存事件，不会重算已提交窗口。如果在阶段 2 中终止，SQLite 回滚整个事务，不会暴露半提交窗口。读取方只能通过当前指针看到完整已提交快照。

测试钩子：

```bash
flowguard run --crash-after-stage 10
flowguard resume
```

或设置 `FLOWGUARD_CRASH_AFTER_STAGE=10`。该钩子使用硬退出模拟强制终止。

### 并发与幂等

- 裁决写入使用数据库旁路文件锁 `data/flowguard.db.lock` 串行化本地多进程。
- SQLite 事务提供第二层互斥和 WAL 读一致性。
- `events.seq`、`decisions.seq`、`window_events.seq` 均为唯一键。
- 相同事件序号重复提交返回已提交决定，不重复计入窗口。
- 相同窗口版本通过唯一键只插入一次；若版本已存在但摘要不同，返回 `FG-2003`。
- 并发冲突优先保证一致性和可重复性，不追求并行吞吐。

## CLI

### `load`

```bash
flowguard load --reset
flowguard load --db data/custom.db --topology samples/topology.json \
  --events samples/events.json --policy samples/policy.json
```

全量严格校验；任何非法字段都在打开/修改数据库前失败，不写状态。同一摘要重复装载幂等；不同摘要拒绝。

### `run`

```bash
flowguard run
flowguard run --events extra-events.json
flowguard run --events extra-events.json --strict-events
```

默认处理所有待裁决事件。`--events` 默认逐条严格解析但隔离单条异常：非法事件不写状态并记录错误，其他服务继续裁决。`--strict-events` 改为整文件原子拒绝。

### `resume`

```bash
flowguard resume
```

恢复 `pending_jobs` 和尚未裁决的已装载事件，并输出恢复的暂存/装载事件数。

### `status`

```bash
flowguard status
flowguard status --service payments
```

输出服务令牌、熔断、降级、模式、当前窗口、水位、待处理任务和决定摘要。

### `explain`

```bash
flowguard explain --seq 14
flowguard explain --seq 27 --output artifacts/explain.json
```

以递归键排序 JSON 输出完整决定链。阶段固定为：

```text
logical_clock -> token_bucket -> breaker_gate -> degrade -> window_statistics -> final
```

关键字段：

- `window_id` / `window_version` / `stats_mode`
- `rate_limit.*`
- `breaker.*`
- `degrade.*`
- `window_evidence`
- `mode_switch`
- `final.decision/source/reason`
- `chain`

### `verify`

```bash
flowguard verify
flowguard verify --topology samples/topology.json \
  --events samples/events.json --policy samples/policy.json
flowguard verify --expected-decision-digest <sha256>
```

执行 SQLite 完整性、重复计数、决定/事件对应、快照存在性、令牌非负、精确模式保留上限等检查，并在本地临时数据库中完整重放，与持久化决定摘要比较。

## 业务错误码

| 错误码 | 含义 |
|---|---|
| `FG-1001` | 服务名重复 |
| `FG-1002` | 依赖边端点未闭合 |
| `FG-1003` | 事件序号未严格递增/落后水位 |
| `FG-1004` | 逻辑时钟字段非法 |
| `FG-1005` | 结果标记非法 |
| `FG-1006` | 耗时非法或为负 |
| `FG-1007` | 事件服务/成本等字段非法 |
| `FG-1008` | 拓扑结构非法 |
| `FG-1009` | 事件文档结构非法 |
| `FG-1101` | QPS 上限非法 |
| `FG-1102` | 令牌桶容量非法 |
| `FG-1103` | 窗口长度/步长非法 |
| `FG-1104` | 比例/阈值非法 |
| `FG-1105` | 其他策略字段非法 |
| `FG-2001` | 逻辑时钟回拨超过容差 |
| `FG-2002` | 窗口内合法请求数为零 |
| `FG-2003` | 样本/窗口/决定摘要不匹配 |
| `FG-2004` | 统计模式切换失败 |
| `FG-2005` | 同一序号不同事件内容 |
| `FG-2006` | 持久化状态不一致 |
| `FG-3001` | SQLite 忙 |
| `FG-3002` | 数据库/事件/服务不存在 |

CLI 退出码：

- `0`：成功。
- `2`：输入/校验错误。
- `3`：业务裁决错误或部分事件异常。
- `4`：数据库忙/一致性错误。

异常响应包含稳定 `code`、`message` 和 `fields` 诊断字段。

## 固定样本

`samples/events.json` 共 28 个事件：

- `gateway`：容量 5 的突发流量，高成本事件触发限流拒绝。
- `batch`：同窗口 7 个事件触发 `exact -> layered`；时间推进后窗口规模回落触发 `layered -> exact`。
- `payments`：3 次连续错误打开熔断；打开态拒绝；120ms 后两次半开成功探测闭合；随后进入降级恢复观察期，比例抽样两次后解除降级。

## 实际验证结果

当前工作区已执行：

```text
.venv/bin/python -m pytest -q
............................                                             [100%]
28 passed
```

固定样本完整验收：

```text
events:                       28
allow:                        24
degrade_allow:                 2
reject:                        2
exact decisions:              24
layered decisions:             4
decision_digest (run #1):     4f3baccb73af0e52cdf56532cf5f049d1662f1668fd7de4e80ef394c5c894ce9
decision_digest (run #2):     4f3baccb73af0e52cdf56532cf5f049d1662f1668fd7de4e80ef394c5c894ce9
verify replay digest:         4f3baccb73af0e52cdf56532cf5f049d1662f1668fd7de4e80ef394c5c894ce9
SQLite integrity:             ok
duplicate counted events:     0
pending after completion:     0
```

验收输出保存在 `artifacts/`：

- `load.json`
- `run.json`
- `run-second.json`
- `verify.json`
- `status.json`
- `explain-14.json`（分层摘要裁决）
- `explain-27.json`（降级解除裁决）

注意：如果修改了样本、策略或裁决 payload，摘要会相应变化；确定性验收应比较同一输入下的两次运行和重放摘要，而不是把摘要当跨版本常量。

## 测试覆盖

`tests/` 包含 28 个测试，覆盖：

- 重复服务、悬空依赖、序号错误、负时钟、非法结果、负耗时、非法 QPS/容量/窗口/比例。
- 非法 `load` 不创建数据库；重复 `load` 幂等；不同样本摘要拒绝。
- 逻辑时钟超容差回拨保持状态；容差内回拨钳制到水位。
- 令牌容量、成本、确定性补充和拒绝。
- 熔断打开、打开态拒绝、半开探测、闭合和预算重置。
- 分层摘要切换、规模回落精确恢复、误差上界、模式切换失败回滚。
- 降级放行/全拒、恢复代数、稳定窗口解除。
- 硬退出后 `resume` 与不中断运行的决定摘要一致。
- 3 个进程并发运行同一事件流，事件只计一次且重放一致。
- 异常事件按服务隔离，不影响其他服务。
- 同序号重复提交返回逐位相同的已存裁决。
- `explain` JSON 稳定排序和固定决定链。

## 范围说明

- 本组件是本地单机一致性实现；多进程并发通过文件锁串行化，不宣称分布式高吞吐。
- 逻辑时钟必须由事件流提供；运行路径不读取系统墙钟。
- 强制终止后不保证生成退出报告，但已提交决定和未完成补算任务可恢复。
- 降级级别语义固定为：`0` 关闭，`1` 按比例，`>=2` 全拒；恢复观察期例外按比例抽样。
