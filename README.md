# shardrouter：本地确定性分库分表路由组件

这是一个只依赖 Python 标准库的教学与排障演示组件。它把本地 JSON 分片配置和逻辑 SQL 谓词编译为可复现的路由计划，覆盖取模、范围、一致性哈希三种算法，并演示版本化缓存、路由单元依赖失效、快照整体替换恢复和三类可注入业务异常。

组件不启动网络服务，不访问第三方索引，也不执行真实业务 SQL。样本 SQLite DDL 位于 `samples/schema.sql`，路由结果给出目标物理库表名。

## 目录

| 路径 | 说明 |
| --- | --- |
| `shardrouter/` | 可集成 Python 包：配置、谓词、算法、缓存、依赖、会话、快照、UI、CLI |
| `samples/config.json` | 确定性 v1 分片配置，包含三种算法和路由单元依赖 |
| `samples/config_v2.json` | 规则与哈希环演进后的 v2 配置 |
| `samples/schema.sql` | 逻辑表结构样本，可直接交给 `sqlite3` |
| `samples/anomalies/` | 损坏配置、冲突哈希环、版本不一致元数据样本 |
| `scripts/demo.py` | 默认运行三场景 demo 的入口 |
| `tests/` | 46 个标准库 `unittest` 测试 |
| `artifacts/demo/` | 实际运行产物：计划、增量失效、快照恢复、日志、摘要 |
| `artifacts/verification/` | 测试和双进程复现验证记录 |

## 环境

- Python 3.11 或以上。
- 运行时不依赖第三方包；`sqlite3` 与 `tkinter` 均来自标准发行版。
- Windows 可直接使用 Python 官方安装包；安装时勾选 `tcl/tk and IDLE` 即可获得 Tkinter。
- 无显示环境可只验证 CLI 和 `--headless` UI 视图模型。

本工作区实际检查结果：

- Python `3.11.0rc1`
- SQLite `3.37.2`
- 当前解释器未包含 `_tkinter`，因此 GUI 启动按设计返回 `UI_SKIPPED`；`python -m shardrouter ui --headless` 已验证稳定视图模型。
- 没有新增监听端口，也没有网络访问逻辑；预留回环端口 `127.0.0.1:18765` 已验证可绑定，`samples/schema.sql` 已在内存 SQLite 中执行成功。

## 快速运行

Windows PowerShell / CMD：

```bat
python -m shardrouter demo --output-dir artifacts\demo
python -m shardrouter validate --config samples\config.json
python -m shardrouter route --table orders --predicate "tenant_id = 7 AND region = 'east'"
python -m shardrouter anomaly --type broken-config --log-file artifacts\broken.jsonl
python -m shardrouter anomaly --type ring-conflict --log-file artifacts\ring.jsonl
python -m shardrouter anomaly --type version-mismatch --log-file artifacts\version.jsonl
python -m shardrouter ui --headless
python -m unittest discover -s tests -v
```

Linux/macOS 使用相同命令和 `/` 路径。也可以直接执行：

```bash
python scripts/demo.py --output-dir artifacts/demo
```

`demo` 顺序执行：

1. 正常路由：三种算法、广播标记、边界值、缓存命中、路由单元派生。
2. 规则变更后的增量路由：`orders` 取模基数变化、`sessions` 哈希环节点权重变化，输出拓扑序失效集合。
3. 快照恢复后重放：从 v2 会话恢复 v1 快照，整体替换配置、依赖图和缓存，并比较重放计划字节。
4. 三类异常：损坏配置 `E_CONFIG`、同权重环节点冲突 `E_RING`、元数据版本不一致 `E_VERSION`，同时写入 JSONL。

## 配置格式

配置根对象示例（完整样本见 `samples/config.json`）：

```json
{
  "format_version": 1,
  "config_version": 1,
  "metadata_version": 1,
  "tables": [
    {
      "name": "orders",
      "columns": [
        {"name": "tenant_id", "type": "INTEGER", "nullable": false},
        {"name": "region", "type": "TEXT", "nullable": false}
      ],
      "shard_keys": ["tenant_id", "region"],
      "database_count": 2,
      "tables_per_database": 4,
      "version": 1,
      "algorithm": {"type": "mod", "modulus": 8}
    }
  ],
  "route_units": [
    {
      "name": "orders_lookup",
      "table": "orders",
      "predicate": "tenant_id = 7 AND region = 'east'",
      "depends_on": []
    },
    {
      "name": "orders_rollup",
      "depends_on": ["orders_lookup"]
    }
  ]
}
```

算法参数：

```json
{"type": "mod", "modulus": 8}
{"type": "range", "boundaries": [100, 500, 1000], "boundary_policy": "lower_inclusive_upper_exclusive"}
{
  "type": "consistent_hash",
  "ring_version": 1,
  "nodes": [
    {"database": 0, "table": 0, "weight": 8},
    {"database": 0, "table": 1, "weight": 16}
  ]
}
```

