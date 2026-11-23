# WALCP

WALCP 是一个面向本地数据库检查点与 WAL 回放领域的确定性命令行应用。它不连接任何真实数据库服务，只使用 Python 标准库 JSON/SQLite 和本地文件，实现：

- WAL 段与 v1/v2 两代检查点的严格解析、摘要校验和错误码分流
- v1 相对时间换算、v2 分片校验、同代跨格式裁决与统一回放基线
- 从检查点出发的增量回放、逐行版本号/来源事务/删除墓碑维护
- 注入逻辑时钟下的毫秒级延迟记录、时钟回拨和提交前回放拒绝
- WAL 行修改/删除后的“行版本—事务—检查点—延迟”失效闭包
- v2 检查点创建、临时指针表整体替换、增量追加/完整替换/无操作裁决
- SQLite 两阶段事务、操作日志、强制终止恢复、文件锁与多进程幂等
- `load`、`apply`、`edit`、`checkpoint`、`resume`、`status`、`verify`

## 环境

本次实际执行环境：

```text
Python 3.10.12
SQLite 3.37.2
NumPy 1.26.4
Linux (WSL)
```

代码只依赖 Python 标准库，NumPy 已按任务准备步骤安装，但不参与核心状态机。`pyproject.toml` 声明 Python 3.10+，可直接运行于 Python 3.11/3.12。Windows 10/11 的 Python 3.11/3.12 可按同样命令运行；文件锁已提供 `msvcrt.locking` 分支，SQLite 路径均通过 `pathlib` 处理。本次环境没有 Windows 实例，因此 Windows 分支已实现但未在真实 Windows 主机上复跑。

## 快速开始

```bash
python -m pip install --upgrade pip
python -m pip install numpy
python -c "import sqlite3, numpy; print(sqlite3.sqlite_version, numpy.__version__)"

python tools/generate_samples.py
python -m walcp load --db state.db --samples samples \
  --now-ms 1700000500000 --anchor-ms 1700000100000
python -m walcp apply --db state.db --now-ms 1700000500000
python -m walcp verify --db state.db
python -m walcp status --db state.db
```

`load` 先完成全部解析与校验，再打开/写入 SQLite。非法 WAL、断链、摘要不符等场景不会创建或修改领域状态库。

## 确定性样本

`samples/` 由 `tools/generate_samples.py` 生成：

- `wal/wal-000001.jsonl` 至 `wal-000003.jsonl`：事务 1..22，跨段严格递增
- `checkpoints/v1/cp-0001-v1.json`：代数 1，水位 8，相对保存 20.0 秒
- `checkpoints/v2/metadata.sqlite` + `shards/*.json`：代数 1、2，分片哈希和回放代数 10、20
- 代数 1 同时存在于 v1/v2，v1 锚点默认 1700000100000 ms；v2 绝对时钟更晚，因此 v2 胜出
- 代数 2 水位 14；增量回放事务 15..22；共 9 个当前行/墓碑

WAL 行字段：

```json
{
  "txn_id": 15,
  "table": "accounts",
  "key": "acct-erin",
  "op": "insert",
  "before": null,
  "after": {"balance": 40},
  "checkpoint_gen": 2,
  "content_hash": "sha256:...",
  "commit_ms": 1700000015000
}
```

`content_hash` 对 `txn_id/table/key/op/before/after/checkpoint_gen/commit_ms` 的规范 JSON（键排序、紧凑分隔符、UTF-8）做 SHA-256。一个 WAL 行对应一个事务。

## 命令

所有命令输出稳定排序 JSON。成功退出码 0；业务错误退出码 2；`verify` 不一致退出码 3。

```bash
# 加载、合并检查点、写入基线
python -m walcp load --db state.db --samples samples \
  --now-ms 1700000500000 --anchor-ms 1700000100000

# 逐事务增量回放；clock-step 为每个事务递增的注入逻辑时钟
python -m walcp apply --db state.db --now-ms 1700000500000 --clock-step-ms 0

# 修改 WAL 行；op/table/key 可写裸值，其他字段必须是 JSON
python -m walcp edit --db state.db --txn-id 18 \
  --set 'after={"balance":32}' --now-ms 1700000600000
python -m walcp edit --db state.db --txn-id 22 --delete --now-ms 1700000601000

# 创建、接收、列出 v2 检查点包
python -m walcp checkpoint create --db state.db --out out/gen3 \
  --now-ms 1700000700000
python -m walcp checkpoint create --db state.db --out out/full4 --full \
  --now-ms 1700000800000
python -m walcp checkpoint receive --db state.db --package out/gen3 \
  --now-ms 1700000700000
python -m walcp checkpoint list --db state.db

# 强制终止后的恢复；只补缺失事务，不重复写延迟
python -m walcp resume --db state.db --now-ms 1700000700000

# 稳定状态与全量对照验证
python -m walcp status --db state.db
python -m walcp verify --db state.db
```

