# GSB SQL 逻辑计划管线与结果验证工具

这是一个可在 Linux / WSL Ubuntu 本地独立运行的数据处理管线。它读取固定种子生成的 v1/v2 逻辑计划快照和补丁事件流，完成 SQL 计划解析、跨版本合并、列级依赖失效、事务化快照替换、中断恢复与多进程幂等提交。

项目不依赖真实数据库、外部服务或网络。运行期只使用 Python 标准库与 `sqlparse`。

## 交付物

- `gsb_pipeline/`：核心库与 `load/parse/merge/patch/resume/status/verify` CLI。
- `scripts/generate_sample.py`：固定种子样本生成器。
- `sample/`：已生成的确定性验收样本。
- `tests/test_pipeline.py`：端到端与恢复/并发测试。
- `pyproject.toml`、`requirements.txt`：Python 包与依赖声明。

## 快速开始

```bash
python3 -m pip install -r requirements.txt

# 已提供 sample/，也可用固定种子重新生成
python3 scripts/generate_sample.py --out sample

# 校验两代快照、解析计划、合并并初始化 SQLite 状态
python3 -m gsb_pipeline load \
  --sample-dir sample \
  --state-db sample/pipeline_state.db

# 查看解析结果与统一计划
python3 -m gsb_pipeline parse --sample-dir sample
python3 -m gsb_pipeline merge --sample-dir sample

# 应用完整补丁流
python3 -m gsb_pipeline patch \
  --state-db sample/pipeline_state.db \
  --events-file sample/patch_events.jsonl

# 状态、完整性和恢复
python3 -m gsb_pipeline status --state-db sample/pipeline_state.db
python3 -m gsb_pipeline verify --state-db sample/pipeline_state.db
python3 -m gsb_pipeline resume --state-db sample/pipeline_state.db
```

所有命令输出 UTF-8、键排序的 JSON。业务失败输出 `{"ok": false, "error": {...}}`，退出码为 `2`；`verify` 发现不一致时输出完整检查项并以 `2` 退出。

## 命令行

| 命令 | 作用 |
| --- | --- |
| `load` | 校验 `view_definitions.json`、v1 快照、v2 分片及元数据库，完成合并并创建初始代数 1 状态 |
| `parse` | 将定义集合转换为稳定逻辑计划树；可用 `--view NAME` 只输出一个视图 |
| `merge` | 按逻辑时钟、来源优先级和计划代数裁决统一计划，输出来源、原始时间、换算信息和裁决依据 |
| `patch` | 按 JSONL 顺序处理补丁；单个补丁失败只记录该补丁，不撤销其他已提交补丁 |
| `resume` | 查询并完成持久化的 `PREPARED` / `STAGED` 运行 |
| `status` | 读取当前指针、计划代数、时钟水位、失效事件数、脏视图和未完成运行 |
| `verify` | 校验 SQLite 完整性、指针、快照记录哈希、计划树摘要、列契约、脏节点、失效事件和半提交状态 |

常用选项：

```text
load --force
merge --base-clock-ms 1700000000000
patch --event-id patch-0001
patch --from-sequence 5 --clock-tolerance-ms 0
```

## 输入格式

### `view_definitions.json`

```json
{
  "format_version": 1,
  "sources": {
    "orders_raw": ["order_id", "customer_id", "amount", "status"]
  },
  "views": [
    {
      "name": "paid_orders",
      "sql": "SELECT order_id, amount FROM orders_raw WHERE status = 'paid'",
      "upstreams": []
    }
  ]
}
```

- 所有 FROM/JOIN 上游必须位于 `sources` 或 `views`。
- 视图名唯一、非空且为合法标识符。
- SQL 必须是单条 `SELECT`，依赖图检测自环和任意环。
- 上游模式从 `sources` 和按拓扑顺序编译的视图输出列得到。

### `v1_snapshot.json`

```json
{
  "format_version": 1,
  "snapshot_generation": 1,
  "logical_clock_ms": 1700000005000,
  "injected_clock_ms": 1700000000000,
  "views": [
    {
      "name": "paid_orders",
      "sql": "SELECT ...",
      "generated_seconds": 1
    }
  ],
  "column_map": [
    {
      "view": "paid_orders",
      "column": "order_id",
      "upstream_view": "raw_orders",
      "upstream_column": "order_id",
      "generated_seconds": 1
    }
  ]
}
```

v1 视图时间换算为：

