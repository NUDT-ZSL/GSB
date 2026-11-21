# 实际验证记录

验证日期：2026-11-22

可用验证环境：Python 3.10.12、SQLite 3.37.2、NumPy 1.26.4。代码未使用 3.11 专属语法，目标运行版本仍为 Python 3.11+；所有验证不依赖容器。

## 自动化测试

命令：

```text
python -m unittest discover -s tests -v
python -m pyflakes elasticctl tests samples/generate_samples.py
```

结果：

```text
Ran 33 tests in about 9s

OK
```

`pyflakes` 无输出（无未使用导入等问题）。

连续两次运行 `python samples/generate_samples.py` 前后，7 个样本 JSON 的 SHA-256 全部相同。

覆盖项：

- 11 类非法配置/补丁输入和“非法输入不创建/不修改持久化状态”。
- 41 个逻辑时刻 × 6 个指标源的窗口聚合，固定/浮点双通路全部一致。
- 已知 avg=76.750000、p95=385.500000 的固定期望值。
- 窗口边界、逻辑时钟回拨超限和小幅回拨水位保持。
- 规则优先级、同方向遮蔽、阈值未命中、冻结窗口、夹取与两次运行一致。
- 核时、内存、每轮逻辑时间三类预算截断及 `not_started`/`abandoned` 分类。
- 连续两次失败的基础指数增长、确定性抖动和两次运行一致。
- 补丁闭包、对象链、冷却状态精确保留、未受影响工作负载逐字段不变。
- `after_reservation`、`after_start`、`before_effect_commit`、`after_effect_commit` 四种强制退出恢复，恢复前后核时相同且内存归零。
- 未确认动作占用并发名额，新轮不启动。
- 两进程不同工作负载共享预算、两进程同一工作负载同一轮次仅执行一次。
- CLI 全流程、`status` 两次输出逐字节一致、`resume`/`verify` 幂等。

## 固定样本结果

### 120000ms 决策与下发

三个动作，固定样本在 `r-search-out` 注入启动后失败：

```text
succeeded:  api=4 -> 6
succeeded:  queue=3 -> 8
failed:     search=5 -> 8（模拟供应失败，副本保持 5）
no action:  batch, cache
```

核时账目：

```text
poolCoreUnits      = 3600000
chargedCoreUnits   = 2500000
remainingCoreUnits = 1100000
memoryReservedMiB  = 0（全部动作已确认）
```

`verify` 返回 `ok=true`，快照 checksum 有效且 live-state 匹配。

### 150000ms 预算截断

在 120000ms 下发后继续 150000ms：

```text
startedSucceeded: 1（queue）
notStarted:       1（api latency, core_hours_budget_exhausted）
activelyAbandoned: 2（search, batch）
```

账目仍满足 `chargedCoreUnits <= poolCoreUnits`。

### 增量补丁

应用 `samples/rule_patches.json`：

```text
configVersion: 1 -> 2
affectedWorkloads:   wl-api, wl-queue, wl-search, wl-realtime
unaffectedWorkloads: wl-batch, wl-cache
```

`wl-batch` 与 `wl-cache` 的副本、规则状态和 `lastDecision` 在补丁前后逐字段相同。`r-queue-fast-out` 仅修改百分比，其 150000ms 冷却截止仍保留；`r-search-out` 的失败退避也保留。

### 强制终止恢复

四种故障注入均通过：

```text
after_reservation      exit 86 -> resume -> verify ok
after_start            exit 87 -> resume -> verify ok
before_effect_commit   exit 88 -> resume -> verify ok
after_effect_commit    exit 89 -> resume -> verify ok
```

以 `after_reservation` 为例，终止后核时为 2500000 units、内存预留 5376 MiB；`resume` 后核时仍为 2500000 units、内存归零，没有重复扣减。

### 多进程

- 两进程分别下发 `wl-api` 与 `wl-queue`：核时从共享账本原子扣减，账本与动作成本之和一致。
- 预算缩为 720000 units 时，两进程竞争后只成功预扣 400000 units，没有超卖。
- 两进程同时下发同一 `wl-api` 120000ms 轮次：最终副本 6、核时 400000 units，动作业务键唯一。

### 可重复性

两个全新数据库分别执行相同的 `load -> decide(120000) -> dispatch(120000)`：

```text
decide 输出完全相等
dispatch 输出完全相等
status 快照完全相等
```