默认逻辑时钟为 `1700000500000`，默认 v1 锚点为 `1700000100000`；显式传入可避免部署时的隐式假定。`--clock-tolerance-ms` 控制允许的回拨容差。

## 校验与错误码

| 错误码 | 含义 |
| --- | --- |
| `WAL_INVALID_ROW` | JSON 行结构、枚举、镜像类型/组合非法 |
| `WAL_CHAIN_BROKEN` | 段号、段内事务序、跨段事务序断裂 |
| `WAL_HASH_MISMATCH` | WAL 行内容摘要不符 |
| `WAL_CHECKPOINT_DEPENDENCY` | WAL 依赖的检查点代数不在链中 |
| `CHECKPOINT_INVALID` | v1/v2 结构、摘要、快照声明非法 |
| `CHECKPOINT_HASH_MISMATCH` | v2 分片或清单哈希不符 |
| `CHECKPOINT_CHAIN_BROKEN` | 检查点代际、水位、回放代数或清单版本回退 |
| `CHECKPOINT_ROLLBACK` | 收到低于当前指针的检查点包 |
| `CHECKPOINT_CONFLICT` | 同代不同清单、路径已被提交、非增量关系 |
| `CLOCK_ROLLBACK` | 注入逻辑时钟回拨超过容差 |
| `CLOCK_BEFORE_COMMIT` | 回放时刻早于事务提交时刻 |
| `EDIT_TARGET_NOT_FOUND` | 编辑目标不存在或已删除 |
| `UNSUPPORTED_EDIT` | 编辑字段/值不受支持 |
| `REPLAY_ROW_MISSING` | update/delete 在回放时找不到活行 |
| `REPLAY_INCONSISTENCY` | 行版本、来源事务或增量/全量结果冲突 |
| `STATE_NOT_INITIALIZED` / `STATE_STALE` | 未加载或失效编辑尚未重建 |
| `RECOVERY_CONFLICT` | 恢复状态无法安全继续 |

错误输出示例：

```json
{
  "ok": false,
  "error": {
    "code": "WAL_HASH_MISMATCH",
    "message": "WAL content hash does not match the row content",
    "details": {"segment": "wal-000001.jsonl", "line_no": 1, "txn_id": 1}
  }
}
```

## 检查点裁决

同一代数有多个来源时按以下顺序选择：

1. 声明的绝对逻辑时钟（v1 先换算）
2. 回放代数
3. 来源格式优先级：v2 > v1

每个候选版本均保留在 `_checkpoint_versions`，包含来源路径、来源哈希、裁决策略、候选列表、v1 换算参数和是否选中。v1 换算结果包含：

```json
{
  "raw_relative_seconds": 20.0,
  "scale_ms_per_unit": 1000,
  "rounding": "half_up",
  "rounding_direction": "nearest_ties_away_from_zero",
  "clock_anchor_ms": 1700000100000,
  "offset_ms": 20000,
  "absolute_saved_at_ms": 1700000120000
}
```

若检查点格式只有摘要而没有行快照，`load` 会从 WAL 前缀确定性地物化基线，并再次核对表行数和内容摘要。无法物化或摘要不符时返回 `CHECKPOINT_INVALID`。

## 行版本、延迟与编辑闭包

SQLite 表 `_rows` 保存当前行版本，字段包括 `table_name/row_key/version/txn_id/deleted/value_json`；`_row_history` 保存版本历史。删除操作保留墓碑和删除前镜像。

每个事务的延迟为：

```text
latency_ms = replay_ms - commit_ms
precision_ms = 1
rounding = millisecond_exact
```

`commit_ms` 来自 WAL，`replay_ms` 来自注入逻辑时钟；默认同一批回放使用同一逻辑时刻，也可用 `--clock-step-ms` 构造确定性序列。

编辑闭包沿以下引用计算：

```text
WAL 行 -> 事务 -> 检查点代数 -> 回放行版本 -> 延迟记录
```

- 目标及同一 `(table,key)` 的下游行版本标记 `recompute`
- 覆盖目标事务水位的检查点引用标记 `invalidate`
- 删除事务或修改 `commit_ms` 时，该事务延迟标记 `recompute`
- 仅值变化时，目标及下游的提交/回放时刻未变，延迟标记 `reuse`
- 每个否决复用的项都写入 `reuse_denials`，含触发来源与原因
- 未受影响事务的 `_delays` 行不被改写；重建时直接复用原记录

编辑先做全文校验，非法编辑不会写 WAL、失效清单或事务状态。

## 检查点创建与接收

`checkpoint create`：

