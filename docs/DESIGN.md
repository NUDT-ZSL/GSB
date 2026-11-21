# LockLedger 设计说明

## 1. 数据契约

一个场景 JSON 必须包含 `database`、`transactions`、`config` 三部分。

### database

```json
{
  "tables": [
    {
      "name": "accounts",
      "primary_key": "id",
      "columns": [
        {"name": "id", "type": "INTEGER"},
        {"name": "owner", "type": "TEXT"}
      ]
    }
  ],
  "rows": {
    "accounts": [{"id": 1, "owner": "owner-01"}]
  }
}
```

表名和列名大小写不敏感地要求唯一，并必须匹配 `^[A-Za-z_][A-Za-z0-9_]*$`。构建样本库时所有 SQL 标识符都经过双引号转义。

### transactions

每个事务声明：

- `tx_id`：稳定事务标识
- `tx_no`：正整数事务号，用于确定性排序和牺牲决策
- `isolation`：`READ_COMMITTED`、`REPEATABLE_READ`、`SERIALIZABLE`、`SNAPSHOT`
- `wait_budget_ms`：正整数锁等待预算
- `max_retries`：正整数重试上限，持久化并纳入决策依据
- `operations`：有序操作
- 可选 `rollback_after_ops`：完成指定数量操作后主动回滚

闭合操作类型：

- `read`：行级共享锁 `S`
- `write`：行级排他锁 `X`
- `range_scan`：区段共享锁 `S`
- `lock`/`lock_declare`：显式声明行或区段、`S` 或 `X`

引用表、列、主键行和范围边界均在纯内存阶段校验。

### config

- `clock_step_ms`：逻辑时钟基础步进，正整数
- `scale`：定点标度，单位/毫秒，可为正整数、十进制字符串或 `"p/q"`
- `tolerance`：固定点单位容差，非负有理数
- `error_budget`：单事务累计误差上界预算
- `max_concurrency`：全局并发实例上限
- `deadlock_check_period_ms`：逻辑时钟死锁检测周期
- `max_rounds`：逻辑轮次上限
- 可选 `logical_horizon_ms`：显式逻辑水位

`scale` 必须大于 0，分子和分母均不超过 `1_000_000_000`。

锁等待预算是事务级累计值：每次等待完成后写入 `waited_ms`，下一次冲突使用
`wait_budget_ms - waited_ms` 作为剩余截止时间；不会按每次锁申请重新获得完整预算。

## 2. 校验与零副作用

`load_scenario` 只读取 JSON 并构造不可变模型。只有它成功后，CLI 才会：

1. 检查工作区可写和磁盘余量；
2. 重建 SQLite 样本库；
3. 初始化锁表与账本。

因此 `E_BAD_SCRIPT`、`E_NO_TABLE`、`E_BAD_SCALE` 等业务错误不会创建锁表、账本指针或等待登记。

## 3. 确定性调度

调度单位是一个逻辑毫秒轮次。时钟只来自 `run_state.clock_ms`，推进规则为：

```text
next = min(
  current + clock_step_ms,
  nearest_wait_deadline,
  current + deadlock_check_period_ms,
  logical_horizon
)
```

这保证超时永远在其真实 `enqueue_tick + wait_budget_ms` 时刻处理，而不是等到下一个粗粒度步进。死锁检测也以逻辑 tick 为依据。

每轮顺序固定：

1. 按 `tx_no` 启动不超过并发上限的 `NOT_STARTED` 事务；
2. 处理到达预算截止时刻的等待；
3. 按周期构造成等待图并处理死锁；
4. 按 `tx_no` 依次推进每个 `RUNNING` 事务一个操作；
5. 按 `enqueue_seq` 扫掠等待队列，授予当前可兼容队首；
6. 整个轮次在同一个 SQLite 事务中提交。

没有任何 `time.time()`、`perf_counter()` 或进程实际耗时参与业务判断。

## 4. 锁资源与 FIFO

资源键：

```text
row:<table>:<canonical-json-row-key>
range:<table>:<canonical-json-lo>:<canonical-json-hi>
```

冲突规则：

- 行-行：同一主键，且至少一方为 `X`
- 区段-区段：区间重叠，且至少一方为 `X`
- 行-区段：行值落入区段，且至少一方为 `X`
- 同一事务重复申请已覆盖的 `S`，或已持有 `X` 后申请 `S/X`：兼容复用
- 同一事务持有同资源 `S` 后申请 `X`：无其他冲突时原地升级

`lock_holders` 保存持有者、资源、模式、稳定获得序号。`lock_queue` 保存 `enqueue_seq`、模式、入队 tick 和截止 tick。新申请只要与任一更早的冲突等待项重叠，就必须排到其后，即使当前持有者模式兼容，也不能插队。

## 5. 定点换算与误差链

等待时长：

```text
raw_wait_ms = complete_tick - enqueue_tick
exact_units = raw_wait_ms * scale
fixed_units = floor(exact_units)
remainder   = exact_units - fixed_units
```

每次登记保存：

- 全局唯一 `registration_seq`
- `raw_wait_ms`
- 标度分子/分母
- 精确值、固定点值
- 被舍弃余量、舍入方向 `DOWN`、有符号误差
- 事务内权重位置和锁资源

事务累计值：

```text
wait_fixed_units = sum(fixed_units)
error_upper_bound = sum(remainder)
```

状态中的 `error_source_chain` 按权重位置输出参与登记的序号、余量、误差和锁资源，可直接追溯每一单位误差来源。

## 6. 账本敏感判定

死锁牺牲候选按固定点等待值排序：

