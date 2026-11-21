# SQLite 锁等待与索引命中联合诊断工具

这是一个可离线运行的确定性诊断器，用自建 SQLite 锁表复现并发事务脚本中的共享锁、排他锁、行锁、区段锁与表锁交错，并在注入逻辑时钟下记录等待时长、定点换算误差、索引命中和死锁裁决。

实现只依赖 Python 标准库；核心运行链路使用 `sqlite3`、`json` 和 `multiprocessing`，没有 NumPy、外部数据库服务、网络访问或容器依赖。

## 快速开始

环境：Python 3.10+，Windows/Linux 均可，命令行可调用 `python`。

```text
python -m unittest discover -s tests -v
python demo.py
```

手工运行：

```text
python -m lockdiag generate --out sample
python -m lockdiag run --sample sample --state runtime
python -m lockdiag status --state runtime
python -m lockdiag verify --state runtime
python -m lockdiag report --state runtime
```

中断恢复：

```text
python -m lockdiag run --sample sample --state crash-state --crash-at enqueue
python -m lockdiag resume --state crash-state --sample sample
python -m lockdiag report --state crash-state --out report-resumed.json
```

`--crash-at enqueue|grant|abandon` 是测试故障注入开关。它在对应原子事件提交后、下一步推进前以退出码 `97` 强制终止进程，用于验证已提交等待/锁记录可继续推进，以及恢复后账目与未中断运行逐字节一致。

## 目录

```text
lockdiag/
  cli.py          run/resume/status/verify/report/generate 命令
  generator.py    固定种子样本、事务脚本、配置和逻辑时钟生成器
  validation.py   样本库、脚本、依赖和配置预校验
  schema.py       SQLite 状态库 DDL
  store.py        原子事件边界、恢复意图和状态投影
  clock.py        注入时钟、定点换算、舍入与误差上界
  locks.py        行/区段/表锁冲突和 FIFO 原语
  engine.py       轮转调度、等待、预算超时、死锁牺牲、归因
  worker.py       多进程 EXPLAIN QUERY PLAN / SELECT 操作探针
  report.py       稳定排序 JSON 报告
  verify.py       账目、队列、锁、时钟和死锁不变量验证
tests/
demo.py
```

## 样本

`generate` 使用固定种子生成以下文件：

- `sample.db`：`customers`、`orders`、`audit`、`settings` 四表，固定数据和三个显式索引。
- `schema.json`：表、列、索引的声明副本，用于唯一性和数据库一致性校验。
- `transactions.json`：5 个事务，包含读、写、范围扫描、表级锁声明和主动回滚。
- `config.json`：并发上限、每事务等待预算、逻辑时钟参数、定点标度、容差、死锁检测周期。
- `clock.json`：严格递增的显式注入时钟原始序列；原始时间单位为 `1/7 ms`。
- `manifest.json`：固定文件名清单。

同一种子重复生成时，上述文件（包括 `sample.db`）逐字节一致。

## 关键设计

### 预校验

运行任何写入前先完成全量校验。校验失败不会创建状态库，也不会写入锁记录或等待记录。

- 表名、索引名唯一；`schema.json` 与 SQLite 实际结构和索引一致。
- 操作种类、顺序、表列引用、隔离级别、锁模式全部闭合枚举。
- 读操作只允许共享锁，写操作只允许排他锁；范围扫描和锁声明允许共享/排他锁。
- 等待预算、并发上限、工作进程数、定点标度和时钟参数必须为正整数。
- 容差必须为严格正数。
- `depends_on` 执行静态 DFS，发现循环事务依赖时返回 `E_TX_CYCLE`。

### 锁表与 FIFO

- 资源键使用规范化 JSON：行锁 `["row", table, row]`、区段锁 `["range", table, column, start, end]`、表锁 `["table", table]`。
- 共享锁之间兼容；排他锁与任意锁冲突；行与重叠区段冲突；表锁与同表所有行/区段按模式冲突。
- 冲突申请写入 `wait_queue`，`queue_no` 单调递增；释放时只从队首按序批准，队首不能批准则停止。
- 同一事务重复申请同一资源时执行重入裁决：原锁满足则复用；共享锁升级排他锁时可原位升级或按 `UPGRADE_WAIT` 排队；重复排队返回同一 `wait_id`。
- 所有“持锁开始、入队、获得、放弃”时刻只使用注入逻辑时钟样本，不读取系统墙钟。

### 等待时长与误差

每次等待保存：

- 入队/出队采样点编号、序列编号和原始值；
- 原始差值、定点标度、换算比例；
- `HALF_UP` 舍入方向、舍入前分子/分母、舍入后定点单位；
- 实际残差和保守的单次换算上界 `1/(2*scale) ms`。

事务误差上界为各次换算上界之和；整数定点相加本身不引入额外舍入。误差传播链保存等待资源、持锁事务集合、采样点、舍入方向和中间值。

报告对所有等待总时长差小于容差的事务对生成精度敏感标记：

- 普通排序敏感：`WAIT_ORDER`；
- 涉及超时放弃裁决：`BUDGET_DECISION`；
- 标记包含首个舍入分歧采样点和两事务的定点值、精确中间值、误差上下界。

### 调度、预算与超时

- 全局并发上限控制 NEW→RUNNING；事务依赖全部进入终态后才允许启动。
- 调度采用固定 `start_order` 轮转；每个事务一次推进一个操作，确保同一时钟序列下顺序可复现。
- 等待预算按事务累计；等待原始时间达到预算阈值后裁决为 `ABANDONED_TIMEOUT`，同时释放该事务已持有锁并批准后续队列。
- 只有 WAITING 事务会因等待预算超时；RUNNING 事务不会因达到调度步数被改写成超时。
- 预算不足且从未启动的事务可区分地表示为 `NOT_STARTED_CONCURRENCY`；主动回滚表示为 `ROLLED_BACK_ACTIVE`。