```text
absolute_ms = injected_clock_ms + generated_seconds * 1000
```

换算记录固定写入 `scale=1000`、`rounding=floor`。`--base-clock-ms` 可覆盖注入基准。

### `v2_shards/*.json` 与 `sqlite_meta.db`

分片包含规范视图名、计划代数、绝对毫秒、SQL、输出列和列级依赖摘要。SQLite 元数据库包含：

- `meta`：格式、逻辑时钟、当前计划代数、v1 注入基准。
- `shards`：分片文件、SHA-256、计划代数、时钟和生成毫秒。
- `view_meta`：视图代数、时钟、列级依赖摘要、墓碑标记、所属分片。
- `tombstones`：失效墓碑。

加载时重新计算每个分片哈希，并与 `shards.sha256` 比较；分片计划代数必须严格递增，`view_meta` 必须与分片逐项一致。

### `patch_events.jsonl`

```json
{"patch_id":"patch-0001","sequence":1,"logical_clock_ms":1700000008000,"op":"modify","view":"raw_orders","sql":"SELECT ..."}
{"patch_id":"patch-0002","sequence":2,"logical_clock_ms":1700000008100,"op":"add","view":"daily_orders","sql":"SELECT ..."}
{"patch_id":"patch-0003","sequence":3,"logical_clock_ms":1700000008200,"op":"delete","view":"legacy_orders"}
```

支持：

- `op=add/modify/delete/upsert`。
- 修改 SQL 文本。
- `dependencies` 整体替换声明依赖。
- `upstream_add` / `upstream_remove` 增删声明依赖。
- 修改补丁可省略 `sql`，表示沿用当前 SQL。

同一 `patch_id` 载荷相同则幂等；载荷不同返回 `E_PATCH_PAYLOAD_CONFLICT`。同一视图同一 `sequence` 已被其他补丁占用时返回 `E_PATCH_SEQUENCE_CONFLICT`。

## SQL 逻辑计划

计划器以 `sqlparse` 完成语句校验、注释处理和词法边界判断，再以确定的子句分析构造树。每个节点包含：

- `node_id`：`nod_` 加节点规范摘要 SHA-256。
- `type`：`scan`、`filter`、`aggregate`、`join`、`project`。
- `output_columns`、`predicate_columns`、`join_keys`。
- `references`：规范化的 `source.column` 列引用。
- `children` 与 `details`。
- `summary`：规范化节点 JSON 的 SHA-256。

计划顶层还包含 `column_dependencies`、`output_columns`、`sources`、`dependencies`、`plan_summary` 和扁平 `node_index`。同一 SQL 在任意运行次数下产生逐字一致的树和摘要。

字符串字面量和带引号标识符在规范化时保持原内容；仅折叠字面量外空白并移除注释，因此缓存的可执行 SQL 保持原语义。

当前支持单条 SELECT、FROM/JOIN、WHERE、GROUP BY、HAVING、ORDER BY、LIMIT 及常见表达式。CTE、集合运算、子查询等会以 `E_SQL_UNSUPPORTED` 明确拒绝，而不是静默生成错误计划。

## 跨版本合并

每个视图的所有 v1/v2 候选按以下稳定键裁决：

```text
(logical_clock_ms, source_priority, plan_generation, source_order)
```

- 逻辑时钟优先。
- 时钟相同时 `v2` 优先于 `v1`。
- 同来源、同时钟时计划代数更高者优先。
- 最新候选是 v2 墓碑时，视图从活动集合移除并进入 `tombstones`。

合并结果保留 `source_version`、`source_plan_generation`、`absolute_time_ms`、`original_time`、v1 换算记录、候选对比和 `decision.reason`。选定 SQL 会在统一上游模式下重新拓扑编译，避免跨版本列契约不一致。

## 列级失效闭包

补丁候选先生成直接编辑视图的新计划，再比较新旧输出契约：

- `removed`：输出列删除或重命名后的旧列。
- `added`：新增输出列。
- `mapping_changed`：输出列名相同，但依赖的上游列变化。

从直接编辑视图出发，在“旧计划”的真实列引用索引上广度优先传播：

- 下游只有引用受影响列的输出列才进入闭包。
- `dirty_nodes` 只包含引用该列的扫描、过滤、聚合、联接或投影节点。
- 未命中的下游视图保留在 `reusable_views`。
- 未受影响视图的 `plan_json` 与 `executable_sql` 从上一代逐字节复制。
- 下游引用的列已删除时，提交前返回 `E_DOWNSTREAM_COLUMN_MISSING`，并输出首个缺失列、节点 ID、补丁序号和 `reference_path`。

