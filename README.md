# MVCC Domain Editor

面向多用户共享记录集的确定性 MVCC 编辑与可见性判定应用。项目从空工作区实现，使用 Python 3.11+、标准库 `sqlite3` 和 NumPy；不依赖 Docker、服务端进程、外部数据或系统墙钟。

核心能力：

- 严格校验表描述、会话脚本和引擎配置，非法输入在打开/创建数据库前失败。
- 按行维护版本链：创建事务、删除事务、逻辑时钟、内容摘要、前驱版本和合并次数。
- 为每个事务注册读视图：活跃事务集合、快照水位、隔离级别和固定代快照。
- 区分 `repeatable_read` 与 `read_committed`，支持删除后重建、回滚、同事务多写合并和丢失更新拒绝。
- 范围读输出稳定排序 JSON，包含可见行、快照水位、事务依据链和逻辑延迟。
- SQLite 候选代快照 + 单指针原子切换；中断恢复清理未发布代，只重放缺失步骤。
- 同步/异步 GC；GC 保留所有被读视图引用的版本，并保证并发查询不读断链版本。
- 多进程提交幂等；`verify` 使用 NumPy 批量比对两次独立重放结果。

详细设计见 `TECHNICAL.md`，实际运行结果见 `VERIFICATION.md`。

## 环境准备

Windows PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
```

验证运行时：

```powershell
.\.venv\Scripts\python.exe -c "import sys, sqlite3, numpy; print(sys.version, sqlite3.sqlite_version, numpy.__version__)"
```

`sqlite3` 使用 Python 标准库，不需要额外 SQLite 驱动。数据库文件和 `store_dir` 版本目录在首次初始化时自动创建。

## 快速开始

样本位于 `samples/`，其中 `config.json` 的默认数据库路径是 `data/mvcc.db`。

```powershell
# 初始化并重放全部确定性会话
python -m mvcc_app init `
  --table samples/table.json `
  --config samples/config.json `
  --sessions samples/sessions.json `
  --replay

# 查看版本链、读视图和 GC 状态
python -m mvcc_app status

# 以事务 109 在注入时钟 2010ms 查询 id=3
python -m mvcc_app read --txn-id 109 --clock-ms 2010 `
  --where '[{"column":"id","op":"eq","value":3}]'

# 同步 GC；也可加 --async 启动独立后台进程
python -m mvcc_app gc --clock-ms 2200

# 两次独立重放 + NumPy 批量摘要比对
python -m mvcc_app verify --runs 2 --gc-clock-ms 2200
```

首次直接运行 `edit --sessions ... --table ... --config ...` 时，如果数据库不存在，也会自动初始化并重放。

## CLI

| 子命令 | 作用 |
| --- | --- |
| `init` | 校验三个样本并初始化数据库；`--replay` 立即执行会话脚本 |
| `edit` | 重放会话脚本，或对指定事务执行插入、更新、删除、提交/回滚 |
| `read` | 在注入时钟执行范围读，输出稳定 JSON |
| `gc` | 同步或异步回收满足条件的版本 |
| `resume` | 清理未发布候选代，幂等重放缺失步骤 |
| `status` | 输出版本链长度、读视图注册数、GC 回收数和保留原因 |
| `verify` | 校验当前快照完整性，并比较多次独立确定性重放 |

所有成功和业务错误输出均为排序 JSON。默认压缩输出；加 `--pretty` 生成缩进 JSON。

### 交互式编辑

```powershell
python -m mvcc_app edit --txn-id 100 --type insert --clock-ms 1000 `
  --row '{"id":10,"name":"new","balance":10,"region":"east"}'

python -m mvcc_app edit --txn-id 100 --type update --clock-ms 1010 `
  --key '{"id":10}' --changes '{"balance":20}'

python -m mvcc_app edit --txn-id 100 --finish --clock-ms 1020 --outcome committed
```

`--finish --outcome rolled_back` 执行回滚。事务 ID 必须在初始化时注册；交互式编辑不允许修改主键。

### 异步 GC

```powershell
python -m mvcc_app gc --clock-ms 2200 --async
python -m mvcc_app status
```

`gc --async` 返回 `status: started`，随后可用 `status` 观察 `gc.runs` 和 `gc.collected_total`。Windows 使用 detached process，非 Windows 使用独立 session。

### 中断恢复

测试钩子会在候选代落盘后立即强制退出：

```powershell
python -m mvcc_app edit --sessions samples/sessions.json --crash-at-event 2
python -m mvcc_app resume
python -m mvcc_app resume
```

第二次 `resume` 的 `missing_steps` 应为全零，版本总数不变。

## 样本格式

### 表描述

```json
{
  "table": "accounts",
  "columns": [
    {"name": "id", "type": "integer", "primary_key": true},
    {"name": "name", "type": "text", "nullable": false}
  ]
}
```

列类型闭合枚举：`integer`、`real`、`text`、`boolean`。至少一个主键列；主键不可更新、不可为 `null`。

### 会话脚本