### 索引归因

工作进程先执行 `EXPLAIN QUERY PLAN` 和受限 `SELECT`，保存实际计划路径，例如：

- `INDEX:idx_orders_customer`
- `INDEX:idx_orders_status`
- `INDEX:idx_customers_region`
- `ROWID:customers`
- `TABLE_SCAN:table`（确实走全表扫描时）

等待发生后按 `holder_tx -> waiter_tx -> index_path` 三元组累计次数、时长、实际舍入残差和误差上界。

### 死锁

调度器周期性从提交的等待队列重建等待图，并对等待者到持锁者/相关排队者的边做确定性环检测。环的规范起点取最小事务 ID；牺牲者取环内最大 `start_order`（同序再按事务 ID）。

每次裁决保存完整环、牺牲依据、释放的等待、后续获准事务、裁决后的队列和锁快照，并重新检查持锁集合是否仍兼容。

### 原子持久化与恢复

每个锁事件在一个 `BEGIN IMMEDIATE ... COMMIT` 中同时更新：

- 事务阶段和当前操作版本；
- 锁持有与 FIFO 队列；
- 等待记录、事务账目、误差链和三元组归因；
- 死锁/超时决策；
- 逻辑时钟样本和水位；
- `commit_bundles` 中的队列摘要、锁摘要和时间水位。

事件开始前可写一条 `recovery_intents(STAGED)`。恢复时：

- 删除仍未提交的 `STAGED` 意图；
- 只清理指向不存在/终态事务的孤儿锁和未提交排队登记；
- 从已提交记录重建队列摘要、事务账目、误差链和归因；
- 继续使用同一逻辑时钟序列和水位。

测试会在故障状态中人工插入 `STAGED` 意图和孤儿锁，再执行恢复，验证清理数量、账目和最终报告。

### 多进程与文件锁

- `multiprocessing` 的 spawn 进程池并行执行事务操作探针；结果按 `(tx_id, op_seq)` 排序后提交，消除进程完成顺序差异。
- 状态目录使用跨平台文件锁（Windows `msvcrt`，Unix `fcntl`）保护初始化/恢复/推进临界区。
- SQLite 主键/唯一约束保证同一事务同一操作版本只登记一次、同一等待 ID 只解析一次、同一资源同一事务只有一个持有记录；`resolve_wait` 对已解析记录为幂等空操作。
- SQLite 写事务串行化锁授予检查，避免同一行/区段被并发超授。

## 报告

`report` 使用 `json.dumps(sort_keys=True)` 稳定输出，包含：

- 事务等待总时长、实际舍入残差、误差上界；
- 每次等待的入队/出队采样、原始值、舍入方向、换算与容差；
- 误差传播链；
- `holder_tx -> waiter_tx -> index_path` 归因；
- 所有操作实际索引路径；
- 精度敏感清单和首个分歧采样点；
- 完整死锁环、牺牲事务、后续推进、队列/锁复核结果；
- 最终队列、最终锁和汇总。

报告不包含墙钟时间、PID、临时路径或进程完成顺序，因此固定样本重复运行可逐字节比较。

## 错误码

`E_NO_TABLE`、`E_NO_COLUMN`、`E_DUP_TABLE`、`E_DUP_INDEX`、`E_DUP_TX`、`E_BAD_JSON`、`E_BAD_OP`、`E_BAD_OPERATION_ORDER`、`E_BAD_BUDGET`、`E_BAD_CONCURRENCY`、`E_BAD_SCALE`、`E_BAD_TOLERANCE`、`E_BAD_LOCK_MODE`、`E_BAD_ISOLATION`、`E_BAD_CLOCK`、`E_BAD_DEPENDENCY`、`E_TX_CYCLE`、`E_SAMPLE_MISMATCH`、`E_STATE_EXISTS`、`E_NO_STATE`、`E_STATE_CORRUPT`。

CLI 业务错误返回码为 `2`，验证失败返回 `1`，成功返回 `0`。

## 实际验证结果

验证环境：Python `3.10.12`，SQLite `3.37.2`，Linux 本地文件系统。

```text
python -m unittest discover -s tests -v
Ran 24 tests in 7.8s
OK
```

固定样本 `python demo.py` 的结果：

- 两次报告逐字节一致；SHA-256：`3b1a40ce9c1a515eb35c7451354554f1968b7152e655a1b7e49a5731d1b8b6dc`。
- `verify` 全部 11 项不变量通过。
- 事务结局：`T1=COMMITTED`、`T2=ROLLED_BACK_DEADLOCK`、`T3=COMMITTED`、`T4=ABANDONED_TIMEOUT`、`T5=ROLLED_BACK_ACTIVE`。
- 等待记录 4 条，总等待 `0.08 ms`，累计误差上界 `1.000000 ms`。
- 精度敏感事务对 3 组，同时包含 `WAIT_ORDER` 与 `BUDGET_DECISION`。
- 死锁环为 `T1 -> T2 -> T1`，确定性牺牲 `T2`，`T1` 继续推进；裁决后持锁冲突为空。
- 三元组归因 4 组，命中 `idx_orders_customer`、`idx_orders_status`、`idx_customers_region`，并记录主键 ROWID 路径和锁声明无索引路径。
- `enqueue`、`grant`、`abandon` 三个故障点均在退出码 `97` 后成功恢复，恢复报告与未中断报告逐字节一致；恢复过程清理了人工插入的 `STAGED` 意图和孤儿锁。