加载采用全有或全无语义。以下情况都会整体拒绝并返回 `E_CONFIG`，同时给出 `field_path`；多个错误放在 `details.errors` 中稳定排序：

- 表名或路由单元名重复。
- 列名重复，或分片键列不存在。
- `database_count`、`tables_per_database`、表版本、环版本不是正整数。
- 取模基数不是正整数。
- 范围边界不是严格递增的有限数值。
- 一致性哈希虚拟节点权重不是正整数，或节点库表下标越界。
- 路由单元依赖不存在或存在环。
- 配置字段类型、算法类型或未知字段不正确。

范围算法要求恰好一个分片键。`range` 边界含义固定为左闭右开：`[-inf,100)`、`[100,500)`、`[500,1000)`、`[1000,+inf)`，因此值 `100` 属于 bucket 1，值 `500` 属于 bucket 2。

## 谓词 DSL

支持：

- 等值：`col = value`、`col == value`、`col != value`、`col <> value`
- IN：`col IN (v1, v2, ...)`
- 比较：`< <= > >=`
- 区间：`col BETWEEN low AND high`，两端都包含
- AND 组合与括号
- 可选前缀 `WHERE`
- 字面量：整数、浮点、单引号字符串、`TRUE`、`FALSE`、`NULL`

规范化规则保证同一语义得到同一 `normalized` 文本和 `routing_id`：

- 关键字大小写、空白和括号被规范化。
- 标识符转小写。
- AND 项按稳定文本排序。
- IN 值去重并稳定排序；单元素 IN 归一为等值。
- 字符串内容大小写保留，不做不安全的折叠。

无法解析的语法、未知列、非有限数值和 `OR` 返回 `E_SQL`，`position` 同时给出 `offset`、`line`、`column`。不包含完整分片键的计划会显式写入：

```json
{
  "broadcast": true,
  "coverage": "broadcast",
  "reason_code": "BROADCAST_MISSING_SHARD_KEYS"
}
```

## 三种算法

物理目标命名固定为 `db_NNNN.<logical_table>_NNNN`，例如 `db_0001.orders_0002`。目标按 `(logical_table, database_index, table_index)` 稳定排序。

### 取模

1. 对完整分片键元组做类型化 canonical JSON 编码。
2. 对编码做 SHA-256，取前 8 字节为大端无符号整数。
3. 对配置的 `modulus` 取模。
4. 若 modulus 大于物理分片数，再折叠到 `database_count * tables_per_database`。

算法不使用 Python 内置随机化 `hash()`，跨进程可复现。

### 范围

对点值使用 `bisect_right` 定位 bucket。对范围谓词，计算与查询区间相交的所有 bucket，并输出每个 bucket 的物理映射。边界值归属固定为左闭右开。

### 一致性哈希

每个物理节点按 `weight` 生成虚拟节点，token 由 `SHA-256(logical_table|database|table|replica)` 前 8 字节确定。环按 token、库下标、表下标、replica 稳定排序；查找使用顺时针第一个 `token >= key_hash` 的节点，越界回绕到 0。重复物理节点或虚拟 token 冲突返回 `E_RING`。

## 缓存与版本

缓存键为：

```text
(table, normalized_predicate, table_rule_version, ring_version)
```

非一致性哈希表的缓存环版本记为 `0`。命中时还会校验条目记录的规则版本和环版本；不一致返回 `E_CACHE`、删除旧条目并重算。缓存容量超限时按 `(accessed_seq, inserted_seq, canonical_key)` 的确定顺序逐出。

配置演进规则：

- 表规则内容变化：表规则版本至少 `old + 1`。
- 哈希环节点/权重变化：环版本至少 `old + 1`。
- 路由单元定义或元数据变化：配置版本至少 `old + 1`。
- 显式版本更高时保留更高值，因此版本单调递增。
- 发生规则或环变化时清空整个旧计划缓存，不做逐条修补。

## 依赖与失效

路由单元可以有自己的逻辑表和谓词，也可以由依赖单元的物理目标并集派生。依赖图要求无环。表规则变化会先找到直接引用该表的单元，再沿依赖边找到全部下游；报告包含：

```json
{
  "seeds": ["orders_lookup", "orders_region"],
  "order": ["orders_lookup", "orders_region", "orders_rollup"],
  "unit_results_removed": ["orders_lookup", "orders_region", "orders_rollup"],
  "cache_entries_removed": 2
}
```

删除仍被路由单元引用的表规则或删除仍被其他单元依赖的路由单元会返回 `E_DEPEND`，`details.referrers` 列出直接引用者。

## 快照与整体替换

快照 JSON 保存：

- 完整配置与 `config_version`。
- `metadata_version`、所有表规则版本和哈希环版本。
- 依赖图内容指纹 `dependency_graph_version`。
- 计划缓存条目、容量和访问序号。
- 路由单元结果。
- 确定性路由历史摘要。

