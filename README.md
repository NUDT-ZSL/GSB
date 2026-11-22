# 本地分库分表路由与查询改写组件

本组件只依赖 Python 标准库 `sqlite3` 与文件锁，在本地确定性样本上完成：

- v1 单 JSON 快照与 v2 分片 JSON + SQLite 元数据库的解析、校验和统一建模；
- 等值、范围、IN、无分片键全路由四类逻辑查询的路由与 SQL 改写；
- 跨分片 SQLite 执行、固定顺序整数聚合、成功/部分成功/失败/主动放弃结局；
- 规则版本原子热切换、进行中查询版本绑定、回滚拒绝与未来时钟排队；
- 路由、改写、执行、合并四阶段受控异常注入；
- 查询、分片结局、提交摘要的 SQLite 事务持久化和强制终止后的幂等恢复；
- 多进程并发、重复提交幂等、稳定 `status` JSON；
- `load`、`route`、`query`、`apply`、`resume`、`status`、`verify` 七个子命令。

## 环境与安装

要求 Python 3.10+。项目已在 `.venv` 中安装 pytest：

```bash
python3 -m virtualenv .venv
.venv/bin/python -m pip install pytest
```

若系统自带 `venv` 且 `ensurepip` 可用，也可以使用 `python3 -m venv .venv`。

## 快速开始

```bash
# 1. 校验全部样本并初始化 8 个分片 SQLite 文件及持久化状态库
.venv/bin/python -m shard_router load --data-dir data

# 2. 只看 v1 或 v2 快照的独立路由结果
.venv/bin/python -m shard_router route \
  --data-dir data --source v1 \
  --template ledger_by_user --params '{"user_id":5}'

.venv/bin/python -m shard_router route \
  --data-dir data --source v2 \
  --template ledger_by_user --params '{"user_id":5}'

# 3. 跨分片查询与整数聚合
.venv/bin/python -m shard_router query \
  --data-dir data \
  --template ledger_range --params '{"low":2,"high":9}'

# 4. 当前时钟为 1,000,000ms。将 v2 未来规则入队
.venv/bin/python -m shard_router apply --data-dir data

# 5. 将逻辑时钟推进到 2,000,000ms，原子切换 rule-v2
.venv/bin/python -m shard_router apply --data-dir data --now-ms 2000000

# 6. 稳定状态与一致性复核
.venv/bin/python -m shard_router status --data-dir data
.venv/bin/python -m shard_router verify --data-dir data

# 7. 全量 pytest
.venv/bin/python -m pytest -q
```

`load` 会重建样本分片和状态库，是确定性复现入口。所有命令成功时输出 `ok: true`；业务失败时输出稳定错误 JSON，并以对应业务退出码结束。

## 目录与数据契约

```text
shard_router/                 组件实现
tools/build_v2_meta.py        重建 v2 哈希链元数据库
data/logical_tables.json      逻辑表、列、分片键、规则类型、生成列
data/physical_mapping.json    逻辑表到本地 SQLite 文件/物理表/主备标识
data/rules/v1.json            v1 单文件快照
data/rules/v2/*.json          v2 分片规则内容
data/rules/v2/meta.sqlite     v2 元数据库（摘要、时钟、代数、前序摘要）
data/templates.json           四类逻辑查询模板
data/seed_data.json           确定性种子行
data/injections.json          默认注入清单（全部 disabled）
data/injections/*.json        可复现的启用注入夹具
tests/test_shard_router.py    端到端与并发/中断测试
```

### 逻辑表

关键字段：`logical_table`、`shard_key`、`rule_type`（`modulo`/`range`）、`shard_count`、`columns`、`generated_columns_enabled`。范围表可声明 `shard_key_min`、`shard_key_max` 与 `range_boundaries`。

加载时会校验：

- 逻辑表名唯一，分片键列存在于表结构；
- 取模分片数为正整数；范围边界长度为 `shard_count + 1`、严格递增且不越界；
- 生成列声明与开关一致；
- 每个逻辑表的物理映射完整覆盖 `0..shard_count-1`，文件可被 SQLite 打开；
- 主备标识合法且至少有一个 primary；
- 种子行只引用已知列、不写生成列、分片键为整数。

任何校验失败均在写分片文件或状态库前终止，并返回明确业务码。

### v1 快照

v1 规则使用 `relative_effective_seconds`。换算记录在规则裁决证据中：

```text
effective_clock_ms = logical_clock_base_ms
                   + relative_effective_seconds * clock_scale_ms
rounding = floor
```

样本中的换算是：

