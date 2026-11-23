# 弹性伸缩指标驱动控制面

这是一个面向 Windows 本地工作区的确定性弹性伸缩控制面。它从声明式
JSON 策略和注入逻辑时钟的指标流出发，完成窗口聚合、受控定点换算、多规则
裁决、资源预算内并行动作调度、SQLite 事务提交和强制终止恢复。

实现不依赖 Docker、消息队列、外部数据库或网络服务。除 NumPy 用于固定种子
样本生成外，运行路径只使用 Python 标准库。

## 环境与安装

目标环境：Windows 10/11、Python 3.11 及以上。

当前交付环境实际为 Python 3.10.12、NumPy 1.26.4、SQLite 3.37.2；代码
保持 3.10+ 兼容，并已在当前环境完成全部测试。

```powershell
python -m pip install numpy
python scripts/generate_samples.py --out samples
New-Item -ItemType Directory -Force out
```

`sqlite3` 是标准库；WAL、`BEGIN IMMEDIATE`、唯一索引和文件锁均无需额外
组件。Windows 上文件锁通过 `msvcrt.locking` 实现，Linux/macOS 上通过
`fcntl.flock` 实现。

## 快速开始

```powershell
python -m elastic_control --db out/control.db load --data-dir samples --reset
python -m elastic_control --db out/control.db decide --round-id r1 --at-ms 2000 --expected-version 1
python -m elastic_control --db out/control.db decide --round-id r2 --at-ms 5000 --expected-version 1
python -m elastic_control --db out/control.db decide --round-id r3 --at-ms 8000 --expected-version 1
python -m elastic_control --db out/control.db decide --round-id r4 --at-ms 9000 --expected-version 1
python -m elastic_control --db out/control.db status
python -m elastic_control --db out/control.db verify
```

一条命令完成主流程、强制终止恢复和双进程并发演示：

```powershell
python scripts/run_demo.py
```

脚本会重写 `samples/`，生成：

- `out/control.db`：四轮主演示数据库。
- `out/demo_status.json`：稳定排序的最终状态。
- `out/demo_report.json`：窗口/裁决/预算/恢复/并发验证汇总。
- `out/recovery_demo.db`：在 started 阶段强制终止后恢复的数据库。
- `out/concurrent_demo.db`：两个进程并行处理同一轮不同工作负载的数据库。

## 目录

```text
elastic_control/
  models.py       四类对象解析、校验、引用闭包与环检测
  aggregation.py  逻辑时间窗口、平均值/分位数、时钟回拨检查
  fixedpoint.py   定点余量账本、误差传播、Decimal 双通路比较
  decision.py     目标副本数、优先级/方向裁决、精度敏感标记
  scheduler.py    冻结/冷却/退避、并发上限、预算内动作计划
  graph.py        增量补丁、影响闭包和安全复用标记
  store.py        SQLite 账本、版本指针、动作阶段、恢复、verify
  engine.py       加载、决策、执行、补丁、恢复服务层
  cli.py          load/apply/decide/edit/resume/status/verify
scripts/
  generate_samples.py
  run_demo.py
samples/          确定性生成的工作负载、指标、规则、预算、采样流
out/              SQLite 账本、快照和演示报告
tests/            25 项单元与端到端测试
```

## 输入格式

### 工作负载 `workloads.json`

```json
{
  "workloads": [
    {
      "name": "api-web",
      "current_replicas": 3,
      "min_replicas": 1,
      "max_replicas": 10,
      "cpu_millicores": 250,
      "memory_mib": 128,
      "topology_domain": "zone-a"
    }
  ]
}
```

校验：名称唯一；`min_replicas <= max_replicas`；CPU 毫核和内存 MiB 为正
整数；当前副本数非负。

### 指标源 `metrics.json`

```json
{
  "metrics": [
    {
      "name": "api_cpu",
      "aggregation": "average",
      "window_ms": 1000,
      "target_value": "0.60",
      "alert_threshold": "0.80",
      "lower_threshold": "0.40",
      "scale": 1000,
      "error_budget": "0.01"
    }
  ]
}
```

- `aggregation`：`average` 或 `percentile`。
- `percentile`：0 到 100；百分位路径必须提供。
- `window_ms`、`scale`、`error_budget` 必须为正。
- `target_value`、`alert_threshold` 必须为正；`lower_threshold` 若提供必须
  小于 `alert_threshold`。
