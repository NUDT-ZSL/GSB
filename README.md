# Elasticctl

本地、确定性、指标驱动的弹性伸缩控制面。项目使用 Python 标准库 `sqlite3` 持久化声明式策略、采样水位、预算账目、冷却/退避截止时刻、在途动作和决策快照；动作执行器是本地确定性模拟器，不访问网络或外部目标。

## 快速开始

目标运行环境为 Windows 10/11 或 Windows Server、Python 3.11+、NumPy 1.26+。SQLite 使用 Python 标准库，无需数据库服务。

```powershell
python -m pip install -r requirements.txt
python -m elasticctl load --db .\var\control.db --replace
python -m elasticctl decide --db .\var\control.db --at 120000
python -m elasticctl dispatch --db .\var\control.db --at 120000
python -m elasticctl apply --db .\var\control.db
python -m elasticctl status --db .\var\control.db
python -m elasticctl verify --db .\var\control.db
```

Linux/macOS 使用 `python3` 即可。所有成功结果都是稳定排序 JSON；业务失败输出 `{"ok": false, "error": {"code": ...}}` 并返回退出码 2，`verify` 不一致返回 1。

## 命令

| 命令 | 作用 |
| --- | --- |
| `load` | 先完整校验固定 JSON 样本，再原子写入 SQLite；非法输入不会创建或修改持久化状态 |
| `apply` | 原子提交对象新增/修改/删除补丁，计算受影响工作负载闭包和精确失效集合 |
| `decide` | 按注入逻辑时钟聚合指标，计算目标副本数、规则优先级裁决和被否决原因 |
| `dispatch` | 在核时、内存、每轮逻辑时间、每工作负载并发上限下预留预算并并行执行动作 |
| `resume` | 只重试持久化状态中的 `reserved`/`started` 未确认动作 |
| `status` | 从当前决策快照输出稳定排序 JSON：副本数、预算余额、冷却/退避截止时刻、在途动作 |
| `verify` | 校验 SQLite、快照指针/校验和、预算账目、副本账本和动作效果一致性 |

`decide`、`dispatch` 可选 `--workload <id>`（可重复）把一轮限制到指定工作负载，便于多进程并行处理不同工作负载。`--at` 是唯一进入聚合窗口的逻辑毫秒值，不读取系统墙钟。

## 固定样本

`samples/` 下的文件全部由 `samples/generate_samples.py` 本地确定性生成：

- `workloads.json`：5 个工作负载，含副本范围、每副本 CPU/内存和拓扑域。
- `metric_sources.json`：平均值与分位数指标源。
- `scaling_rules.json`：12 条全局唯一优先级的扩容/缩容规则。
- `resource_budget.json`：池核时、内存上限、每轮时间预算、并发上限、冻结窗口和退避参数。
- `metric_stream.json`：0–600000ms、每 15000ms 一点的 246 条确定性采样。
- `rule_patches.json`：修改指标/规则、删除规则、新增工作负载/指标/规则的增量补丁。
- `action_outcomes.json`：本地确定性动作结果注入；样本在 120000ms 模拟一次启动后失败。

重新生成：

```powershell
python .\samples\generate_samples.py
```

## 强制终止恢复演示

动作下发和账目提交的故障注入点通过 `ELASTICCTL_FAULT` 控制：

- `after_reservation`：预算已预扣、动作 `reserved`，尚未下发。
- `after_start`：动作已标记 `started`，结果尚未提交。
- `before_effect_commit`：结果事务提交前。
- `after_effect_commit`：结果事务提交后。

PowerShell 示例：

```powershell
$env:ELASTICCTL_FAULT = "after_reservation"
python -m elasticctl dispatch --db .\var\recover.db --at 120000
# 进程以 86 退出
Remove-Item Env:ELASTICCTL_FAULT
python -m elasticctl resume --db .\var\recover.db
python -m elasticctl verify --db .\var\recover.db
```

恢复只完成持久化动作 ID，不重新扣减核时或重复预留内存；最终账目与正常执行一致。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试覆盖校验失败零写入、定点/浮点聚合、时钟回拨、规则裁决、冻结、四重预算约束、指数退避、补丁闭包、未受影响对象逐字段保持、四种强制退出恢复、多进程共享预算、同工作负载幂等和 CLI 稳定输出。

可选静态检查：`python -m pip install pyflakes` 后运行 `python -m pyflakes elasticctl tests samples\generate_samples.py`。

详细设计语义、账目公式、状态机与实测结果见 `docs/TECHNICAL.md` 和 `docs/VALIDATION.md`。

