# 本地顺序投递网关

这是一个不依赖外部服务或 Docker 的确定性消息顺序投递网关。项目同时提供 CLI、
FastAPI 控制面、SQLite 数据面、版本化事务账本和独立进程强杀恢复验证。

实现重点：

- 稳定分区路由与 0 基连续偏移。
- 同分区最多一个在途投递，严格按前缀确认。
- 多分区线程并发、全局在途额度、单轮逻辑时间预算。
- 确定性失败注入、代数演进、抖动退避、重试耗尽死信。
- 追加、投递、确认均通过 SQLite 事务原子提交。
- 每轮账本快照整体写入新版本并切换单行指针。
- 外部进程强杀后的恢复结果与不中断基线使用同一逻辑状态指纹。

## 目录

```text
gateway/
  api.py          FastAPI 控制面
  cli.py          load/produce/deliver/resume/status/verify/confirm
  engine.py       调度、追加、投递、确认、恢复、verify
  errors.py       业务错误码
  ledger.py       账本状态、重试、退避、空隙裁决
  routing.py      稳定分区路由
  scheduling.py   自适应锁粒度裁决
  storage.py      SQLite schema、版本指针、文件锁
  validation.py   四类 JSON 样本解析和全量预检
samples/          合法样本、紧预算样本、非法样本
scripts/
  kill_recovery_demo.py
tests/
```

## 环境安装

目标环境为 Windows 10 及以上、Python 3.11。项目代码同时兼容当前验证环境
Python 3.10。

```bash
python -m pip install -r requirements.txt
```

`requirements.txt` 固定为：

```text
fastapi==0.110.0
uvicorn==0.27.1
numpy==1.26.4
pytest==8.0.0
```

不需要 Docker、消息中间件、外部数据库或网络访问。

## 快速验收

完整流程也可以一键执行：

```bash
python scripts/run_acceptance.py --output artifacts/acceptance
```

脚本会依次执行加载、三个订阅者追平、紧预算裁决、`status` 和 `verify`，并将完整
JSON 报告写到 `artifacts/acceptance/acceptance-report.json`。

### 1. 加载合法样本

```bash
python -m gateway.cli --db data/gateway.db load \
  --topics samples/topics.json \
  --events samples/events.json \
  --subscriptions samples/subscriptions.json \
  --config samples/config.json
```

加载会在一个事务内写入主题、订阅、配置、初始账本、24 条事件和分区日志摘要。
重复加载相同样本是幂等的。

### 2. 并发追平

每个订阅者可运行多轮；每轮受 `catchup_logical_time_budget_ms` 和
`global_concurrency_limit` 约束。

```bash
python -m gateway.cli --db data/gateway.db deliver \
  --subscriber sub-orders-a --round-id orders-a-1

python -m gateway.cli --db data/gateway.db deliver \
  --subscriber sub-orders-b --round-id orders-b-1

python -m gateway.cli --db data/gateway.db deliver \
  --subscriber sub-audit --round-id audit-1
```

达到追赶点后再次调用同订阅者会返回
`NOT_STARTED_IN_BUDGET / no unresolved offsets`，不会重复投递。

### 3. 状态和一致性校验

```bash
python -m gateway.cli --db data/gateway.db status
python -m gateway.cli --db data/gateway.db verify
```

`status` 输出 `sort_keys=True` 的稳定 JSON，分区按 `partition_id` 排序。
每个分区包含偏移水位、可投递偏移、连续确认水位、死信偏移、在途登记、
逻辑时钟水位、自适应锁模式和空隙解释。`verify` 校验：

- 快照和 SQLite 分区日志偏移连续、无跳号。
- 事件序号和事件 ID 唯一。
- 分区键重新路由结果与日志所在分区一致。
- 同一订阅者未确认前缀之后没有投递越过。
- 每个分区最多一个在途投递，全局在途计数准确。
- 同一事件同一代数只有一条投递记录。
- 订阅者幂等键最多累计一次实际投递。
- 账本快照校验和与版本指针一致。

### 4. 紧预算主动放弃