- 十进制数可使用 JSON 数字或十进制字符串。内部使用 `Decimal`，不会先经过
  IEEE-754 浮点再解析。

### 伸缩规则 `rules.json`

```json
{
  "rules": [
    {
      "name": "api-cpu-scale-up",
      "workload": "api-web",
      "metric": "api_cpu",
      "direction": "up",
      "step": 1,
      "percent": "20",
      "cooldown_ms": 3000,
      "freeze_ms": 2000,
      "priority": 100,
      "duration_ms": 1000,
      "version": 1
    }
  ]
}
```

- `direction`：`up`、`down` 或 `auto`。
- `up` 在聚合值达到 `alert_threshold` 时触发。
- `down` 在聚合值不高于 `lower_threshold`（未提供时用
  `alert_threshold`）时触发。
- `auto` 在聚合值达到告警阈值且不低于目标值时扩容；在低水位阈值或目标值
  以下缩容。
- `step` 与 `percent` 不能同时为零；冷却、冻结窗口非负。
- 初始规则版本默认为 1；补丁修改规则时版本单调递增。

### 资源预算 `budget.json`

```json
{
  "budget": {
    "pool_cpu_core_ms": 15000,
    "pool_memory_mib": 8192,
    "round_time_budget_ms": 5000,
    "per_workload_concurrency": 2,
    "float_tolerance": 0.01,
    "error_upper_bound": 1,
    "clock_rollback_tolerance_ms": 50,
    "backoff_base_ms": 1000,
    "action_duration_ms": 1000
  }
}
```

CPU 预算单位是“核 × 毫秒”。动作成本：

```text
cpu_cost = abs(delta_replicas) * cpu_millicores / 1000 * duration_ms
memory_cost = abs(delta_replicas) * memory_mib
```

内存上限是池累计消耗量，不表示瞬时并发容量。

### 采样流 `samples.json`

```json
{
  "samples": [
    {
      "sample_id": "api_cpu-0008",
      "metric": "api_cpu",
      "timestamp_ms": 2000,
      "value": 0.91
    }
  ],
  "events": [
    {
      "type": "action_outcome",
      "round_id": "r1",
      "workload": "worker",
      "success": false,
      "reason": "injected_worker_failure"
    }
  ],
  "rounds": [
    {
      "round_id": "r1",
      "at_ms": 2000,
      "workloads": ["api-web", "worker", "batch", "archive"]
    }
  ]
}
```

支持的事件：

- `action_outcome`：为指定轮次/工作负载预置启动后成功或失败结果。
- `freeze`：注入冻结截止逻辑毫秒。
- `set_failures`：注入连续失败次数和退避截止时刻。
- `clock`：保留为逻辑时钟事件；实际决策仍以 `--at-ms` 为准。

`sample_id` 冲突会被拒绝；完全相同的重复采样只计一次。逻辑时钟按指标流
到达顺序检查回拨，超过 `clock_rollback_tolerance_ms` 返回
`CLOCK_ROLLBACK`。

## 算法

### 确定性窗口聚合

窗口只由逻辑毫秒决定，边界固定锚定在 0：

```text
window_start = floor(timestamp_ms / window_ms) * window_ms
window_end   = window_start + window_ms
window       = [window_start, window_end)
```

只在 `window_end <= --at-ms` 时提交闭窗口。采样按
`(timestamp_ms, sample_id)` 排序后聚合。分位数先按
`(value, sample_id)` 排序，再使用线性插值：

```text
rank  = (n - 1) * percentile / 100
value = lower + (upper - lower) * fractional_rank
```

平均值和分位数除法均在 `Decimal` 精度 60 的局部上下文中执行，因此同一
输入在重复运行中逐位一致。

### 受控定点与误差账本

每个指标独立保存：

- 当前未满一个定点单位的余量；
- 累计误差上界；
- 下一次换算步骤号；
- 每次换算/乘法的完整审计条目。

换算公式：

```text
adjusted = input + carried_residual
units    = floor(adjusted * scale)
residual = adjusted - units / scale
```

`residual` 在下一轮同指标换算中参与。`ratio_numerator` 和
`replica_ratio` 条目累计乘法/除法传播误差。定点结果与 Decimal 高精度
结果的差超过 `float_tolerance` 时，快照和精度敏感标记给出首个分歧指标的
两通路数值与步骤编号。

