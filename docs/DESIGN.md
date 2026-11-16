# 技术设计说明

## 1. 分层

```text
main.py / cli.py / gui.py
        |
        +-- service.py
        +-- snapshot.py <---- replay.py
        |
        +-- scheduler.py ----> locks.py
        |
        +-- script.py -------> storage.py (SQLite)
```

- `script.py`：JSON 结构、键存在性、隔离级别、终止操作和操作序列校验。
- `storage.py`：SQLite schema、初始 SQL、提交原子性、全局/行版本、状态整替。
- `locks.py`：行锁持有表、FIFO 等待队列、S→X 升级、等待图和死锁 SCC。
- `scheduler.py`：并发槽位、逻辑时钟、等待预算、事件轨迹、提交与回滚。
- `snapshot.py`：自包含快照、原子写、整体替换恢复与版本差异报告。
- `replay.py`：从快照初始状态重跑，逐会话/逐事件比较。
- `gui.py`：后台执行 + 主线程事件队列刷新。

## 2. 数据模型

SQLite 表：

```sql
lab_rows(row_key TEXT PRIMARY KEY,
         payload TEXT NOT NULL,
         row_version INTEGER NOT NULL,
         updated_by TEXT)

lab_meta(meta_key TEXT PRIMARY KEY,
         meta_value TEXT NOT NULL)  -- data_version
```

`payload` 为规范化 JSON。提交写集合时，在一个 SQLite `BEGIN IMMEDIATE` 事务内完成所有行更新和版本更新；任何异常整体回滚。

行内容摘要只包含排序后的 `row_key -> payload`，不包含行版本与实际时间：

```text
sha256(canonical_json({key: payload, ...}))
```

调度器在内存中保存每个已提交事务的写集合，按提交顺序重放到初始行状态，得到期望摘要和期望版本；再与 SQLite 最终状态比较。

## 3. 脚本校验不变量

在创建任何 `SessionRuntime` 前，以下不变量必须全部成立：

1. `script_version == 1`。
2. 会话非空、会话 ID 非空且唯一。
3. 隔离级别属于 `read_committed`、`repeatable_read`。
4. 操作名属于 `read`、`write`、`commit`、`rollback`。
5. 读/写操作有非空 `key`，且键存在于当前 SQLite 行集合。
6. 写操作存在 `value` 字段。
7. 终止操作之后没有操作。
8. 最后一个操作是终止操作。

失败抛出 `ScriptError(E_SCRIPT)`，调用方不会启动调度器，因此不会产生部分写入。

## 4. 锁规则

### 4.1 持有与兼容

- 行键粒度：`S` 或 `X`。
- `S` 与 `S` 兼容；`X` 与任何其他持有者不兼容。
- 同一事务重复申请已持有的 `S`/`X` 为幂等。
- 持有 `S` 的事务申请 `X` 是升级请求；等待期间保留原 `S`。
- 队列非空时，新请求不能直接插队获得锁。

### 4.2 等待队列

每个行维护有序等待者列表。请求被拒时追加到队尾；位置从 1 开始。授予条件为：

1. 自己位于队首；
2. 请求模式与所有其他持有者兼容。

该规则保证 FIFO 和可观测的稳定顺序。

### 4.3 生命周期

- 所有 `X` 锁持有至 `commit` / `rollback`。
- REPEATABLE READ 的 `S` 锁持有至事务结束。
- READ COMMITTED 的 `S` 锁在单次读操作结束释放，这是该隔离级别允许不可重复读的必要条件；写锁仍只从事务结束释放。
- 事务结束时同时移除该事务的所有持有锁和队列项。调度器之后按轮转顺序重试等待者，使每次授予都产生事件。

## 5. 调度算法

状态：

- `pending`：尚未占用并发槽位；
- `running`：可执行下一个操作；
- `waiting`：当前操作已排队等待锁；
- `finished`：已提交或已回滚。

逻辑：