```text
max(wait_fixed_units, -tx_no)
```

即先比较固定点等待，平局时较小事务号优先被牺牲。裁决依据同时记录事务号、原始等待、固定点等待、偏差与误差上界。

对任意两个候选：

```text
abs(fixed_wait_i - fixed_wait_j) < tolerance
```

且该差值参与牺牲选择时，决策标记 `ledger_sensitive`。系统再用 `scale * 1000` 重算候选顺序：

- 若高标度牺牲者与当前牺牲者不同，`would_flip_at_higher_scale = true`
- 输出当前顺序、精确顺序、当前/高标度牺牲者和具体依据
- 敏感对的两个事务在 `status.transactions` 中都标记为敏感

主样本正好复现：当前两者都向下取整为 0，平局选 `T03`；高标度下 `T04` 等待更久，选择翻转。

## 7. 结局状态

严格区分：

- `NOT_STARTED / BUDGET_NOT_STARTED`：从未启动，没有启动 tick，不生成等待或超时
- `TIMEOUT / WAIT_BUDGET_EXCEEDED`：已入队且真实计时到达截止 tick，记录等待后释放全部持有锁
- `ABORTED / VOLUNTARY_ROLLBACK`：运行中主动回滚
- `SACRIFICED / DEADLOCK_VICTIM`：死锁环中的确定性牺牲者
- `COMMITTED`：完成全部操作

全局逻辑水位到达时，活动事务标记 `ABORTED / GLOBAL_HORIZON`，未启动事务仍标记 `BUDGET_NOT_STARTED`。不会把未启动事务改写成 `TIMEOUT`。

## 8. 死锁检测

等待图节点为事务，边包括：

- 等待者到冲突持有者
- 等待者到更早的冲突队列项

按周期进行确定性 DFS，收集完整简单环，选择事务号元组最小的环。牺牲者最终等待记录在解除前登记为 `SACRIFICED`，随后：

1. 释放牺牲者的全部持有锁；
2. 取消其余未完成等待项；
3. 只删除牺牲者的队列项，不重排其他 `enqueue_seq`；
4. 按原 FIFO 顺序继续授予可推进事务。

## 9. 持久化与恢复

SQLite 运行表：

- `run_state`：逻辑时钟、水位、序列号、最终状态
- `transactions`、`op_commits`
- `lock_holders`、`lock_queue`
- `wait_records`、`rounding_entries`
- `decisions`、`events`

版本化账本：

- `ledger_versions`
- `ledger_transactions`
- `ledger_wait_records`
- `ledger_rounding_entries`
- `ledger_decisions`
- `ledger_lock_holders`、`ledger_lock_queue`
- `ledger_pointer`

每轮提交时：

1. `BEGIN IMMEDIATE`；
2. 将当前账本复制到 session 临时表 `_stage_*`；
3. 插入新的 `version` 快照；
4. 在同一个 SQLite 事务内更新 `ledger_pointer`；
5. 提交。

因此账本指针只会指向完整版本。进程在入队、获锁、放弃或一轮中途被强制终止时，未提交轮次整体回滚；恢复从 `run_state` 的已提交水位继续。

幂等约束：

- `op_commits PRIMARY KEY(tx_key, op_index, operation_version)`
- `wait_records UNIQUE(tx_key, op_index, operation_version)`
- `lock_queue UNIQUE(tx_key, op_index, operation_version)`
- 锁队列入队序号和登记序号均为运行内唯一
- 相同精确资源出现第二个 `X` 持有者时，数据库触发器直接拒绝

## 10. 多进程一致性

所有写事务采用：

1. 跨平台文件锁（POSIX `flock` / Windows `msvcrt.locking`）；
2. SQLite `BEGIN IMMEDIATE`；
3. `PRAGMA busy_timeout=30000` 和 `synchronous=FULL`；
4. WAL 读取快照。

多个 `--workers` 进程共同推进同一逻辑事件流。一次只有一个进程能提交一轮，其他进程拿到锁后观察最新状态，因此不会重复授予同一操作版本或重复登记等待。实测双进程与单进程的账本摘要和逐事务等待登记序列完全一致。

## 11. 真值与 verify

`verify` 从已提交的逻辑入队/完成 tick 重建序列，使用 `fractions.Fraction` 独立重算：

- 原始等待毫秒
- 精确固定点值
- 向下取整值
- 被舍弃余量

并核对：

- 在隔离临时目录中按同一脚本从头串行重放，要求等待序列、决策与摘要一致
- 每条等待的登记序号全局唯一
- 事务等待总和、误差上界和来源链一致
- 完成后无锁持有者、无 `WAITING` 队列项
- 样本库 `integrity_check`
- 场景副本 SHA-256 与账本元数据一致
- 当前账本指针存在有效摘要
- 当前版本账本的行内容重新计算后与 `state_digest` 一致

## 12. 边界与约定

- 锁协议采用保守严格两阶段锁；隔离级别被校验、持久化和输出，但不改变锁粒度。
- 样本库是不可变引用数据；逻辑 `write` 只产生锁意图和操作提交记录，不回写样本库，避免 SQLite 自身页面锁干扰所测的共享锁表。
- `max_retries` 是声明上限并进入决策依据；当前确定性策略不以自动重试复活已放弃或已牺牲事务，避免违反恢复幂等。
- 逻辑时钟不读取墙钟，因此同一 Python/SQLite 语义下的结果可重复；SQLite 文件字节级布局不属于确定性契约。
- 范围冲突支持整数和字符串等可比较 JSON 标量，不做隐式跨类型转换。