```text
1,000,000ms + 0s * 1,000ms/s = 1,000,000ms
```

### v2 快照

`meta.sqlite` 的 `v2_rules` 表包含：

```text
version, content_digest, effective_clock_ms, generation,
rollback_target, shard_file, source_priority, previous_digest
```

加载时会：

- 对分片 JSON 的规范化内容计算 SHA-256，并与 `content_digest` 比较；
- 校验逻辑时钟为整数、分片代数为正整数；
- 按时钟/代数排序后要求代数严格递增；
- 校验 `previous_digest` 摘要链连续；
- 校验 JSON 内版本、时钟、代数与元数据一致。

`tools/build_v2_meta.py` 可从分片 JSON 重建该元数据库。

### 两代规则合并

同一版本同时出现在 v1/v2 时，按以下顺序裁决胜者：

1. `effective_clock_ms` 较大者；
2. `generation` 较大者；
3. `source_priority` 较大者。

每个合并后版本保留全部候选来源、摘要、时钟、代数、换算方式与裁决原因。样本中 `rule-v1` 同为 1,000,000ms/generation 1 时，v2 的优先级 20 高于 v1 的 10，因此合并模型选择 v2 来源；两代单独装载时，同一查询的目标分片集合仍一致。

### 查询模板

模板必须是一个只读 `SELECT`/`WITH` 语句，支持：

- `equal`：`WHERE shard_key = :param`；
- `range`：`WHERE shard_key BETWEEN :low AND :high`；
- `in`：`WHERE shard_key IN (:p0, :p1, ...)`；
- `all`：无分片键谓词，计划中显式输出 `all_route: true`。

改写计划为每个目标分片保存：

- 命中的规则版本、来源、摘要、分片号；
- 数据库相对/绝对路径、物理表名；
- 可独立绑定执行的物理 SQL 与参数；
- 改写前后分片键谓词、是否裁剪、裁剪原因；
- 语句摘要与整体计划摘要。

范围规则会把谓词裁剪到分片区间；IN 列表会按目标分片过滤；取模范围保留原范围谓词，因为余数类不连续，但仍只向可能命中的分片分发。

## 执行、聚合与异常结局

- 成功分片按 `shard_no` 升序合并；
- 结果行按模板 `order_by` 全局稳定排序；
- 计数使用返回行数，求和使用 Python 整数逐项累加；
- 每个成功分片保存行、行数、整数和、结果摘要；
- 失败分片保存业务码、阶段字段、错误 JSON 与原始异常；
- `abandoned` 与 `failed` 是不同终态；
- 有成功且有失败/放弃时为 `partial`；全部失败时为整体失败。

样本期望：

```text
ledger_by_user(user_id=5)        -> shard [1], count 1, sum 600
ledger_range(low=2, high=9)      -> shard [0,1,2,3], count 6, sum 4000
ledger_in(0,5,10,3)              -> shard [0,1,2,3], count 4, sum 2200
ledger_all                       -> shard [0,1,2,3], count 12, sum 7800
event_range(low=250, high=650)   -> shard [0,1,2], count 4, sum 1800
```

## 热更新与版本绑定

- `apply` 在一个 SQLite `BEGIN IMMEDIATE` 事务内更新规则版本记录与 `current_rule` 单指针；
- 查询开始时在同一事务中读取指针并把完整规则模型写入查询审计记录，后续改写/执行只使用该绑定；
- 未来生效时钟规则进入 `queued`，查询或 `apply --now-ms` 推进时钟后可原子提升；
- 同版本不同摘要返回 `E_VERSION_CONFLICT`；
- 回滚目标、旧时钟、旧代数或排序更低的候选返回 `E_ROLLBACK_REJECTED`；
- 已生效后更晚时钟但更小代数的候选返回 `E_ROLLBACK_REJECTED`；
- 所有排队、拒绝、提升、加载决定写入 `rule_decisions` 并包含原因。

## 持久化与恢复

状态库为 `data/state/meta.sqlite`，启用 WAL、`synchronous=FULL`、外键与 30s busy timeout。主要表：

- `rule_versions` / `current_rule` / `rule_decisions`；
- `query_runs`（开始时钟、绑定规则、计划、状态、水位）；
- `shard_runs`（逐分片终态、行、摘要、错误）；
- `query_commits`（唯一查询提交结果与摘要）；
- `audit_log`；
- `logical_tables` / `physical_mappings`。

生命周期：

1. 先提交 `routing` 查询与绑定规则；
2. 改写计划与 pending 分片任务原子提交；
3. 每个分片终态独立原子提交，成功分片不再重跑；
4. 合并时从已持久化分片记录重新计算，单事务提交 `query_commits` 与 `committed` 状态。