```text
clock = 0
while pending or active:
    start pending sessions while active_count < max_concurrency
    runtime = next unfinished session in round-robin queue
    if runtime.running:
        execute one operation or enqueue a lock request
    else if runtime.waiting:
        if clock - wait_started >= budget:
            rollback(E_LOCKTIMEOUT)
        else:
            retry lock; execute data operation if granted
    detect and resolve every current deadlock ring
    requeue runtime if still active
    clock += tick_ms
```

等待中的事务仍占有一个并发槽位；超时/死锁回滚后立即移除并允许下一个 `pending` 会话启动。逻辑时钟保证等待预算和事件顺序可复现。

## 6. 死锁检测

等待图节点为事务。对每个排队请求添加：

- 指向所有不兼容持有者的边；
- 非队首请求指向直接前驱等待者的边（FIFO 阻塞关系）。

通过 Tarjan 强连通分量找环。一个环内按 `session_id` 字典序选择最小者为牺牲事务。事件 `deadlock_detected` 写入：

- `rule=lowest_session_id`；
- `cycle_members`；
- `cycle_transactions`（含开始序号）；
- `victim_session_id` / `victim_tx_id`；
- `wait_graph`。

释放牺牲者锁后继续检测，直到没有环。

## 7. 隔离语义

- READ COMMITTED 每次读直接读取 SQLite 当前已提交值；共享锁仅在语句期间存在。
- REPEATABLE READ 首次读取缓存 `(payload, value_version)`；重复读取返回缓存，来源标记为 `repeatable_read_cache`。
- 事务读取自己尚未提交的写时优先返回 `own_write`。
- 提交写集合原子落地。只读提交不增加数据版本。

## 8. 事件模型

每个事件包含：

```json
{
  "seq": 1,
  "ts_ms": 0,
  "timestamp": "2026-11-16T16:26:56.255+00:00",
  "type": "transaction_begin",
  "session_id": "n1",
  "tx_id": "tx-001"
}
```

主要事件：

- `transaction_begin`
- `lock_acquired` / `lock_wait` / `lock_timeout` / `lock_released`
- `read` / `write`
- `deadlock_detected`
- `transaction_commit` / `transaction_rollback`
- `isolation_failed`

`ts_ms` 是确定性逻辑时间；`timestamp` 是实际 UTC 时间，仅用于人工观察，不参与重放序列比较。

## 9. 快照格式

核心字段：

```json
{
  "snapshot_format_version": 1,
  "name": "normal-v1",
  "script": {},
  "isolation_levels": {"n1": "read_committed"},
  "config": {"lock_wait_budget_ms": 500, "max_concurrency": 2, "tick_ms": 10},
  "initial_state": {"rows": [], "data_version": 0, "digest": "..."},
  "result_state": {"rows": [], "data_version": 3, "digest": "..."},
  "execution": {"events": [], "sessions": [], "deadlocks": []},
  "state_version": 3
}
```

写入流程使用同目录临时文件、`fsync`、`os.replace`，避免进程终止留下半写快照。

恢复读取 `result_state`，在单个 SQLite 事务内删除并重写全部行与 `data_version`，随后重新计算摘要验证。`state_version` 与当前版本不同时报告 `version_mismatch=true`，但恢复策略始终为 `full_replace`。

## 10. 重放比较

重放不依赖原 SQLite 文件：

1. 创建临时本地 SQLite 文件；
2. 写入快照 `initial_state`；
3. 用快照脚本与参数重新调度；
4. 为每个会话比较结局与业务码；
5. 规范化事件（去掉 `timestamp`）后逐项比较；
6. 汇总分歧会话并按原脚本顺序稳定排序。

若重放事件/结局不一致，返回 `E_REPLAY`。若只改参数但运行结果仍完全一致，则不算不可复现。

## 11. 非目标与边界

- 不实现 SQLite 文件级锁竞争；SQLite 仅保存已提交版本，冲突由应用层行锁决定。
- 并发是确定性协作式交错，不是操作系统线程级随机竞争；这是稳定演示和稳定死锁仲裁的前提。
- 脚本与 SQL 被视为可信本地输入；空值、任意 JSON 值可写入行载荷。
- 快照格式版本固定为 1；不兼容版本返回 `E_REPLAY`。