### 目标副本数

固定顺序为：计算候选分数、按方向取整、再夹取到最小/最大副本数。

```text
ideal = current_replicas * aggregate / target_value
up:   score = max(ideal, current + step + ceil(current * percent / 100))
down: score = min(ideal, current - step - floor(current * percent / 100))
```

扩容使用 `ceil`，缩容使用 `floor`。多规则裁决顺序：

1. 更高 `priority`；
2. 同优先级时扩容方向优先；
3. 同方向比较候选分数；
4. 仍相同则规则名稳定排序。

每个被否决规则输出稳定的首个原因，例如 `lower_priority`、
`direction_tiebreak_scale_out_preferred`、`lower_candidate_score`。

同优先级、同方向的候选分数差或目标副本数差小于容差时，标记
`close_candidate_scores`，输出参与规则、误差上界、更高标度以及是否翻转。

### 预算内并行调度

动作排序键固定为：

```text
(-priority, direction(up=0, down=1), workload, rule_name)
```

每个候选依次检查冻结/冷却/退避、工作负载并发上限、轮次逻辑时间和累计
CPU/内存预算。预算不足后将剩余本来可执行的动作标记为 `abandoned`，不会
继续尝试更小动作，也不会超卖池预算。

动作结局严格区分：

- `not_started`：冻结、冷却、退避、并发上限或零变化导致预算内未启动。
- `failed_after_start`：动作已启动并执行完毕，但声明结果为失败。
- `abandoned`：资源或逻辑时间预算不足，动作未启动且未扣预算。
- `succeeded`：已启动并成功完成。

已启动动作不使用线程超时。执行器等待所有已启动动作完成；失败是预先声明
的业务结果，不是“终止失控线程”。

### 持久化与恢复

SQLite 表保存：

- 已提交样本水位和最近窗口；
- 各工作负载副本数、冷却/冻结/退避截止时刻；
- 池 CPU/内存累计用量和逐动作扣账单；
- 指标定点余量与误差条目；
- 动作 `reserved -> started -> running -> succeeded/failed_after_start` 阶段；
- 每轮工作负载输入哈希、决策和精度敏感记录；
- 唯一当前决策快照指针。

`load`、补丁、轮次决策、动作完成均使用事务。轮次提交和快照替换在同一事务
中完成；`current_snapshot` 指针只指向一行，旧快照在同一事务中取消当前标记。
请求版本低于已提交版本时返回 `VERSION_ROLLBACK`。

强制终止恢复：

1. `reserved` 动作没有消耗执行时间，恢复时转为 `started`。
2. `started` 动作表示启动阶段已持久化，恢复时继续执行。
3. `running` 动作在轮次文件锁保护下视为前一个进程遗留，可安全重领。
4. 预算在 `reserved` 动作创建时已入账；恢复不再次扣账。
5. 指标余量账本和轮次决策已随轮次事务提交；恢复不再次换算。
6. 成功动作的副本数和快照在同一事务提交，不会重复增加副本。

### 多进程一致性

- SQLite `BEGIN IMMEDIATE` + WAL 防止预算并发超卖。
- 决策文件锁使“读余量账本 → 计算 → 提交轮次”成为单进程临界区。
- 轮次文件锁使动作启动/重领/完成恢复对同一轮互斥。
- `(round_id, workload)` 输入哈希保证同轮同工作负载只裁决一次。
- 动作唯一键包含工作负载、轮次、规则版本、规则名和方向。
- 动作结果提交是 CAS 状态迁移；重复相同结果幂等，不同结果返回
  `ACTION_RESULT_CONFLICT`。

## 补丁格式

```json
{
  "base_version": 1,
  "operations": [
    {
      "op": "update",
      "kind": "metric",
      "name": "api_cpu",
      "value": {"target_value": 0.65}
    }
  ]
}
```

支持 `kind`：`workload`、`metric`、`rule`、`budget`；`op`：
`add`、`update`、`replace`、`delete`。预算为全局单例，不允许删除。

补丁先在内存中完整应用并校验引用闭合/无环，通过后才开启事务。影响报告逐
工作负载给出：

- 目标副本数缓存是否失效；
- 冷却/冻结计时是否失效；
- 预算预扣/预算纪元是否失效；
- 定点精度账本是否失效；
- 样本和样本水位是否可复用；
- 未被影响的工作负载列表。