若在改写时终止，恢复会从绑定规则重新生成相同计划；若在执行时终止，恢复只处理 `pending` 分片；若在合并提交前终止，恢复重算但只提交一次。进程级文件锁保护 `load`/`apply` 过渡，SQLite 事务保护查询与规则指针并发。

## 受控异常注入

注入清单字段：

```json
{
  "stage": "route|rewrite|execute|merge",
  "template_id": "ledger_range",
  "query_id": null,
  "shard_no": 2,
  "error_type": "physical_table_missing",
  "code": "E_PHYSICAL_TABLE_MISSING",
  "action": "raise|fail_shard|abandon_shard|corrupt_digest|crash|delay|none",
  "params": {"low": 2},
  "delay_ms": 0,
  "position": "before|after_persist"
}
```

可直接使用的夹具：

| 文件 | 阶段/结局 |
|---|---|
| `data/injections/route-conflict.json` | 路由规则版本冲突 |
| `data/injections/rewrite-out-of-range.json` | 改写分片键越界 |
| `data/injections/execute-fail-and-abandon.json` | 执行缺表 + 主动放弃 |
| `data/injections/merge-digest-mismatch.json` | 合并摘要不匹配并追溯到执行分片 |
| `data/injections/crash-execute-shard-2.json` | 第 2 分片前强制退出 |
| `data/injections/crash-rewrite.json` | 改写中强制退出 |
| `data/injections/pause-execute.json` | 执行延迟，用于并发热更新绑定验证 |

业务错误码与退出码：

| 退出码 | 错误码示例 | 含义 |
|---|---|---|
| 2 | `E_VALIDATION`、`E_BOUNDARIES_INVALID`、`E_V2_DIGEST_MISMATCH` | 输入/样本校验失败 |
| 3 | `E_STATE_NOT_LOADED`、`E_QUERY_NOT_FOUND` | 状态不存在或查询不存在 |
| 4 | `E_VERSION_CONFLICT`、`E_ROLLBACK_REJECTED`、`E_RULE_VERSION_CONFLICT` | 规则版本冲突/回滚拒绝 |
| 5 | `E_SHARD_KEY_OUT_OF_RANGE`、`E_PHYSICAL_TABLE_MISSING`、`E_SHARD_RESULT_DIGEST_MISMATCH`、`E_VERIFY_FAILED` | 路由/执行/合并/复核失败 |
| 6–9 | `E_INTERNAL` 等 | 未预期内部问题 |

## 实际验证结果

验证环境：

```text
Python 3.10.12
SQLite 3.37.2
pytest 9.2.0
```

执行命令与结果：

```text
$ .venv/bin/python -m pytest
....................                                                 [100%]
20 passed in 12.77s
```

覆盖并通过的验收点：

- 非法逻辑表/边界/映射/v2 摘要均在落盘前返回业务码，`data/state` 与分片 SQLite 未创建；
- v1、v2 分别独立加载后，同一模板目标分片集合与聚合行一致；
- 范围模板实际裁剪为 `event_id BETWEEN :__sr_0_low AND :__sr_0_high`（250..299）；
- 等值单分片、范围全分片、IN 过滤、全路由的聚合值符合样本期望；
- 相同查询重复执行输出逐字段一致，分片 attempt 保持 1，提交只有一次；
- 未来规则先排队，推进逻辑时钟后指针从 `rule-v1` 原子切到 `rule-v2`（pointer_seq 2）；
- 热更新期间暂停的进行中查询仍输出 `rule-v1`，更新后新查询使用 `rule-v2`；
- 旧版本回退返回 `E_ROLLBACK_REJECTED`，并持久化拒绝原因；
- 路由、改写、执行、合并四阶段注入分别得到对应业务码和模板/规则/分片/阶段字段；
- 执行失败与主动放弃在同一 partial 结果中分别标记为 `failed`、`abandoned`；
- 全部分片失败返回整体 `E_EXECUTION_FAILED`，且不写查询提交记录；
- 合并摘要不匹配可追溯至 `execute` 阶段的具体成功分片；
- 改写中与执行中强制退出后，`resume` 不重跑成功分片，恢复输出与未中断基线逐字段一致；
- 两查询进程并发提交同一 query id 时输出一致，`query_commits` 仅一条；
- `status` 连续调用逐字节一致，`verify` 校验两代快照路由、当前指针、提交摘要与聚合。

重建 v2 元数据库：

```bash
.venv/bin/python tools/build_v2_meta.py data/rules/v2
```
