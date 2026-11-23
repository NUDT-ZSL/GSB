# SQL 逻辑计划编辑与失效引擎

这是一个完全本地运行的 SQL 视图规划工具。它把本地 JSON 视图定义解析为确定性的逻辑计划，合并 v1/v2 两代快照，并在新增、修改、删除视图后计算计划、列引用解析和可执行 SQL 的依赖失效闭包。状态保存在 SQLite 中，写入由跨平台文件锁串行化，并通过 `prepared -> staged -> committed` 两阶段提交支持强制终止后的恢复。

不依赖网络服务、真实数据库、Docker 或外部账号。

## 快速开始

目标环境为 Windows + Python 3.11/3.12。代码同时兼容 Linux，已在 Python 3.12.13 和 3.10.12 上验证。

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python -m sqlplan load
python -m sqlplan status
python -m sqlplan plan --view user_totals
python -m sqlplan edit patches/01_modify_paid_amount.json
python -m sqlplan verify
```

Linux/macOS 将激活命令替换为 `source .venv/bin/activate` 即可。也可以执行 `pip install -e .` 后使用 `sqlplan` 命令。

默认路径：

- 状态库：`runtime/state.db`
- 文件锁：`runtime/app.lock`
- 日志：`runtime/logs/sqlplan.jsonl`
- 样本：`views.json`、`snapshots/`、`patches/`

所有命令均支持 `--db`、`--lock`、`--logs`、`--lock-timeout`，可放在子命令前后。`load`/`verify` 还支持 `--views`、`--v1`、`--v2-dir`、`--v2-meta`、`--base-clock`。

## 交付目录

```text
views.json                         初始视图集合
snapshots/v1.json                  v1 单文件、扁平列映射、相对生成秒数
snapshots/v2/shard-*.json          v2 分片
snapshots/v2/v2_meta.db            v2 分片清单与墓碑元数据
patches/*.json                     编辑补丁样本
sqlplan/                           应用实现
tests/test_app.py                  自动化验收测试
```

样本可通过以下命令确定性重建：

```bash
python -m sqlplan.sample
```

## 数据格式

### 视图集合 `views.json`

```json
{
  "format": "sqlplan/views/v1",
  "logical_clock_ms": 1700000004000,
  "views": [
    {
      "name": "users",
      "sql": "SELECT id, name, country FROM raw_users",
      "upstream": [],
      "logical_clock_ms": 1700000000000,
      "content_digest": "sha256:..."
    }
  ]
}
```

`content_digest` 的计算对象是规范化后的 `name`、`sql`、`upstream`、`logical_clock_ms`。摘要格式为 `sha256:<64 hex>`，加载与编辑时都会复核。

### v1 快照

v1 是单文件快照，包含扁平 `view.column` 映射和相对生成秒数：

```json
{
  "format": "sqlplan/snapshot/v1",
  "logical_clock_ms": 1700000005000,
  "plan_generation": 2,
  "generated_at_relative_seconds": 4.25,
  "plans": {
    "users": {"plan_digest": "sha256:...", "logical_clock_ms": 1700000000000}
  },
  "column_map": {
    "users.id": {
      "view": "users",
      "column": "id",
      "ordinal": 1,
      "expression": "id",
      "expression_digest": "sha256:..."
    }
  }
}
```

相对秒数在注入逻辑时钟下换算：

```text
absolute_generated_ms =
    base_logical_clock_ms
    + round_half_up(generated_at_relative_seconds * 1000)
```

换算结果写入合并审计，显式保留 `base_logical_clock_ms`、`scale_ms_per_second=1000`、`rounding=half_up`、`delta_ms` 和 `absolute_generated_ms`。`--base-clock` 未提供时，使用视图集合的逻辑时钟水位。

### v2 快照

每个分片包含绝对毫秒戳、计划代数和视图/列/计划摘要：

```json
{
  "format": "sqlplan/snapshot/v2-shard",
  "plan_generation": 3,
  "logical_clock_ms": 1700000009000,
  "generated_at_ms": 1700000009500,
  "views": [
    {
      "name": "users",
      "logical_clock_ms": 1700000000000,
      "plan_generation": 3,
      "plan_digest": "sha256:...",
      "columns": [],
      "executable_sql": "SELECT ...",
      "tombstone": false
    }
  ]
}
```

`v2_meta.db` 的 `shard_manifest` 表保存文件名、SHA-256、分片计划代数、逻辑时钟、绝对生成时刻和视图数。`tombstones` 表保存视图失效墓碑。加载时要求：

- 清单中的每个分片存在且字节级 SHA-256 一致；
- 没有未登记 JSON 分片；
- 分片内容与 SQLite 元数据中的计划代数、逻辑时钟、生成时刻、视图数一致；
- 按 `seq` 排列的 `plan_generation` 单调不减。

### 编辑补丁

```json
{
  "patch_id": "p-paid-amount-v2",
  "edit_round": 1,
  "logical_clock_ms": 1700000020000,
  "base_plan_generation": 3,
  "plan_generation": 4,
  "operations": [
    {
      "op": "modify",
      "name": "paid_orders",
      "sql": "SELECT ...",
      "upstream": ["users", "orders"],
      "logical_clock_ms": 1700000020000,
      "content_digest": "sha256:..."
    },
    {"op": "delete", "name": "leaf_view"}
  ]
}
```

- `op` 支持 `add`、`modify`、`delete`。
- `base_plan_generation` 和 `plan_generation` 可选；提供时会做并发基线与回退检查。
- `add`/`modify` 可把完整视图放在 `view` 字段，也可平铺字段。
- `modify` 可省略未变化字段；省略 `content_digest` 时按合并后的最终定义计算。
- 同一 `patch_id` 和相同内容重复提交只返回第一次结果，不新增代数或事件；相同 ID 不同内容返回 `E_PATCH_CONFLICT`。
- 补丁先按最终视图集合整体校验，因此可以在同一补丁内协调完成“上游改名 + 下游更新”；整体不可编译时再退化为逐操作隔离，单操作异常只拒绝该操作，已成功操作仍可提交。全部操作失败时不写任何计划状态。

样本补丁顺序：

| 补丁 | 预期 |
| --- | --- |
| `01_modify_paid_amount.json` | 修改被引用 `amount` 表达式，传播到两个下游 |
| `02_add_users_tier.json` | 上游只新增未被引用列，下游计划安全复用 |
| `03_rename_referenced_id_invalid.json` | `id -> user_key` 破坏下游，返回 `E_COLUMN_NOT_FOUND` 且零写入 |
| `04_add_paid_by_day.json` | 新增叶子视图 |
| `05_delete_paid_by_day.json` | 删除叶子视图；应在 04 之后执行 |

`01` 的时钟低于 `02`，因此按 `01 -> 02` 顺序执行；若先执行 `02` 再执行 `01`，会按设计返回 `E_CLOCK_REGRESSION`。

## 逻辑计划

`sqlglot==25.24.5` 把每个 `SELECT` 解析为包含以下节点的树：

- `scan`：数据源、别名、实际引用列；
- `filter`：`WHERE`/`HAVING` 条件及谓词列；
- `join`：连接类型、连接条件、左右连接键；
- `aggregate`：`GROUP BY`、聚合表达式和聚合引用列；
- `project`：输出列、别名、表达式摘要、列解析结果；
- `sort`/`limit`/`subquery`：可选节点。

每个节点有稳定 ID，例如 `paid_orders:join:0`。节点保留 `output_columns`、`predicate_columns`、`join_keys` 与列引用位置。扫描节点只保留该查询实际引用的上游列；只有投影星号才展开完整 schema。这样未引用列变化不会污染下游计划摘要。

计划摘要：

```text
plan_digest = "sha256:" + sha256(canonical_json(plan))
```

规范化 JSON 使用排序键、无多余空白、UTF-8。相同视图集合和 SQL 在任意运行次数下产生一致的逐字段计划、可执行 SQL 和摘要。计划中的列解析引用包含上游输出列的 `expression_digest`，因此失效粒度跟随列级表达式，而非整张上游视图。

## 快照合并

同一视图在 v1/v2 同时存在时，按以下元组选择最新候选：

```text
(logical_clock_ms, plan_generation, source_priority, sequence, source_ref)
source_priority: v2=2, v1=1
```

合并审计为每个视图保留：

- 胜出来源版本、文件/墓碑引用、原始时间表示；
- v1 时间换算记录；
- `decision_basis`；
- 被否决候选及 `rejected_reason`；
- 是否进入统一计划、是否为墓碑。

v2 墓碑在时钟/代数更高时压过旧 v1 计划，不进入统一计划，但保留审计。

快照声明的计划摘要被视为缓存证据。若有效摘要已落后于当前 SQL 的确定性计划，合并审计标记 `snapshot_cache_verdict=recompute_stale` 与 `requires_recompute=true`，加载结果列入 `snapshot_recompute_required` 并重算，而不是拒绝整个快照包；格式非法的摘要仍返回 `E_PLAN_DIGEST_MISMATCH`。

`load` 对已存在状态库进行包级判定：

- 代数更低：`E_GENERATION_ROLLBACK`；
- 代数相同、内容或视图集合不同：`E_GENERATION_CONFLICT`；
- 代数相同且内容一致：`noop`；
- 代数更高且旧视图及计划摘要均为新包子集：`incremental_append`；
- 其他更高代包：`complete_replace`。

## 依赖失效闭包

每次有效编辑产生一个新计划代。引擎沿下列关系计算闭包：

```text
编辑视图
  -> 声明/实际依赖的下游视图
  -> 被修改列的旧解析引用与节点
  -> 计划摘要是否变化
  -> 可执行 SQL 是否重算
```

结果显式输出：

- `recomputed_views` / `reused_views`；
- `recomputed_executable_sql` / `reused_executable_sql`；
- `column_changes`（added/removed/renamed/changed）；
- 每个失效事件的触发来源、传播路径、节点 ID、引用位置；
- `propagation_paths` 汇总。

未受影响计划从旧代复制，数据库中的 `plan_json`、`plan_digest`、`columns_json`、`executable_sql`、`resolution_json` 逐字段不变。

下游解析期缺列会返回独立错误 `E_COLUMN_NOT_FOUND`，包含：

- 下游视图、节点 ID、列名、限定列名、上下文；
- 行、列、字符偏移和 SQL 片段；
- `trigger_view`、`removed_columns`、`renamed_columns`；
- 编辑轮次和操作序号。

不会通过运行期空结果掩盖不可编译视图。

## 持久化、恢复与并发

SQLite 主要表：

- `current_pointer`：唯一当前计划代数指针；
- `plan_generations` / `plans`：按代数追加的完整计划集合；
- `edit_runs`：`prepared/staged/committed` 运行日志及完整重放载荷；
- `invalidation_events`：去重失效事件，状态为 `pending/applied`；
- `snapshot_packages`：导入包内容摘要、裁决和合并审计。

提交流程：

1. 校验、解析、失效计算在内存完成；失败不创建数据库或运行记录。
2. `prepared` 事务持久化完整目标载荷。
3. `staged` 事务在 SQLite 临时表装入完整目标代，再整体写入 `plans`，同时写入唯一的 pending 失效事件。
4. `committed` 事务原子切换 `current_pointer`、标记代和事件、保存结果。

任一步强制退出后：

- `prepared`：从持久化载荷重新 staging；
- `staged`：校验完整计划集后补齐最终化；
- 指针从未指向半提交代，因此不会出现新旧计划混合；
- 事件 ID 确定性去重，不会重复计数。

故障注入通过环境变量 `SQLPLAN_FAULT_POINT`：

```text
after_prepare
before_stage
after_stage
before_finalize
during_finalize
after_commit
```

示例：

```bash
SQLPLAN_FAULT_POINT=after_stage python -m sqlplan edit patches/01_modify_paid_amount.json
python -m sqlplan status
python -m sqlplan resume
```

所有写命令和查询命令使用跨平台文件锁；Linux 使用 `fcntl.flock`，Windows 使用 `msvcrt.locking`。SQLite 使用 `BEGIN IMMEDIATE`、WAL 和完整同步。并发提交同一 `patch_id` 时只有一个进程创建新代，另一个返回幂等结果。

## 命令

| 命令 | 说明 |
| --- | --- |
| `load` | 校验视图和两代快照，合并、裁决并初始化/推进状态 |
| `edit <patch>` | 提交补丁；`-` 从标准输入读取 |
| `plan [--view NAME]` | 输出当前代完整逻辑计划或指定视图计划 |
| `resume` | 重放 prepared 或最终化 staged 运行 |
| `status` | 稳定排序 JSON：当前代数、水位、各视图摘要、待失效事件数 |
| `verify` | 检查 SQLite 完整性、指针计划集、代数单调、重算确定性、内容摘要、待处理运行/事件和样本快照 |

退出码：`0` 成功；`2` 输入/快照错误；`3` 代数或并发基线错误；`4` 视图/列操作错误；`5` 未加载或状态损坏；`6` 锁/恢复冲突；`7` 内部错误。

## 业务错误码

| 错误码 | 含义 |
| --- | --- |
| `E_BAD_JSON` | JSON 不可解析 |
| `E_BAD_VIEW` | 视图字段、名称或摘要格式非法 |
| `E_DUPLICATE_VIEW` | 视图名重复 |
| `E_UPSTREAM_NOT_FOUND` | 声明的上游视图不存在 |
| `E_CYCLE` | 自环或依赖成环 |
| `E_SQL_PARSE` | SQL 语法或语句类型非法 |
| `E_BAD_CLOCK` | 逻辑时钟不是非负整数 |
| `E_CONTENT_DIGEST_MISMATCH` | 视图内容摘要不匹配 |
| `E_PLAN_DIGEST_MISMATCH` | 快照计划/列摘要格式非法 |
| `E_BAD_SNAPSHOT` | 快照结构、列追溯或代数顺序非法 |
| `E_SHARD_HASH_MISMATCH` | v2 分片 SHA-256 不匹配 |
| `E_SNAPSHOT_META_MISMATCH` | v2 分片与 SQLite 元数据不一致 |
| `E_GENERATION_ROLLBACK` | 快照或补丁计划代数回退 |
| `E_GENERATION_CONFLICT` | 同代不同内容 |
| `E_COLUMN_NOT_FOUND` | 引用的上游列不存在 |
| `E_COLUMN_AMBIGUOUS` | 未限定列在多个源中有歧义 |
| `E_VIEW_EXISTS` | 新增视图已存在 |
| `E_VIEW_NOT_FOUND` | 修改/删除/查询视图不存在 |
| `E_VIEW_IN_USE` | 视图仍被下游声明依赖，不能删除 |
| `E_PATCH_CONFLICT` | 相同 patch_id 不同内容 |
| `E_BAD_PATCH` | 补丁结构非法 |
| `E_BASE_GENERATION_MISMATCH` | 补丁基线与当前代不一致 |
| `E_CLOCK_REGRESSION` | 编辑逻辑时钟低于水位 |
| `E_NOT_LOADED` | 尚无已提交状态 |
| `E_LOCK_TIMEOUT` | 等待文件锁超时 |
| `E_RECOVERY_CONFLICT` | 恢复时基线/目标代冲突 |
| `E_STATE_CORRUPT` | SQLite 状态不一致 |
| `E_INTERNAL` | 未处理内部异常 |

所有错误以稳定 JSON 输出，保留上下文诊断字段。

## 验证记录

在仓库根目录执行：

```bash
python -m unittest discover -v
```

实际结果（Python 3.12.13）：

```text
Ran 28 tests in 3.068s
OK
```

同时已在 Python 3.10.12 运行相同 28 项测试，全部通过。

关键实测：

- `load`：初始代 `3`，v2 候选按代数和来源优先级胜出；v1 相对 `4.25s` 换算增量 `4250ms`，标度 `1000`、舍入 `half_up`；v2 墓碑 `legacy_alias` 保留但不进入统一计划。
- `edit 01`：代 `3 -> 4`；重算 `paid_orders/user_totals/daily_totals`，复用 `users/orders`；4 个列解析失效事件定位到下游聚合/投影节点，传播路径为 `paid_orders -> user_totals` 与 `paid_orders -> daily_totals`。
- `edit 02`：只重算 `users`；新增未被引用的 `tier` 列不使 4 个下游计划失效；`verify` 仍通过。
- `edit 03`：返回 `E_COLUMN_NOT_FOUND`，报告 `paid_orders:join:0`、`u.id`、行列位置和 `id -> user_key`；指针、运行数和事件数均不变。
- 强制退出 `after_stage`：指针保持旧代，pending 失效事件为 `10`，运行状态为 `staged`；`resume` 后代推进、pending 归零、事件只计一次。
- 两个进程并发提交同一补丁：一个成功提交，另一个返回 `duplicate=true`；目标代只有一份计划集和一份事件集。
- 协调补丁同时把 `users.id` 改为 `user_key` 并更新 `paid_orders` 的联接条件，最终状态整体校验通过；而只改上游、不改下游时返回 `E_COLUMN_NOT_FOUND`。

完整本地验收：

```bash
python -m sqlplan.sample
python -m sqlplan --db runtime/acceptance.db --lock runtime/acceptance.lock --logs runtime/acceptance-logs load
python -m sqlplan --db runtime/acceptance.db --lock runtime/acceptance.lock --logs runtime/acceptance-logs edit patches/01_modify_paid_amount.json
python -m sqlplan --db runtime/acceptance.db --lock runtime/acceptance.lock --logs runtime/acceptance-logs status
python -m sqlplan --db runtime/acceptance.db --lock runtime/acceptance.lock --logs runtime/acceptance-logs verify
```

## 实现边界

- 逻辑计划以 SQLite 方言解析 `SELECT` 查询，支持常见扫描、过滤、投影、聚合、联接、排序、限制与简单 FROM 子查询。
- 未声明为视图的 SQL 表名视为物理源；视图间依赖必须显式写入 `upstream`。
- 删除仍被下游声明的单个视图会返回 `E_VIEW_IN_USE`；同一补丁同时删除整条依赖链时，会按最终视图集合整体校验。
- 不使用真实数据库执行查询；“可执行 SQL”是规范化、可落地的视图查询文本，不代表已在外部数据库物化。
