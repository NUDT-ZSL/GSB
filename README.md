# LockFair

本地分布式锁管理中间件的**批量授予编排与公平性裁决**组件。项目只使用 Python 3.10+ 标准库与 SQLite，不依赖第三方包、容器或网络；所有业务时间判定只使用注入的逻辑毫秒值。

核心能力：

- 每轮同时约束**总逻辑时间预算**、**持有者并发授予上限**与**公平配额最少授予数**。
- 每锁键按公平标签分组、组内按逻辑时钟与序号稳定排队。
- 在队轮数达到阈值后触发下一轮**强制让渡**，记录被对比的高优先级申请与预计避免的推迟轮数。
- 授予、租约代数推进、持有者占用、到期回收、队列事件在同一轮事务内提交。
- SQLite 快照指针 + 持久化轮次意图 + 跨平台文件锁，支持幂等重放、崩溃恢复与多进程并发。
- 提供命令行、HTTP JSON API 和本地交互式裁决面板。

## 快速开始

要求：

- Python 3.10 或以上
- 标准库 `sqlite3` 可用
- 无需 `pip install`

Linux / macOS：

```bash
python3 app.py --port 8765 --open
```

Windows PowerShell：

```powershell
py -3 app.py --port 8765 --open
```

不传 `--open` 时手动访问 `http://127.0.0.1:8765/`。`app.py` 会重置本地演示数据库 `state/demo.sqlite` 并启动面板。

也可以只重置演示状态：

```bash
python3 -m lockfair.cli demo --headless
```

## 交互面板

面板中的“提交下一轮”“连续 5 轮”“恢复”“重置”和快照切换均直接调用后端 API：

- 六项指标：轮次、逻辑时钟水位、预算利用、活跃租约、公平配额、强制让渡/错误数。
- 锁键队列：租约代数、当前持有者、到期逻辑时钟、标签分组、队列位置、在队轮数。
- 裁决轨迹：本轮授予、被推迟候选、首触发约束、强制让渡对比与避免推迟轮数。
- 租约账本与队列生命周期事件。
- 错误码诊断，保留轮次号、锁键、申请序号、约束与细节字段。
- 历史快照只读取 SQLite 已完整提交的版本，不暴露编排中间态。

## 命令行

```bash
# 纯校验，不创建数据库
python3 -m lockfair.cli validate samples/demo.json --compact

# 初始化/覆盖 SQLite 状态
python3 -m lockfair.cli init \
  --bundle samples/demo.json \
  --db state/demo.sqlite \
  --force

# 提交一轮 / 连续六轮
python3 -m lockfair.cli step --db state/demo.sqlite --request-id demo-step-1
python3 -m lockfair.cli run --db state/demo.sqlite --rounds 6

# 查看当前或历史快照、错误链、队列事件
python3 -m lockfair.cli state --db state/demo.sqlite
python3 -m lockfair.cli state --db state/demo.sqlite --round 3 --full
python3 -m lockfair.cli errors --db state/demo.sqlite
python3 -m lockfair.cli events --db state/demo.sqlite --round 3

# 恢复强杀后遗留的已准备作业
python3 -m lockfair.cli recover --db state/demo.sqlite

# 启动面板
python3 -m lockfair.cli serve --db state/demo.sqlite --port 8765
```

提交新申请与撤销/放弃：

```bash
python3 -m lockfair.cli submit \
  --db state/demo.sqlite \
  --seq 901 \
  --holder alice \
  --lock-key ledger-main \
  --arrival-ms 9000 \
  --desired-hold-ms 500 \
  --label gold \
  --priority 88

python3 -m lockfair.cli cancel --db state/demo.sqlite --seq 901 --reason operator_test
```

## 样本格式

`samples/demo.json` 包含四个部分：

```json
{
  "resources": [
    {
      "lock_key": "ledger-main",
      "lease_duration_ms": 600,
      "lease_generation": 4,
      "priority": 80,
      "max_wait_rounds": 8
    }
  ],
  "holders": [
    {
      "identity": "alice",
      "heartbeat_ms": 250,
      "session_generation": 3,
      "max_concurrent_grants": 2
    }
  ],
  "applications": [
    {
      "seq": 101,
      "holder": "alice",
      "lock_key": "ledger-main",
      "logical_clock_ms": 100,
      "desired_hold_ms": 500,
      "fairness_label": "gold",
      "priority": 90,
      "expected_lease_generation": 4
    }
  ],
  "config": {
    "round_budget_ms": 1200,
    "fair_quota_min_grants": 1,
    "starvation_rounds": 2,
    "clock_step_ms": 1000,
    "fairness_labels": ["gold", "silver", "bronze"],
    "initial_clock_ms": 0,
    "max_forced_yields_per_application": 1,
    "clock_rollback_tolerance_ms": 0
  }
}
```

字段约束：