成功补丁会把受影响下游行标记为 `stale=1`，并记录 dirty node；未受影响行保持原字节。

## 原子状态与恢复

状态数据库使用 WAL、`BEGIN IMMEDIATE`、`busy_timeout` 和数据库旁的 `fcntl.flock` 文件锁。

初始 `load` 写入计划代数 1。每个成功编辑创建 `current_generation + 1`：

1. 先独立事务持久化 `patch_runs.status=PREPARED` 和完整补丁载荷。
2. 计算候选计划与失效闭包。
3. 在一个事务内把完整目标代数写入连接内 `TEMP TABLE`，整体插入 `snapshot_views`，并写 `snapshots.status=STAGED`。
4. 在另一个事务内写唯一失效事件、把快照改为 `COMMITTED`、更新 `pointer` 和补丁运行为 `COMMITTED`。

读方只通过 `pointer.plan_generation` 读取完整已提交代数，因此不会看到 STAGED 代数的部分行。

恢复规则：

- `PREPARED`：数据库事务未产生快照，按持久化载荷重新计算并提交。
- `STAGED`：复用已保存的结果和计划行，只完成最终提交，不重新计算、不重复计入失效。
- 失效事件主键为 `invalidation:<patch_id>:<view>`，恢复使用 `INSERT OR IGNORE`。
- 文件锁在进程被强杀时由操作系统释放。

同一 `patch_id` 多进程并发时，只有一个进程推进代数；其余读取已提交结果并标记 `duplicate=true`。

## 业务错误码

| 错误码 | 场景 |
| --- | --- |
| `E_VIEW_NAME_EMPTY` / `E_VIEW_NAME_INVALID` / `E_VIEW_NAME_DUPLICATE` | 视图名校验 |
| `E_SQL_SYNTAX` | SQL 为空、多语句、括号或结构非法 |
| `E_SQL_UNSUPPORTED` | CTE、集合运算、子查询等不支持结构 |
| `E_SQL_AMBIGUOUS_COLUMN` | 未限定列名有歧义 |
| `E_COLUMN_UNKNOWN` | 初次解析引用不存在的列 |
| `E_DEPENDENCY_MISSING` | 上游未声明或不存在 |
| `E_DEPENDENCY_SELF` | 视图直接自依赖 |
| `E_DEPENDENCY_CYCLE` | 依赖图存在环，含 `dependency_path` |
| `E_LOGIC_CLOCK_INVALID` | 时钟不是非负整数 |
| `E_LOGIC_CLOCK_REGRESSION` | 时钟回拨超过容差 |
| `E_V1_TIME_INVALID` | v1 相对秒数非法 |
| `E_V1_COLUMN_UNKNOWN` | v1 扁平列映射无法追溯 |
| `E_V2_META_MISMATCH` | v2 分片与 SQLite 元数据不一致 |
| `E_SHARD_HASH_MISMATCH` | 分片 SHA-256 不匹配 |
| `E_PLAN_GENERATION_REGRESSION` | 分片/视图计划代数回退 |
| `E_DOWNSTREAM_COLUMN_MISSING` | 下游引用列被删除，返回首个缺失列和引用路径 |
| `E_PATCH_PAYLOAD_CONFLICT` | 同 patch ID 不同载荷 |
| `E_PATCH_SEQUENCE_CONFLICT` | 同视图同编辑轮次被其他补丁占用 |
| `E_STATE_EXISTS` / `E_STATE_NOT_INITIALIZED` / `E_STATE_CONFLICT` | 状态初始化与并发冲突 |
| `E_RESUME_CONFLICT` | 半提交快照无法安全衔接当前指针 |
| `E_PLAN_INTEGRITY` | 持久化计划摘要或列契约损坏 |

错误输出保留 `view`、`node_id`、`edit_round`，并在适用时提升 `missing_column`、`reference_path`、`patch_id`、时钟水位等诊断字段。

## 固定种子样本

`sample/` 包含 9 个视图定义、v1 快照、3 个 v2 分片、SQLite 元数据库和 11 条补丁事件。固定基准为：

```text
seed              = 20261127
base_clock_ms     = 1700000000000
v2 plan generations = 2, 3, 4
```

