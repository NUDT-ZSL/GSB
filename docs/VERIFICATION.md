# 实际验证记录

验证日期：2026-11-24（Asia/Shanghai）。

## 环境

```text
Python 3.11.18
Flask 3.1.3
NumPy 2.4.6
pytest 9.2.0
```

项目在 Linux 工作区执行；Windows 兼容分支已实现但未在 Windows 实机验证。

## 自动化测试

命令：

```bash
.venv/bin/python -m pytest
```

结果：

```text
29 passed
```

覆盖点：

- 服务/节点重复名称、非法整数、重复窗口、非法退避、非回环目标。
- 配置版本回退和同版本内容冲突不改变快照。
- 时钟超容差回拨、容差内钳制、无观测拒绝。
- 100 次换算逐位一致及换算证据字段。
- 拓扑、显存、节点并发三重装箱拒绝与幂等重复请求。
- 六轮完整生命周期、预算耗尽、拉起失败、健康失败排空、回收到新副本。
- 请求幂等键冲突和完成态幂等。
- 连接拒绝、冷启动逻辑预算超标错误码。
- 显存账本篡改拒绝且快照指针不推进。
- 多进程不同服务并发不超卖。
- 多进程同服务并发只拉起一次。
- 工作台页面、状态 API、恢复 API。
- `SIGKILL` 中断后恢复状态与无中断基线逐字段一致。
- 相同端点、相同输入重复运行时完整状态 JSON 逐字节一致。
- 同一 `operation_id` 并发重复请求会等待原请求并复用同一结果，服务尝试计数只增加一次。

## 确定性样本结果

运行六轮后出现的关键裁决：

- 第 1 轮：两个 `chat-alpha` 拉起成功，`guard-gamma` 首次拉起失败，`embed-beta` 因 600 ms 预算耗尽被主动放弃。
- 第 2 轮：`guard-gamma` 按逻辑时钟退避后重试成功；预算再次截断低优先级副本。
- 第 3 轮：`chat-alpha` 第 5 个副本因 zone-a GPU 显存不足得到 `WITHIN_BUDGET_NOT_STARTED`。
- 第 4 轮：目标下降，多个 READY 副本进入 DRAINING，DRAINING 不计入可用容量。
- 第 5 轮：排空完成副本回收，健康失败达到阈值的 `guard-gamma` 进入 DRAINING；预算不足副本进入延期列表。
- 第 6 轮：旧 `guard-gamma` 回收，新副本补齐并拉起。

上述轮次的 `recent_rounds[*].dispositions`、`budget.deferred`、`lifecycle` 和 `backoff_plan` 均持久化，并在工作台可见。

## 强制终止演练

命令：

```bash
.venv/bin/python scripts/chaos_drill.py \
  --rounds 3 \
  --fault-round 2 \
  --fault-after-dispatch 2 \
  --fast-delays \
  --run-id final-drill
```

控制面子进程在第 2 轮派发 2 个操作后收到强制终止，退出码为 `-9`（shell 显示 137）。恢复前持久化状态中：

- 轮次水位：`1`
- 在途轮次：`2`
- 在途操作数：`5`（2 个健康探测 + 3 个拉起）
- 操作状态：全部为 `RESERVED`
- 该轮已预占预算：`550 / 600 ms`

执行 `recover` 后：

```text
state_equal: true
state_revision: 3
round_watermark: 3
pending_rounds_after_recovery: 0
baseline_state_digest == recovered_state_digest
budget_recharged: false
```

实际执行中，基线状态与恢复状态的 SHA-256 状态摘要相等。具体摘要值记录在
`artifacts/chaos-drill/final-drill/report.json` 的
`baseline_state_digest` / `recovered_state_digest` 字段中。

恢复审计动作：

- 2 个健康探测通过查询持久化桩完成。
- 3 个拉起操作先查询幂等操作 ID；未发现完成记录后以同一 ID 重放 POST。
- 轮次预算保持恢复前已预占值，没有再次扣减。

详细报告：

```text
artifacts/chaos-drill/final-drill/report.json
```

## 说明

- 状态摘要相同，但物理快照 ID 可能因恢复过程多一次事务提交而不同；这是存储层版本号，不属于业务状态字段。
- 测试中的 HTTP 桩使用短墙钟延迟加速验证；业务预算、退避和恢复判定仍使用注入逻辑毫秒值。
- 演练使用临时回环端口，因此完整状态摘要会随端口变化；同一次演练内基线与恢复摘要必须相同。该次报告见 `artifacts/chaos-drill/final-drill/report.json`。
