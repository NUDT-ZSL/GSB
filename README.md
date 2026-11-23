# Audit Ledger

本地审计事件落盘组件。实现由一个可集成 Python 库、一个六个子命令的 CLI、
确定性 JSON 样本和强制终止/多进程测试组成。SQLite 是唯一持久化后端，不依赖
Docker、网络、账号或外部服务。

## 功能范围

- 严格校验事件序号、事件版本、租户/类别枚举、SHA-256 载荷摘要、逻辑时钟、
  配额和策略。
- 按注入逻辑时钟切分每租户逻辑日窗口，不使用系统墙钟。
- 对每个事件输出可复现的裁决快照：已用字节、事件字节、投影字节、配额、
  剩余字节、首个触发约束和裁决摘要。
- 执行 `reject`、`archive`、`allow_overflow` 三种超窗口配额动作。
- 按确定性公式计算重试逻辑时刻、抖动和上限，支持重试耗尽进入待人工队列。
- 以轮次调度写入，同时限制每轮条数和逻辑时间预算；显式区分预算遗留、
  写入失败和主动放弃。
- 使用 SQLite 原子事务、WAL、跨平台文件锁、版本化账本快照和批次意图日志，
  保证强制终止恢复与多进程写入不重不漏。
- CLI 支持 `load`、`write`、`replay`、`resume`、`status`、`verify`。

## Windows 环境准备

使用 Windows 官方 Python 3.11 或更高版本：

```bat
python --version
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install pytest
```

`sqlite3` 是 Python 标准库，不需要额外安装。以下命令均在代码库根目录执行。
`audit\` 已预留为本地账本和输出目录。

安装依赖后也可直接运行：

```bat
run_windows_example.bat
```

## 快速开始

```bat
python -m audit_ledger load ^
  --db audit\audit.db ^
  --events samples\events.json ^
  --quotas samples\quotas.json ^
  --policy samples\policy.json

python -m audit_ledger write --db audit\audit.db

python -m audit_ledger status --db audit\audit.db

python -m audit_ledger verify ^
  --db audit\audit.db ^
  --expected samples\expected_summary.json
```

从空库重新演示前，关闭正在运行的实例并删除 `audit\audit.db`、`-wal`、`-shm`
和 `.lock` 文件。

PowerShell 可直接使用同一组命令，将每行末尾的 `^` 换成反引号。

## 样本与期望结果

样本文件：

- `samples/events.json`：11 条事件，覆盖允许、窗口拒绝、归档、单事件超限、
  允许超额和跨逻辑日窗口。
- `samples/quotas.json`：三个租户的配额版本 3。
- `samples/policy.json`：每轮最多 4 条、逻辑预算 35 ms、步进 10 ms。
- `samples/expected_summary.json`：期望事件集合摘要、逐事件结局和账本值。

样本的确定性结果：

| 事件 | 租户 | 结局 | 说明 |
|---:|---|---|---|
| 1, 2 | tenant-alpha | written | 当日已用 132 字节 |
| 3 | tenant-alpha | rejected | `AUD-QUOTA-EXCEEDED` |
| 4, 5 | tenant-beta | written | 当日已用 130 字节 |
| 6 | tenant-beta | archived | 归档 63 字节，不增加主表已用字节 |
| 7 | tenant-beta | rejected | `AUD-EVENT-TOO-LARGE` |
| 8 | tenant-gamma | written | 62 字节 |
| 9, 10 | tenant-gamma | written | `overflow_allowed`，账本累计 215 字节 |
| 11 | tenant-alpha | written | 新窗口 `day-19724` |

期望账本：

| 租户 | 窗口 | 已用字节 | 归档字节 | 溢出数 |
|---|---|---:|---:|---:|
| tenant-alpha | day-19723 | 132 | 0 | 0 |
| tenant-alpha | day-19724 | 63 | 0 | 0 |
| tenant-beta | day-19723 | 130 | 63 | 0 |
| tenant-gamma | day-19723 | 215 | 0 | 2 |

策略为每轮 35 ms、每步 10 ms，因此一轮实际启动 3 条事件。完整写入在 4 轮内
完成；每轮的 `budget_unwritten` 保留为后续轮次继续写入。

## CLI

### load

先完整解析和校验全部输入，再开启 SQLite 事务。任何输入错误都会返回业务错误码，
CLI 在创建数据库之前退出。

```bat
python -m audit_ledger load --db audit\audit.db ^
  --events samples\events.json ^
  --quotas samples\quotas.json ^
  --policy samples\policy.json