样本覆盖：

- v1/v2 同视图逻辑时钟相同，v2 来源优先级胜出。
- v1 独有视图胜出。
- v2 墓碑删除 v1 视图。
- 新增、修改、删除、上游列映射变化。
- 列重命名导致下游不可编译。
- 时钟回拨、重复补丁、恢复和并发。

## 实际验证结果

验证环境：

```text
Linux / WSL Ubuntu
Python 3.10.12
sqlparse 0.6.1
SQLite 3.37.2 (Python sqlite3)
```

固定样本生成两次，以下文件逐字节一致：

```text
view_definitions.json
v1_snapshot.json
patch_events.jsonl
sample_manifest.json
sqlite_meta.db
v2_shards/shard-0001.json
v2_shards/shard-0002.json
v2_shards/shard-0003.json
```

分片哈希：

```text
shard-0001 49fcfc1240e1145116edd4a67da3238b1d4d79e3b97601d528561982042cc75b
shard-0002 876bc9dcedd0e5f795248862d1fd405fc829a208dac6b4a44d1cb4f9b45e86dc
shard-0003 3288de325a231f9b5f8e82dcda77c3227f155b5bc21ec371cdd62ffdb3178a2d
```

解析结果连续运行两次，输出 SHA-256 均为：

```text
41b42e4b955fb4039d776e84aab40f5d4850fe02c6404e1c8e105d68eb360e86
```

合并结果连续运行两次，输出 SHA-256 均为：

```text
afec40f32361c08310bf8c4d01d45ee1df8b5cc320c7a00f568718338a5a4056
```

合并统计：

```json
{"active_views": 8, "tombstones": 1, "v1_only": 1, "v2_selected": 7}
```

完整补丁流：

```json
{
  "processed": 11,
  "committed_count": 9,
  "failed_count": 2,
  "duplicate_count": 1,
  "plan_generation": 9,
  "logical_clock_watermark": 1700000008800
}
```

- `patch-0004`：`E_DOWNSTREAM_COLUMN_MISSING`，首个缺失列 `customer_totals.total_amount`。
- `patch-0009`：`E_LOGIC_CLOCK_REGRESSION`，回拨至 `1700000007000`，水位保持 `1700000008800`。
- 重复 `patch-0003`：`duplicate=true`，计划代数仍为 4，未新增失效事件。
- `patch-0010`：只把 `customer_totals`、`daily_orders`、`net_orders` 标脏；`net_orders` 的 dirty node 是原计划节点的真子集。

最终状态：

```json
{
  "plan_generation": 9,
  "view_count": 9,
  "stale_views": ["customer_totals", "daily_orders", "net_orders"],
  "invalidation_event_count": 11,
  "patch_run_counts": {"COMMITTED": 8, "FAILED": 2},
  "pending_runs": []
}
```

`verify` 的 17 项检查全部通过，包括 SQLite 完整性、指针、记录哈希、计划摘要、列契约、dirty node 存在性、事件唯一性、无半提交快照和无待恢复运行。

强杀测试：

- `GSB_CRASH_AFTER_STAGE=1`：退出 98；强杀后指针仍为 1，存在 1 个 `STAGED` 运行和 0 个失效事件；`resume` 后代数 2、失效事件 1、`verify=true`。
- `GSB_CRASH_AFTER_PREPARE=1`：退出 97；强杀后指针仍为 1，存在 1 个 `PREPARED` 运行；`resume` 后只重放该编辑，代数 2、失效事件 1、`verify=true`。

并发测试：4 个进程同时提交 `patch-0001`，仅 1 个进程产生真实提交，3 个返回 `duplicate=true`；最终代数 2、失效事件 1、`verify=true`。

自动化测试：

```bash
python3 -m unittest discover -s tests -v
```

结果：

```text
Ran 7 tests in 2.9s
OK
```

## 设计边界

- SQL 方言是有意收敛的 SQLite 兼容 SELECT 子集，超出范围会显式返回错误。
- 基础表必须显式列在 `sources` 中，以区分源表和视图依赖。
- 失效的是“计划/编译缓存”；未受影响视图的计划 JSON 和可执行 SQL 在 SQLite 中逐字节保留。
- 文件锁适用于本机共享文件系统；本工具不面向网络文件系统或跨主机协调。
- 运行期提交时间使用补丁逻辑时钟而非墙钟，保证样本结果可复现。