```json
{
  "sessions": [
    {
      "transaction_id": 101,
      "start_clock_ms": 1000,
      "isolation": "repeatable_read",
      "commit_clock_ms": 1300,
      "outcome": "committed",
      "operations": [
        {"seq": 1, "clock_ms": 1010, "type": "insert",
         "row": {"id": 1, "name": "alpha", "balance": 100, "region": "east"}},
        {"seq": 2, "clock_ms": 1020, "type": "update",
         "key": {"id": 1}, "changes": {"balance": 150}},
        {"seq": 3, "clock_ms": 1030, "type": "range_read", "read_id": "r1",
         "where": [{"column": "region", "op": "eq", "value": "east"}],
         "order_by": ["id"], "limit": 20}
      ]
    }
  ]
}
```

隔离级别闭合枚举：`read_committed`、`repeatable_read`。操作类型闭合枚举：`insert`、`update`、`delete`、`range_read`。事务开始时钟按文件顺序非降序；事务号、开始时钟和显式操作时钟必须是正整数。

`seq`、`clock_ms`、`commit_clock_ms`、`outcome` 可省略，引擎用配置的 `clock_step_ms` 确定性推导。范围过滤支持 `eq`、`ne`、`gt`、`gte`、`lt`、`lte`、`in`。

### 引擎配置

关键字段：

- `clock_step_ms`：缺省操作时钟步进。
- `snapshot_retention`：保留读视图数量上限；超限按 `(created_clock, view_id)` 确定性过期。
- `visibility_tolerance_ms`：允许的逻辑时钟轻微回拨阈值。
- `gc_watermark_txn`：GC 事务号水位；创建/删除事务号均须低于它。
- `query_latency_budget_ms`：单次查询逻辑时延预算。
- `query_base_cost_ms`、`query_version_cost_ms`、`query_row_cost_ms`：确定性查询代价模型。
- `db_path`、`store_dir`：SQLite 数据库和版本存储目录。

## 时间与可见性

引擎不读取系统墙钟。每个事件使用样本或 CLI 注入的逻辑毫秒时钟。

视图时钟为 `T` 时：

- 活跃事务集合包含 `start_clock <= T` 且 `end_clock is NULL or end_clock >= T` 的事务，并按事务号升序保存。
- 快照水位是 `commit_clock < T` 的已提交事务的最大提交时钟。
- 因此时钟 `T` 的提交在 `T` 的视图中仍按活跃/不可见处理；`T+step` 后才可见。这保证同一逻辑时钟下重建视图不受物理执行先后影响。
- 当前事务自己的写入和删除在该事务提交前即可见；回滚事务创建的所有版本永久不可见。

`repeatable_read` 复用开始时钟构造的固定视图；`read_committed` 在每个操作/查询时钟重建视图。

## 错误码

业务错误统一输出：

```json
{"ok":false,"error":{"code":"STALE_SNAPSHOT","message":"...","details":{}}}
```

主要错误码包括：

- 校验：`INVALID_SCHEMA`、`DUPLICATE_COLUMN`、`INVALID_COLUMN_TYPE`、`INVALID_TXN`、`INVALID_CLOCK`、`TXN_START_ORDER`、`INVALID_ISOLATION`、`INVALID_OPERATION`、`INVALID_VALUE`、`INVALID_CONFIG`。
- 事务与数据：`UNKNOWN_TRANSACTION`、`TRANSACTION_NOT_STARTED`、`TRANSACTION_CLOSED`、`ROW_NOT_FOUND`、`ROW_ALREADY_EXISTS`、`WRITE_CONFLICT`。
- 快照与摘要：`STALE_SNAPSHOT`、`UNKNOWN_READ_VIEW`、`VERSION_DIGEST_MISMATCH`、`SNAPSHOT_DIGEST_MISMATCH`。
- 时钟与预算：`CLOCK_REGRESSION`、`QUERY_LATENCY_BUDGET_EXCEEDED`。
- 幂等与恢复：`IDEMPOTENCY_CONFLICT`、`RECOVERY_CONFLICT`、`DATABASE_NOT_INITIALIZED`、`DATABASE_EXISTS`、`SESSION_MISMATCH`。

完整错误码定义见 `mvcc_app/errors.py`。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用临时数据库，覆盖：

- 输入校验和“非法输入不创建数据库”。
- RR/RC 可见性差异、同事务多写合并、回滚、删除重建、丢失更新拒绝。
- GC 保留视图引用、GC 与查询并发、NumPy 确定性比对。
- 候选代中断、半写入版本清理、重复 `resume` 幂等。
- 4 个并发进程提交同一事务，只产生一份提交与一个版本。
- 未知事务、时钟回拨、过期快照、查询预算、版本摘要不匹配。

## 项目结构

```text
mvcc_app/
  cli.py          CLI 与稳定 JSON 输出
  engine.py       MVCC、读视图、范围读、GC、恢复重放
  store.py        SQLite 代快照、原子指针切换、完整性校验
  validation.py   表/会话/配置校验与规范化
  errors.py       稳定业务错误码
samples/          本地确定性样本
tests/            单元与多进程/并发测试
TECHNICAL.md      设计说明
VERIFICATION.md   实际验证记录
```