恢复前先比较当前会话与快照的配置版本、依赖图版本、元数据版本、各表规则版本和各环版本，并按字段名稳定排序差异。版本不一致时进入 `whole_replace`：配置、依赖图、计划缓存、单元结果和路由历史全部由快照替换，绝不增量合并。版本一致时也会完整替换，报告模式为 `replace_same_versions`。

## 异常注入与业务码

注入只使用输入文件或命令行参数，不修改源码：

| 注入 | 命令 | 业务码 |
| --- | --- | --- |
| 损坏 JSON 配置 | `python -m shardrouter anomaly --type broken-config` | `E_CONFIG` |
| 同权重重复哈希环节点 | `python -m shardrouter anomaly --type ring-conflict` | `E_RING` |
| 元数据版本不一致 | `python -m shardrouter anomaly --type version-mismatch --metadata-version 999` | `E_VERSION` |

也可直接对任意命令使用参数，例如：

```bash
python -m shardrouter route --table orders --predicate "tenant_id=7 AND region='east'" \
  --inject ring-conflict --log-file artifacts/ring.jsonl
```

结构化日志是无时间戳的 canonical JSONL，因此重复运行可逐字节比较。文件 `samples/anomalies/version_mismatch_metadata.json` 提供版本不一致参数样本。

完整业务码：

- `E_CONFIG`：配置或快照配置校验失败。
- `E_SQL`：谓词词法/语法/语义解析失败，带位置。
- `E_RING`：一致性哈希环冲突。
- `E_CACHE`：缓存条目版本不一致，已触发重算。
- `E_DEPEND`：删除仍被引用的表规则或路由单元。
- `E_VERSION`：元数据版本不一致。
- `E_SNAPSHOT`：快照结构损坏或无法读取。

## Python API 示例

```python
from shardrouter import (
    RouterSession,
    compare_canonical,
    load_config,
    normalize_predicate,
    verify_repeated,
)

config = load_config("samples/config.json")
table = config.table_map()["orders"]
info = normalize_predicate(table, "TENANT_ID=7 and REGION IN ('east')")
print(info.normalized, info.routing_id)

session = RouterSession(config, cache_capacity=32)
route_result = session.route_with_cache("orders", info.original)
plan = route_result["plan"]

apply_report = session.apply_config(load_config("samples/config_v2.json"))
snapshot = session.snapshot()
restore_report = session.restore_snapshot(snapshot)

stable = verify_repeated(
    lambda: RouterSession(config).route("ledger", "amount BETWEEN 100 AND 500")
)
assert stable["stable"]
```

所有公开接口从 `shardrouter.__init__` 导出，包括配置加载、谓词规范化、算法路由、缓存、依赖图、会话失效、快照保存恢复和验证函数。

## 确定性保证

- 所有 JSON 输出使用 `sort_keys=True`、固定分隔符、UTF-8、末尾换行。
- 集合与目标均显式排序，不依赖 dict/set 遍历偶然顺序。
- 哈希使用 SHA-256，不使用进程随机化哈希。
- 日志和报告不写时间戳、进程号、绝对输出目录等易变字段。
- UI 行、依赖边、理由、失效集合和业务码都按稳定键排序。
- `tests/test_cli.py` 会在两个独立 Python 进程中运行完整 demo，并逐文件比较字节。

## 验证结果

命令：

```bash
python -m unittest discover -s tests -v
```

结果记录在 `artifacts/verification/test_results.txt`：46 个测试全部通过。

`python -m shardrouter demo --output-dir artifacts/demo` 的实际摘要位于 `artifacts/demo/summary.json`。验收项包括：

- 三种算法均生成目标库表并带算法、规则版本、环版本和理由。
- 范围边界 `100`、`500` 的 bucket 分别为 `1`、`2`。
- 缺失分片键计划显式标记广播。
- 同义谓词命中同一缓存键。
- 规则/环变化后的失效集合包含直接和间接下游，顺序符合拓扑。
- v1 快照在 v2 会话中整体替换恢复，缓存完全回到快照状态。
- 快照重放计划与原计划逐字节一致。
- `E_CONFIG`、`E_RING`、`E_VERSION` 三类注入均得到预期业务码。

双进程独立复现结果见 `artifacts/verification/reproducibility.json`：7 个 demo 产物逐字节一致，摘要相同。环境与 SQLite DDL 验证见 `artifacts/verification/environment.json`，总验收记录见 `artifacts/verification/acceptance.json`。

## 已知边界

- 不支持 `OR`、子查询、函数、JOIN 或完整 SQL 语句；输入是单表谓词表达式。
- 范围分片算法要求单分片键和数值边界。
- 取模/一致性哈希遇到完整但非点集的键约束时，会扫描全部物理分片，但保持 `broadcast=false`，并以 `COMPLETE_KEY_RANGE_SCAN` 区分“键完整但无法枚举”。
- 组件只产出物理目标，不负责连接数据库或执行 SQL；`samples/schema.sql` 用于本地 SQLite 表结构演示。