- `lock_key`、`seq` 唯一；持有者身份必须存在；目标锁键必须存在。
- `lease_duration_ms`、`max_concurrent_grants`、`round_budget_ms`、`starvation_rounds` 为正整数。
- `fairness_label` 必须属于 `config.fairness_labels` 闭合枚举。
- `expected_lease_generation` 可选；提交时落后于持久化代数返回 `STALE_LEASE_GENERATION`，领先返回 `INVALID_LEASE_GENERATION`。
- 校验全部通过后才会创建 SQLite 文件或锁文件。

## 裁决语义

- **一轮预算**：本轮准入租约的 `min(desired_hold_ms, lease_duration_ms)` 成本之和不得超过 `round_budget_ms`。
- **轮内时间轴**：轮次从提交的 `clock_ms` 开始，在不超过 `clock_ms + round_budget_ms` 的时间轴上处理到期回收，因此同一锁键可以在同一轮内先回收旧租约再授予新租约。
- **独占锁键**：同一锁键同一时刻最多一个活跃租约；每次授予推进一代 `lease_generation`。
- **公平配额**：`fair_quota_min_grants` 是当轮应达到的最少授予数；选择时先考虑强制让渡，再优先授予本轮标签计数较少的公平组，之后按资源优先级、申请优先级、在队轮数、逻辑时钟与序号填充。
- **在队轮数**：只在轮次事务提交时推进；重启后从 SQLite 读取，不使用内存残留。
- **饥饿让渡**：申请在队轮数达到阈值后，下一个已提交轮次获得强制优先权，优先于高优先级新到申请；记录阈值、对比申请和预计避免的推迟轮数。
- **让渡上限**：`max_forced_yields_per_application` 限制单个申请被强制让渡的次数。
- **错误隔离**：某个锁键的落后代数、预算或配额诊断不会回滚其他锁键本轮已合法完成的授予。

## 错误码

| 错误码 | 场景 |
| --- | --- |
| `LOCK_NOT_FOUND` | 目标锁键不存在 |
| `HOLDER_NOT_FOUND` | 申请者不在持有者清单 |
| `STALE_LEASE_GENERATION` | 申请观察到的租约代数落后 |
| `BUDGET_CANNOT_SATISFY_ANY` | 现有候选的最小授予成本超过轮预算 |
| `FAIR_QUOTA_UNSATISFIABLE` | 公平配额不可满足或本轮未达到最少授予数 |
| `CLOCK_ROLLBACK_EXCEEDED` | 新逻辑时钟回拨超过容差 |
| `VERSION_CONFLICT` | 提交期望的持久化版本已变化 |
| `ROUND_OUT_OF_ORDER` | 跳过轮次或重复提交旧轮次但不属于幂等读取 |
| `INVALID_*` / `DUPLICATE_*` | 样本结构、枚举、数值或唯一性校验失败 |

JSON 错误始终包含 `code`、`message`、`round_no`、`lock_key`、`application_seq`、`constraint`、`details`。

## 持久化与恢复

- `round_jobs` 先持久化 `prepared` 轮次意图，再在独立事务中提交完整状态。
- 状态、规范化镜像、事件、动作、租约和 `current_snapshot` 指针在同一事务中替换。
- 强杀发生在意图提交之后时，`recover` 只重放 `prepared` 作业；已提交轮次直接返回原快照。
- `actions.idempotency_key`、`grants(round_no, seq)` 和 `grants(lock_key, lease_generation)` 提供数据库级幂等与唯一性。
- 读取端在一个读事务中同时读取快照指针与快照 JSON，因此不会看到半提交状态。

详细协议见 [TECHNICAL.md](TECHNICAL.md)。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 scripts/verification.py
```

当前实际结果：

- 单元/集成测试：`30 tests`，全部 `OK`。
- 样本 6 轮全部提交，预算未超限，持有者占用未越界。
- 强制让渡记录包含高优先级对比申请与预计避免推迟轮数。
- 落后代数样本返回 `STALE_LEASE_GENERATION`，且其他合法锁键继续授予。
- `after_prepare` 与 `mid_apply` 强杀均可恢复，恢复状态哈希与干净运行一致。
- 6 个进程同时提交同一轮：唯一状态版本 `2`、唯一快照哈希、1 个轮次行、2 个授予行、重复租约代数 `0`。
- 4 个进程并发提交不同锁键/标签的申请：版本连续 `2,3,4,5`，无丢失更新。

机器可读结果见 `artifacts/verification_report.json`。

## 项目结构

```text
lockfair/
  models.py       样本解析与零副作用校验
  engine.py       队列、预算、让渡、租约与恢复编排
  store.py        SQLite schema、快照、文件锁、查询
  web.py          本地 HTTP API
  cli.py          命令行
web/
  index.html      交互面板
  styles.css
  app.js
samples/
  demo.json       主演示样本
  stale_generation.json
tests/
scripts/
  verification.py
```

## Windows 说明

- 文件锁在 Windows 使用 `msvcrt.locking`，POSIX 使用 `fcntl.flock`。
- 多进程验证通过独立 Python 子进程和命令行入口运行，兼容 Windows 的 `spawn` 启动方式。
- 所有脚本入口均有 `if __name__ == "__main__"` 保护。