```bash
python -m gateway.cli --db data/tight.db load \
  --topics samples/topics.json \
  --events samples/events.json \
  --subscriptions samples/subscriptions.json \
  --config samples/config_tight.json

python -m gateway.cli --db data/tight.db deliver \
  --subscriber sub-orders-a --round-id tight-1
```

`config_tight.json` 的单轮预算为 30ms。正式样本的分区启动估算为 126ms，
因此四个未启动分区均返回 `ABANDONED_BUDGET`，并保留
`remaining_budget_ms` 和 `estimated_partition_cost_ms` 诊断字段。

### 5. 外部进程强杀恢复

```bash
python scripts/kill_recovery_demo.py --scenario all \
  --output artifacts/recovery-demo --timeout-seconds 20
```

脚本本身不修改被测源码。它：

1. 启动一个独立子进程执行 `deliver` 或 `produce`。
2. 子进程在确定性故障点写入标记并暂停。
3. 父进程观察到标记后调用进程强杀。
4. 父进程执行 `resume` 或重放 `produce`。
5. 将恢复后的账本逻辑指纹与不中断基线比较，并执行 `verify`。

可选故障点：

- `append_before_commit`
- `deliver_after_reserve`
- `deliver_before_commit`
- `deliver_after_commit`

这些故障点只由环境变量 `GATEWAY_FAULT_POINT` 激活，正常运行路径不依赖故障注入。

## 样本格式

### 主题分区清单

```json
{
  "topics": [
    {
      "name": "orders",
      "partition_count": 4,
      "partition_key_rule": "sha256_mod_v1",
      "backlog_limit": 64,
      "out_of_order_tolerance_window": 2
    }
  ]
}
```

### 生产者事件

```json
{
  "events": [
    {
      "event_seq": 1,
      "event_id": "evt-1",
      "topic": "orders",
      "partition_key": "order-1001",
      "payload_digest": "sha256:...",
      "injected_clock_ms": 10,
      "expected_delivery_generation": 1
    }
  ]
}
```

只有一个主题时 `topic` 可省略。多主题场景可在事件文档顶层提供
`default_topic`，也支持每条事件显式给出 `topic`。

### 订阅声明

```json
{
  "subscriptions": [
    {
      "subscriber_id": "sub-orders-a",
      "topic": "orders",
      "pull_batch_size": 6,
      "consume_rate_per_sec": 50,
      "failure_rate": 0.18,
      "retry_budget": 3
    }
  ]
}
```

### 网关配置

```json
{
  "partition_lock_granularity": "partition",
  "catchup_logical_time_budget_ms": 1200,
  "global_concurrency_limit": 3,
  "retry_backoff_base_ms": 10,
  "retry_backoff_max_ms": 60,
  "fixed_scale": 1000,
  "clock_tolerance_ms": 5,
  "deterministic_seed": 20261124,
  "adaptive_lock_enabled": true,
  "lock_switch_backlog_threshold": 4,
  "lock_switch_inflight_threshold": 2,
  "segment_size": 3,
  "lock_cost_ms": 1
}
```

非法样本位于 `samples/invalid/`，覆盖重复主题、零分区、负逻辑时钟、零期望代数、
重复订阅者 ID、错误退避上下界和零定点标度。校验发生在数据库连接前；结构非法时
返回 `E_BAD_SAMPLE` 且不会创建数据库文件。

## 核心语义

### 路由和偏移

- 路由输入为 `topic + NFKC 规范化后的 partition_key`。
- `sha256_mod_v1` 使用 SHA-256 前 64 位对分区数取模；也可配置 `fnv1a32`。
- 每个分区偏移从 0 开始，`next_offset` 表示下一追加偏移。
- 追加在 `BEGIN IMMEDIATE` 事务中完成：同一事务写入分区日志、偏移、摘要、
  期望代数、逻辑时钟和完整账本快照。
- `partition_log` 与快照内 `partitions[*].log` 互为校验；快照日志摘要是投递裁决的
  逻辑真值，SQLite 日志用于持久化和逐字段一致性校验。

