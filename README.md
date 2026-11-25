# 事务锁等待定点账本与回滚裁决引擎

这是一个只依赖 Python 标准库的独立原型，用可注入逻辑时钟模拟并发事务、
行锁等待队列、预算超时、死锁牺牲、两代策略切换和崩溃恢复。工程不访问网络，
不依赖 Docker，也不修改样本源数据库；运行态全部写入本地 SQLite。

## 快速开始

```bash
python3 --version
python3 generate_samples.py --out samples
python3 main.py load --samples samples/
python3 main.py run --samples samples/
python3 main.py status --samples samples/
python3 main.py verify --samples samples/
```

默认运行数据库是 `./lock_wait_lab.sqlite3`，可使用 `--db` 和 `--run-id`
覆盖。命令成功输出 `{"ok": true, ...}`，业务校验失败输出独立错误码和首个
违规字段，例如：

```json
{
  "error": {
    "code": "E_POLICY_VERSION_ROLLBACK",
    "field": "generation",
    "message": "cannot roll policy generation back from 2 to 1"
  },
  "ok": false
}
```

## 交付内容

- `generate_samples.py`：固定种子 `424242` 的确定性样本生成器。
- `samples/`：已生成的样本库描述、实际 SQLite 样本、事务 JSONL、v1/v2
  策略快照、v2 分片/元数据库和逻辑时钟配置。
- `lock_wait_lab/samples.py`：结构、引用、行数据、策略、时钟和 SQLite
  一致性校验，以及策略作用域解析。
- `lock_wait_lab/engine.py`：分层事件循环、自建锁表/FIFO 等待队列、定点
  等待账本、误差传播、死锁牺牲和回滚分类。
- `lock_wait_lab/store.py`：SQLite 状态、锁快照、等待队列、账本、精度记录、
  策略提交和微步日志的原子检查点。
- `lock_wait_lab/verify.py`：固定点重算、策略指针、全新重放和多个断点的
  持久化恢复验证。
- `tests/test_engine.py`：确定性、端到端裁决和真实 `SIGKILL` 恢复测试。
- `VALIDATION.md`：本次实际执行结果。

## 样本契约

### 样本库

`samples/sample_db.json` 描述 `branches`、`accounts`、`ledger_entries`
三张表，包含列类型、非空约束、主键、外键、唯一/普通索引和固定行数据。
`samples/sample.sqlite` 必须与描述中的表、列顺序、类型、空值约束、索引和
行数据完全一致。

校验错误码包括：

- `E_DB_DUPLICATE_TABLE`、`E_DB_DUPLICATE_COLUMN`
- `E_DB_COLUMN_INVALID`、`E_DB_INDEX_INVALID`
- `E_DB_KEY_INVALID`、`E_DB_FOREIGN_KEY_INVALID`
- `E_DB_DATA_INVALID`、`E_DB_SQLITE_MISMATCH`

### 事务流

`samples/transactions.jsonl` 每行是一个事务，含 `tx_id`、`start_at_ms`、
`isolation`、`retry_limit` 和有序 `operations`。每个读写操作声明表、主键、
`SHARED`/`EXCLUSIVE` 锁和逻辑纳秒时长；`ROLLBACK` 只能作为最后操作。

支持 `READ_COMMITTED`、`REPEATABLE_READ`、`SERIALIZABLE`。引擎对所有
隔离级别采用保守的锁保持策略：行锁一直持有到提交或回滚，从而保证固定隔离
级别下不会发生脏读，并让等待图与账本可复现。

### 两代策略

- v1 是单文件 `policies/v1.json`，包含相对等待预算、默认预算、重试倍率和
  扁平作用域映射。作用域优先级为 `resource:`、`tx:`、`table:`、`default`。
- v2 是 `policies/v2/shard-*.json` 加
  `policies/v2/policy_meta.sqlite`。清单保存绝对生效毫秒、代数、摘要、分片
  校验和与墓碑；元数据库保存代际历史、策略项和失效墓碑。
- 校验要求代数和绝对生效毫秒单调、分片摘要和策略摘要一致，并拒绝回退。
- 样本在逻辑时刻 `2 ms` 自动提交 v2。提交时创建新的账本版本并原子切换活动
  指针，不逐条覆盖旧账本。