```

### write

处理数据库中的待写队列。默认持续运行轮次直到没有当前可写任务。

```bat
python -m audit_ledger write --db audit\audit.db
python -m audit_ledger write --db audit\audit.db --rounds 1
python -m audit_ledger write --db audit\audit.db --tenant tenant-alpha
python -m audit_ledger write --db audit\audit.db --abandon-seq 5
```

确定性故障注入用于演示重试：

```bat
python -m audit_ledger write --db audit\audit.db ^
  --fail-key alpha-login-0001 --fail-count 1
```

`--fail-count` 是本次命令中该幂等键被注入失败的次数。重试记录跨进程持久化。

### replay

再次提交同一批样本。已处理事件以 `duplicate` 结局返回，不新增审计事件，
也不会再次计入字节账本。

```bat
python -m audit_ledger replay --db audit\audit.db ^
  --events samples\events.json ^
  --quotas samples\quotas.json ^
  --policy samples\policy.json
```

### resume

恢复 `prepared` 状态的批次意图，并继续 pending/retry 队列。逻辑时钟可显式推进，
用于驱动未来重试时刻：

```bat
python -m audit_ledger resume --db audit\audit.db --advance-clock-ms 20
```

### status

稳定按键排序输出 JSON，包括当前账本版本、摘要、事件/逻辑时钟水位、队列状态、
每个租户每个窗口的已用/归档/剩余字节、溢出数和待人工处理数。

```bat
python -m audit_ledger status --db audit\audit.db
```

### verify

从已物化事件重建账本，逐字段比较当前账本；校验事件载荷摘要、事件字节数、
窗口归属、物化状态、账本摘要和期望摘要文件。成功时退出码为 0，不一致时退出码为 1。

```bat
python -m audit_ledger verify --db audit\audit.db
python -m audit_ledger verify --db audit\audit.db ^
  --expected samples\expected_summary.json
```

## 可集成库

完整可运行示例见 `examples/embedded_api.py`：

```bat
python examples\embedded_api.py --db audit\embedded-example.db --samples samples
```

最小调用：

```python
from audit_ledger import AuditEngine, AuditStore, load_bundle

bundle = load_bundle("samples/events.json", "samples/quotas.json", "samples/policy.json")
store = AuditStore("audit/audit.db")
store.load_bundle(bundle)
report = AuditEngine(store).write()
status = store.status()
verification = store.verify()
```

需要故障注入时传入：

```python
from audit_ledger import FaultPlan