### 顺序锁、并发和预算

- 每个分区有文件锁门闩，SQLite 事务再做最终状态互斥。
- 分区内始终只有一个在途投递；后续偏移只有在当前头偏移终态后才可能启动。
- 不同分区通过线程池并发推进，线程数不超过 `global_concurrency_limit`。
- 全局在途额度在 SQLite 写事务中检查并递增，多个进程不会超卖。
- 单轮调度首先持久化预算和分区起跑计划。估算成本不足时，剩余未启动分区原子标记为
  `ABANDONED_BUDGET`；崩溃恢复复用同一持久化计划。
- 已启动分区会完成其本轮剩余批次，即使逻辑成本超过预留预算；结果单独报告
  `budget_overshoot_ms`，不做事后超时标注。
- 结局明确区分：
  - `STARTED_COMPLETED`
  - `STARTED_FAILED`
  - `NOT_STARTED_IN_BUDGET`
  - `ABANDONED_BUDGET`

### 重试和死信

当前投递代数为 `g`，失败后的下一次逻辑时刻为：

```text
jitter = deterministic(seed, subscriber, event, g) in [0, base]
delay  = min(cap, base * 2^(g-1) + jitter)
next_clock = current_clock + delay
```

允许的总代数上限为 `retry_budget + 1`。达到上限仍失败时写入死信墓碑，并记录
`retry_budget_exhausted:<failure_reason>:generation=...:limit=...`。
正式样本中 `evt-orders-0007` 期望第 5 代成功，但订阅者
`sub-orders-a` 的总代数上限为 4，因此确定性进入死信。

### 幂等

- 投递记录唯一键为 `(subscriber_id, partition_id, event_offset, generation)`。
- 订阅者幂等键为 `sha256(subscriber_id, event_id)`；重复投递只增加
  `duplicate_count`，`actual_count` 始终为 1。
- 同一 `round_id` 重放直接返回已提交结果，不重新投递。
- 已确认或死信的确认请求不改变代数和偏移；跨订阅者确认返回
  `E_SUBSCRIBER_MISMATCH` 且无副作用。

### 自适应锁模式

`scheduling.py` 根据积压深度、单轮计划并发数（不超过全局额度）和注入逻辑时钟
水位裁决锁模式。计划并发数由有序分区计划预计算，避免线程完成顺序影响演示结果：

- 低于阈值：`partition` 模式。
- 达到积压或在途阈值：`segment` 模式，按头偏移所在分段缩短实际加锁范围。
- 分区门闩和 SQLite 头偏移校验始终保留，因此分段模式不会放宽
  “同分区最多一个在途”的顺序约束。

每次决策输出 `actual_mode`、`trigger`、`rejected_mode`、否决原因和切换前后一致性结论。

## 错误码

| 错误码 | 场景 |
| --- | --- |
| `E_BAD_SAMPLE` | JSON 或字段结构、唯一性、正整数/区间校验失败 |
| `E_BACKLOG_LIMIT` | 分区积压达到上限后继续追加 |
| `E_SUBSCRIBER_NOT_FOUND` | 投递引用未知订阅者 |
| `E_EVENT_NOT_FOUND` | 投递引用不存在的事件或分区日志偏移 |
| `E_DELIVERY_NOT_FOUND` | 确认引用不存在的投递 |
| `E_CLOCK_REGRESSION` | 注入逻辑时钟回拨超过容差 |
| `E_SUBSCRIBER_MISMATCH` | 跨订阅者确认 |
| `E_EVENT_CONFLICT` | 相同事件序号/ID 对应不同内容 |
| `E_SEQUENCE_VIOLATION` | 顺序、前缀、代数或在途状态不变量被破坏 |
| `E_DB_CONFLICT` | 数据库元数据与待加载样本不一致 |

`GatewayError.to_dict()` 始终包含 `code`、`message` 和可诊断的 `details`。

## SQLite 账本

主要表：