- 从当前 `_rows` 生成完整快照、v2 分片和 `metadata.sqlite`
- 清单版本号、检查点代数、回放代数单调递增
- 使用 SQLite 临时表 `checkpoint_pointer_stage`，在单个 `BEGIN IMMEDIATE` 事务内整体替换 `_checkpoint_pointer`
- 同一输出路径已经被提交过时拒绝覆盖

`checkpoint receive` 的裁决：

- 同代同清单哈希：`noop`
- 同代不同清单：`CHECKPOINT_CONFLICT`
- 代数/清单版本更低：`CHECKPOINT_ROLLBACK`
- 基代和基清单哈希等于当前指针且水位/回放代数前进：`incremental_append`
- 基代为 0 或包内包含当前清单：`full_replace`
- 所有分片均重新校验哈希，接收前还校验快照与本地行状态

## 恢复与并发

每个回放事务分两阶段：

1. 提交 `_txn(status='pending')` 登记与 `_operations(status='pending')`
2. 在一个 SQLite 事务内应用行变更、延迟、`_txn(committed)`、水位

若进程在阶段 2 提交前被强杀，SQLite 回滚行/延迟/水位，但保留 pending 登记；`resume` 识别 pending 操作，只补缺失事务。`edit`/`checkpoint` 的 pending 操作在领域事务未提交时被判定为“无半状态并中止”。

并发路径同时使用：

- 跨平台工作区文件锁：Unix `flock`，Windows `msvcrt.locking`
- SQLite `BEGIN IMMEDIATE`、WAL journal、`synchronous=FULL`
- `_txn`/`_delays` 主键、检查点代数唯一提交检查、指针单行约束

重复执行 `apply` 不做行更新、不新增延迟；并发实例中同一事务和同一检查点版本只成功一次。

验证用故障注入仅通过环境变量启用：

```text
WALCP_FAULT_POINT=apply_after_pending|apply_after_rows|rebuild_after_reset|edit_before_commit|checkpoint_before_commit
WALCP_FAULT_TXN=18
```

命中时进程以 75 强制退出，用于验证恢复，不参与正常业务逻辑。

## 实际验证记录

执行：

```bash
python -m unittest discover -s tests -v
```

实际结果：

```text
Ran 22 tests in 2.219s
OK
```

覆盖项：

- v1/v2 同代仲裁、v1 半入舍入、摘要型 v1 基线物化
- 检查点增量回放与空库全量回放逐行逐值一致（9 行/墓碑）
- 重复 `apply` 零增量，`status` 稳定逐字相同
- 编辑事务 18 精确失效行版本 `(txn=18, version=2)`、`(txn=22, version=3)`，复用延迟 15..22
- 编辑事务 12 否决检查点代数 2，回退到代数 1，重建后仍与全量一致
- 检查点创建代数 3、重复接收 `noop`、旧包 `CHECKPOINT_ROLLBACK`
- 增量接收 `incremental_append`、完整替换 `full_replace`
- `apply_after_rows` 在事务 18 强杀后水位 17、pending=18；`resume` 后水位 22
- 编辑、检查点提交前强杀均无半领域状态；恢复后行表与不中断运行逐行一致
- 4 个并行 `apply` 进程最终 22 个事务、8 条延迟且无重复
- 非法行/断链/摘要不符/依赖缺失/v2 分片哈希/回放代数回退均返回独立错误码
- `tools/generate_samples.py` 连续两次生成的全部 11 个样本文件 SHA-256 完全一致

## 代码结构

```text
walcp/
  checkpoints.py   v1/v2 解析、分片校验、同代合并
  engine.py        回放、编辑闭包、检查点、恢复、验证
  errors.py        业务错误码
  models.py        WAL/检查点/快照模型
  package.py       v2 包生成与清单/分片哈希
  store.py         SQLite schema、事务、指针、状态查询
  util.py          规范 JSON、SHA-256、跨平台文件锁、v1 时间换算
  wal.py           WAL 解析、顺序/哈希/镜像/依赖校验
  cli.py           七个命令与稳定 JSON 输出
tools/generate_samples.py
tests/
samples/
```

## 约束说明

- 一个 WAL 行视为一个事务；事务在段内及跨段严格递增，编辑删除后可保留数字间隙，但不得回退或重叠。
- `update`/`delete` 回放要求目标行存在且未删除。删除上游插入事务时，应同时编辑其下游删除/更新，否则 `apply` 返回 `REPLAY_ROW_MISSING`。
- 全量验证要求提供的 WAL 链覆盖从空库到检查点的前缀；若样本只含检查点摘要，则先由 WAL 前缀物化并校验摘要。
- 所有领域时间值均来自 `--now-ms`/`--clock-step-ms` 或样本中的显式毫秒字段，不读取系统时间。