plan = FaultPlan(fail_counts={"alpha-login-0001": 1})
report = AuditEngine(store, failure_hook=plan.hook).write()
```

## 数据与裁决语义

### 规范化与字节数

载荷摘要和事件字节数使用同一规范化形式：

```text
json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
```

UTF-8 字节长度是事件字节数，`sha256:<hex>` 是载荷摘要。系统不读取文件大小或
系统墙钟。

### 逻辑窗口

`window_ms` 默认为 `86_400_000`。窗口标识为 `day-<floor(logical_ts_ms/window_ms)>`，
窗口边界为 `start = floor(ts/window_ms) * window_ms`。所有裁决只使用事件携带的
`logical_ts_ms`。

### 裁决顺序

1. 事件字节数超过 `max_event_bytes`：`rejected`，错误码
   `AUD-EVENT-TOO-LARGE`，首个约束 `MAX_EVENT_BYTES`。
2. 已用字节加事件字节超过窗口配额：按声明动作执行。
   - `reject`：`rejected`，`AUD-QUOTA-EXCEEDED`。
   - `archive`：写入 `audit_archive`，不增加主审计表的 `used_bytes`，
     增加 `archived_bytes`。
   - `allow_overflow`：写入主审计表，裁决 `overflow_allowed`。
3. 否则：`written`。

裁决快照保存在报告的 `decision` 字段，`decision_digest` 是快照规范化 JSON 的
SHA-256，可用于逐位比较。

### 重试

第 N 次失败后的延迟：

```text
base_delay = min(retry_cap_ms, retry_base_ms * 2^(N-1))
jitter = sha256(idempotency_key + "|" + N)[0:4] % (max(1, base_delay // 4) + 1)
delay = min(retry_cap_ms, base_delay + jitter)
next_attempt_ts_ms = event.logical_ts_ms + delay
```

达到 `max_attempts` 后写入 `manual_queue`，状态 `manual`，原因
`AUD-RETRY-EXHAUSTED`。已拒绝、已归档、已放弃的事件均为终态，不会再进入重试。

### 轮次结局

- `written` / `overflow_allowed`：已提交。
- `archived`：已归档。
- `rejected`：配额或单事件限制拒绝。
- `duplicate`：幂等键或事件版本已存在。
- `write_failed`：本次尝试失败，仍有重试额度。
- `manual`：重试耗尽，待人工处理。
- `abandoned`：操作员主动放弃。
- `budget_unwritten`：本轮未启动，保留在队列；原因可为
  `LOGICAL_TIME_BUDGET` 或 `MAX_EVENTS_PER_ROUND`。

已启动事件一旦提交或失败，只会出现在 `outcomes`，不会再出现在
`budget_unwritten`。

## 持久化与恢复

- `audit_events`：主审计事件表。
- `audit_archive`：超配额归档事件表。
- `processed_events`：所有终态事件及裁决摘要，幂等键唯一。
- `tenant_ledger`：按 `ledger_version` 保存的完整账本快照。
- `ledger_versions`：版本、父版本、账本摘要、事件/逻辑时钟水位。
- `submissions` / `work_queue`：已校验输入与可恢复写入队列。
- `manual_queue`：重试耗尽事件。
- `write_intents`：批次意图日志，记录已提交组的下一个索引和最后账本版本。

`meta.current_ledger_version` 是当前账本指针。每次组提交都在同一事务中：

1. 校验当前账本摘要。
2. 校验意图中的父账本版本。
3. 追加事件/归档、终态、幂等、队列和时钟水位。
4. 插入新一代完整账本快照。
5. 原子切换账本指针并推进意图索引。

进程在事务中被强制终止时，SQLite 回滚未提交内容。进程在组提交后终止时，
意图仍为 `prepared`，`resume` 从事务中持久化的下一索引继续，不重复计入字节。

## 并发模型

写操作持有数据库旁的跨平台文件锁：

- Windows 使用 `msvcrt` 字节锁。
- POSIX 使用 `fcntl.flock`。

锁内使用 SQLite `BEGIN IMMEDIATE` 与 WAL。事件版本、幂等键和审计事件均有
数据库唯一约束作为第二道防线。多个租户写入实例可以并行启动；数据库提交被
确定性串行化，以避免同窗口并发超扣。

## 业务错误码

| 错误码 | 含义 |
|---|---|
| `AUD-EVENT-SEQ-DUPLICATE` | 事件序号重复 |
| `AUD-EVENT-SEQ-NOT-INCREASING` | 事件序号未严格递增 |
| `AUD-EVENT-TENANT-UNKNOWN` | 租户不在闭合配额声明中 |
| `AUD-EVENT-CATEGORY-UNKNOWN` | 类别不在闭合枚举中 |
| `AUD-EVENT-DIGEST-MISMATCH` | 载荷摘要不一致 |
| `AUD-EVENT-CLOCK-INVALID` | 逻辑时钟非法 |
| `AUD-EVENT-TOO-LARGE` | 超单事件字节上限 |
| `AUD-QUOTA-EXCEEDED` | 超窗口字节配额 |
| `AUD-QUOTA-VERSION-REGRESSION` | 配额配置版本回退 |
| `AUD-POLICY-VERSION-REGRESSION` | 策略配置版本回退 |
| `AUD-IDEMPOTENCY-CONFLICT` | 幂等键绑定到不同内容 |
| `AUD-CLOCK-REGRESSION` | 逻辑时钟回拨超阈值 |
| `AUD-LEDGER-DIGEST-MISMATCH` | 当前账本与提交摘要不一致 |
| `AUD-LEDGER-VERSION-CONFLICT` | 批次父账本版本变化 |
| `AUD-WRITE-FAILED` | 本次写入失败并已安排重试 |
| `AUD-RETRY-EXHAUSTED` | 重试耗尽，转入待人工处理 |
| `AUD-ABANDONED` | 操作员主动放弃 |

## 测试与验证

运行：

```bat
python -m pytest -q
```

测试覆盖：

- 样本 `load → write → replay → status → verify` 全链路。
- 拒绝、归档、允许超额、单事件超限和裁决摘要确定性。
- 幂等重放不重复计数。
- 瞬时重试成功与重试耗尽转人工。
- 每轮条数/逻辑时间预算分类及主动放弃。
- 账本重建逐字段一致与账本篡改检测。
- 子进程在第一组提交后直接 `os._exit(77)` 的恢复不重复计数。
- 三个租户进程并发写入和同幂等键双进程竞争。
- 配置回退、时钟回拨、幂等冲突错误码及租户/窗口上下文。
- 序号、租户、类别、逻辑时钟、配额/策略参数和重试延迟边界校验。

本工作区实际执行记录：

```text
$ python --version
Python 3.10.12

$ python -m pytest -q
..........                                                               [100%]
10 passed (under 2 seconds)

$ python -m audit_ledger verify --db audit/demo-run1.db \
    --expected samples/expected_summary.json
{"ok": true, "mismatches": [], ...}
```

交付代码仅使用 Python 3.10+ 已有语法，文档按任务要求给出 Windows Python 3.11+
安装与运行步骤。Linux 工作区用于自动化验证；Windows 分支的实现使用
`pathlib`、`msvcrt` 和 SQLite 标准库，不包含 POSIX 专用运行时依赖。