补丁事务只更新受影响工作负载行。未受影响工作负载的结果、依据、缓存和计时
逐字段保持不变。

## 命令行

```text
python -m elastic_control [--db out/control.db] <command>
```

| 命令 | 用途 |
| --- | --- |
| `load` | 校验并装入策略、采样和事件；`--reset` 替换已有数据库 |
| `apply` | 提交补丁；`--dry-run` 仅输出影响报告 |
| `edit` | 以补丁方式编辑；默认提交，`--dry-run` 预览 |
| `decide` | 提交一个逻辑轮次并执行动作 |
| `resume` | 恢复 reserved/started/running 动作 |
| `status` | 输出稳定排序 JSON 快照 |
| `verify` | 校验账本、预算、快照、窗口和精度标记 |

常用选项：

```text
decide --round-id r1 --at-ms 2000 --expected-version 1
decide --workload api-web --fail-workload worker
decide --crash-after-start       # 演示/测试：提交 started 后以退出码 97 终止
resume
apply --patch patch.json --expected-version 1
verify
```

业务错误输出到 stderr，格式固定：

```json
{
  "ok": false,
  "error_code": "VERSION_ROLLBACK",
  "message": "requested policy version is older than the committed version",
  "details": {}
}
```

主要错误码包括：`DUPLICATE_NAME`、`INVALID_REPLICA_RANGE`、
`INVALID_AGGREGATION`、`INVALID_PERCENTILE`、`INVALID_THRESHOLD`、
`INVALID_WINDOW`、`INVALID_SCALE`、`INVALID_ERROR_BUDGET`、
`MISSING_REFERENCE`、`DEPENDENCY_CYCLE`、`CLOCK_ROLLBACK`、
`SAMPLE_BEFORE_WATERMARK`、`VERSION_ROLLBACK`、`VERSION_CONFLICT`、
`ROUND_CONFLICT`、`ACTION_RESULT_CONFLICT`、`VERIFY_MISMATCH`。

## 确定性样本

固定 NumPy 种子 `20261124`，生成 5 个指标、200 个采样、4 个工作负载和
7 条规则。主演示结果：

| 轮次 | 逻辑毫秒 | 关键结局 |
| --- | ---: | --- |
| `r1` | 2000 | api 扩容成功；worker 启动后失败；batch 缩容成功；archive 因 CPU 预算放弃 |
| `r2` | 5000 | api 冷却未启动；worker 重试成功；batch 扩容被冻结窗口阻止；archive 再次放弃 |
| `r3` | 8000 | batch 缩容成功并设置新冻结窗口；archive 放弃 |
| `r4` | 9000 | api/worker 扩容成功；batch 扩容仍处于冻结窗口；archive 放弃 |

最终累计 CPU 消耗为 `14250` 核毫秒，池上限 `15000`。最终副本数：

```text
api-web=8  worker=8  batch=3  archive=2
```

`r1` 还包含 `api-cpu-scale-up` 与 `api-latency-scale-up` 同优先级、
同方向的候选分数相等，因此产生 `close_candidate_scores` 精度敏感标记。

## 实际验证结果

当前环境执行：

```powershell
python -m unittest discover -v
python scripts/run_demo.py
```

结果：

- 25 项测试全部通过。
- 四轮主样本 `verify.ok=true`，无账本、预算、副本或窗口不一致。
- 强制终止子进程退出码为 97；恢复前后预算均为 `7500`，动作只成功结算一次，
  第二次 `resume` 无待处理轮次。
- 两个进程同时提交同一轮不同工作负载，最终预算 `7500`、动作 4 条、
  `verify.ok=true`，无超卖或重复动作。
- 补丁验证中，仅 `api-web` 的缓存/精度账本失效；`worker`、`batch`、
  `archive` 快照字段逐字段一致。

## 设计边界

- 动作执行器模拟副本拉起，不调用真实云平台；成功/失败来自样本事件、
  `--fail-workload` 或默认成功。
- 池预算是生命周期累计上限，不自动按时间回补。
- `status` 的快照动作列表聚焦当前轮次和所有在途动作；历史完整动作仍保存在
  SQLite `actions` 表中。
- 样本在 `load` 时一次性装入。若需在线追加，应先扩展入库路径并在水位检查
  下拒绝过期窗口采样；当前实现明确拒绝重复计数。