- 事务在首次启动时绑定当时的策略代数，直到提交、超时放弃或牺牲回滚；
  进行中的 v1 事务不会因 v2 生效而改绑。

## 执行模型

### 逻辑事件顺序

每个微步只执行一个确定性动作，并按固定优先级选择：

1. 到达总逻辑预算后收尾。
2. 提交到期的策略快照。
3. 检测并处理等待图强连通分量中的死锁。
4. 处理到达定点预算触发点的等待超时。
5. 完成已到期的读写操作。
6. 按资源名和队列位置 FIFO 唤醒等待者。
7. 在并行度上限内启动待处理事务。
8. 按 `script_seq` 稳定顺序为就绪事务申请下一把行锁。
9. 推进到下一个启动、操作完成、超时或策略切换事件。

锁兼容规则是共享锁之间兼容，任一排他锁与其它锁互斥。队列不允许后到请求
绕过队首；所有顺序都不读取墙钟、进程号或随机数。

### 定点账本

逻辑时间单位是纳秒。等待时长按策略声明的毫秒定点标度做 `HALF_UP` 换算，
每条账本记录：

- 原始纳秒、定点标度、定点毫秒字符串；
- 舍入方向、残差和被舍弃余量；
- 自身换算误差上界；
- 锁资源、持有事务、持有操作和保持时长依赖链；
- 依赖链累计误差上界。

预算触发点按定点标度计算，因此存在“当前标度达到预算、提高一级后尚差一点”
的真实边界。例如 T06 在 `1,999,500 ns` 被当前 3 位小数标度报告为
`2.000 ms`，提升到 4 位后为 `1.9995 ms`，裁决翻转会被显式记录。

### 放弃与牺牲

- 预算内未启动：`NOT_STARTED_WITHIN_BUDGET`
- 入队后等待超时：`ABANDONED_WAIT_TIMEOUT`
- 显式主动回滚：`MANUAL_ROLLBACK`
- 死锁牺牲：`DEADLOCK_VICTIM_ROLLBACK`
- 总预算耗尽时仍活动：`ABANDONED_AT_HORIZON` /
  `ACTIVE_ROLLBACK_AT_HORIZON`

死锁牺牲先按当前定点标度下的等待时长选择最长者，再以事务 ID 作稳定
平局裁决。若两个候选的误差区间相交，会写入
`DEADLOCK_VICTIM_SELECTION` 精度敏感记录，并同时给出当前标度和提高一级后
的牺牲对象及是否翻转。

## 持久化与恢复

每个微步在一个 `BEGIN IMMEDIATE` SQLite 事务中同时提交：

- 完整运行状态和已提交水位；
- 当前账本版本、全部等待账本记录；
- 所有时长换算记录；
- 精度敏感记录；
- 待处理/已完成事务状态；
- 当前锁持有表和等待队列；
- 事务裁决、策略提交与活动账本指针；
- 轮次摘要和不可重复的微步日志。

运行数据库启用 WAL 和 `synchronous=FULL`。SIGKILL 只可能留下上一完整
检查点；`resume` 从该状态继续，不重放已提交微步。账本主键与微步日志主键
防止重复计数，已提交裁决不会改变。

故障注入示例：

```bash
python3 main.py load --samples samples/ --db /tmp/kill.sqlite3
python3 main.py run --samples samples/ --db /tmp/kill.sqlite3 \
  --crash-after-step 20   # 在持久化后向自身发送 SIGKILL，退出码 137
python3 main.py resume --samples samples/ --db /tmp/kill.sqlite3
python3 main.py verify --samples samples/ --db /tmp/kill.sqlite3
```

也可以使用 `--pause-after-step N --pause-seconds S` 暂停，供外部进程执行
真实 `kill -9`。

## 验证

```bash
python3 -m unittest discover -v
python3 main.py verify --samples samples/
```

`verify` 会重新计算所有定点条目、检查策略指针链、检查 SQLite 与内存状态
一致，并在临时数据库中：

- 全新重放一次完整执行；
- 在多个日志断点模拟持久化后崩溃；
- 关闭并重新打开数据库后恢复；
- 比较状态摘要、账本摘要和裁决摘要。

本次实际结果见 `VALIDATION.md`。