| 表 | 用途 |
| --- | --- |
| `topics` / `subscriptions` / `gateway_config` | 已校验元数据 |
| `partition_log` | 分区追加真值，唯一 `(partition_id, offset_no)` |
| `ledger_versions` | 完整 JSON 快照和 SHA-256 校验和 |
| `ledger_pointer` | 单行指针，原子切换到最新快照版本 |
| `rounds` | 已提交轮次结果，支持同轮幂等重放 |
| `round_state` / `round_partitions` | 持久化预算、起跑计划和批次进度 |
| `deliveries` | 每代投递唯一记录 |
| `subscriber_inbox` | 订阅者幂等键和实际投递计数 |

追加、预约、成功确认、失败退避、死信和轮次提交都各自在单个事务内写入状态并切换
账本指针。进程在事务中被强杀时 SQLite 自动回滚未提交部分；已提交预约通过
`resume` 恢复，原有代数不变，重试复用同一幂等键。

逻辑状态指纹包含分区日志、投递代数、确认/死信/时钟水位、轮次计数和订阅者状态，
排除物理账本版本号与已清理的自适应观测元数据，用于比较强杀恢复与不中断基线。

## FastAPI

```bash
uvicorn gateway.api:app --host 127.0.0.1 --port 8000
```

数据库路径由 `GATEWAY_DB` 指定，默认 `data/gateway.db`。接口：

- `POST /load`
- `POST /produce`
- `POST /deliver`
- `POST /resume`
- `POST /confirm`
- `GET /status`
- `GET /verify`
- `GET /health`

## 实际验证结果

验证环境：

```text
Python 3.10.12
fastapi 0.115.6 (运行环境已装版本)
uvicorn 0.32.1
numpy 1.26.4
pytest 8.3.3
SQLite (Python 标准库)
```

当前自动化验证工作区运行在 Linux/WSL 兼容环境；Windows 专属文件锁和进程存活
检查分支使用 `msvcrt` / `OpenProcess`，目标 Python 3.11 语法与依赖保持兼容。

全量测试：

```text
python -m pytest -q
26 passed, 1 warning in 3.93s
```

正式样本连续三轮 `sub-orders-a`、六轮 `sub-orders-b`、两轮 `sub-audit` 后：

```text
loaded events: 24
topics: 2
partitions: 6
subscriptions: 3
sub-orders-a delivery attempts: 27, dead letters: 1
sub-orders-b delivery attempts: 28, dead letters: 1
sub-audit delivery attempts: 7, dead letters: 0
verify: true, 83 checks, 0 failures
ledger version: 155
```

最终分区水位：

```text
audit/0  offset=4 committed=3 acked=3 dead=[] clock=444
audit/1  offset=2 committed=1 acked=1 dead=[] clock=378
orders/0 offset=8 committed=7 acked=7 dead=[] clock=708
orders/1 offset=4 committed=3 acked=3 dead=[] clock=445
orders/2 offset=2 committed=1 acked=1 dead=[] clock=226
orders/3 offset=4 committed=3 acked=1 dead=[2] clock=497
```

紧预算样本：

```text
abandoned_budget: 4
delivery_attempts: 0
reason: remaining_budget_ms=30 < estimated_partition_cost_ms=126
```

外部强杀恢复 `--scenario all`：

```text
append_before_commit:       process_killed=true, fingerprints_equal=true, verify=true
deliver_after_reserve:      process_killed=true, fingerprints_equal=true, verify=true
baseline delivery summary:  attempts=20, acked=11, failed=9, dead=1
resume delivery summary:    attempts=20, acked=11, failed=9, dead=1
```

恢复时被强杀进程已提交的预约被识别，投递代数保持不变，没有重复累计代数或重复确认偏移。

## Windows 注意事项

- `storage.FileLock` 在 POSIX 使用 `fcntl`，Windows 使用 `msvcrt`。
- 进程存活检查在 Windows 使用 `OpenProcess`，避免误清理活跃实例的在途登记。
- 强杀脚本使用 `Popen.kill()`，Windows 对应终止进程，不依赖 Unix 信号。
- 建议在项目根目录执行命令，保持 `samples/` 和 `data/` 相对路径。
