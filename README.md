# 本地增量物化视图引擎

这是一个从零实现的本地物化视图引擎。它在同一套刷新链路中解析 v1 JSON 定义和
v2 分片 JSON + SQLite 元数据，完成跨版本合并、确定性增量刷新、依赖失效传播、
版本化定义替换、SQLite 原子持久化、中断恢复和多进程并发幂等。

引擎不连接外部服务，也不依赖真实数据库实例。样本、时间戳、日志序号和聚合结果
全部由 `mvengine.samplegen` 确定性生成。

## 环境与安装

要求 Python 3.11+。Windows 示例：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[test]"
python -m pytest --version
```

Linux/macOS 示例：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e '.[test]'
python -m pytest --version
```

本次实际验证环境为 Python 3.12.13、pytest 9.2.0、NumPy 2.5.5、SQLite 3.53.1。
NumPy 按题目要求安装；聚合归约本身刻意使用固定顺序的 Python 标量循环，而不是
NumPy 归约，以避免不同实现对浮点加法顺序做重排。

## 快速演示

```bash
# 重新生成固定样本，并在两个隔离数据库中各跑一遍完整场景
python scripts/demo.py

# 常用命令
python -m mvengine load --db artifacts/state.sqlite \
  --package samples/sample/package.json --with-logs
python -m mvengine status --db artifacts/state.sqlite
python -m mvengine query --db artifacts/state.sqlite --view sales_east
python -m mvengine verify --db artifacts/state.sqlite \
  --package samples/sample/package.json \
  --snapshots samples/sample/snapshots
```

`scripts/demo.py` 会执行初始加载、刷新、深重放校验、快照校验、完整替换、增量
日志追加、版本回退拒绝和强杀恢复，并把两次运行结果逐字比较。输出写入
`artifacts/demo-run-1.json`、`artifacts/demo-run-2.json` 和
`artifacts/demo-verification.json`。

## 样本目录

`samples/sample` 是基础包：

- `v1/views.json`：v1 单文件 JSON 定义。
- `v2/manifest.json`：v2 清单、基表 schema、分片哈希和代数下限。
- `v2/shards/views-00.json`：v2 视图分片。
- `v2/metadata.sqlite`：v2 视图代数历史、绝对刷新时刻、SQLite 元数据和墓碑。
- `logs/`：两个按逻辑时钟递增的 JSONL 变更日志段。
- `snapshots/`：序号 3、6、9、12 的历史视图快照。
- `package.json`：包版本、定义摘要、文件哈希和加载入口。

另外生成三个演进包：

- `samples/sample_append`：包版本 2，定义不变，增加日志段 `seg-0003`。
- `samples/sample_next`：包版本 2，完整定义替换，新增 `east_high` 并提升
  `sales_east` 代数。
- `samples/sample_rollback`：包版本 0，用于验证回退拒绝。

重新生成：

```bash
python -m mvengine.samplegen --out samples
```

## 定义格式

### v1

`v1/views.json` 的关键字段：

```json
{
  "format": "v1",
  "logical_clock_ms": 100000,
  "base_schema": {
    "sales": {"columns": ["id", "region", "amount", "units"]}
  },
  "views": [
    {
      "name": "sales_east",
      "base_table": "sales",
      "filter": "region == \"east\" and amount >= 10.0",
      "aggregate": {
        "column": "amount",
        "functions": ["count", "sum", "sum_sq"]
      },
      "refresh_seconds": 30
    }
  ],
  "base_view_map": {"sales": ["sales_east"]}
}
```

校验包括视图名唯一、基表存在、过滤列和聚合列合法、三个聚合函数各出现一次、
相对刷新秒数为非负整数，以及 `base_view_map` 与视图声明互为精确逆映射。

### v2

v2 清单记录 `manifest_version`、`view_generation_floor`、基表 schema、SQLite
文件路径和 `{分片路径: SHA-256}`。每个分片包含规范名、基表集合、过滤表达式、
三个增量聚合规则和 `upstream_views`。

SQLite 的 `view_revisions` 表按 `seq` 保存代数历史。每条记录包含规范名、代数、
逻辑时钟、绝对刷新毫秒、状态、基表集合、聚合规则、上游依赖、分片文件、
规范摘要和墓碑字段。当前记录是该视图 `seq` 最大的记录。

v2 校验包括：

- 清单中每个分片文件 SHA-256 完全匹配。
- 每个视图历史代数严格递增，当前代数不低于清单下限。
- 绝对刷新时刻、逻辑时钟和墓碑时刻均为非负整数。
- 当前 active 元数据与 active 分片视图集合完全一致，规范摘要一致。
- active 视图不得依赖被墓碑化的视图，依赖两端闭合且全图无环。
- tombstoned 当前记录必须有非空原因和整数时刻，且不得指向 active 分片。

## 变更日志格式

日志清单为 `mv-logs-v1`，每个段记录 `start_seq`、`end_seq` 和 SHA-256。JSONL
每行必须包含：

```json
{
  "seq": 1,
  "logical_clock_ms": 100000,
  "base_table": "sales",
  "key": "s1",
  "op": "insert",
  "before": null,
  "after": {"id": "s1", "region": "east", "amount": 25.0, "units": 2},
  "content_digest": "..."
}
```

`content_digest` 是对除该字段外全部事件字段的规范 JSON SHA-256。解析器还校验
段哈希、声明序号范围、JSONL 行数、全局连续序号、非递减逻辑时钟和
insert/update/delete 镜像约束。刷新对象必须是已有 base 行状态的精确后继，
否则返回 `E_LOG_STATE_MISMATCH`。

## 合并与迁移

同一视图的候选版本按以下有序键裁决：

```text
(logical_clock_ms, generation, source_priority)
```

`source_priority` 为 v1=1、v2=2。因此逻辑时钟更高者胜；同钟同代时 v2 胜。
v1 缺少的代数按 v2 的 `view_generation_floor` 注入，并写入迁移决策。每项裁决
保留 selected/alternatives、来源格式、来源摘要、原始刷新表示和具体原因。

v1 相对刷新时间迁移公式为：

```text
anchor_ms = floor(logical_clock_ms / 1000) * 1000
absolute_refresh_ms = anchor_ms + refresh_seconds * 1000
```

迁移记录 `scale_ms=1000`、`rounding=floor`、锚点、原相对秒数和迁移后代数。
这样在任意毫秒时钟下，`floor(clock_ms/1000)` 的 v1 到期判定与迁移后的
`clock_ms >= absolute_refresh_ms` 判定完全等价。

合并后索引包含 `definition_digest` 和 `index_digest`。两次相同输入合并得到的
规范 JSON 和摘要逐位一致。`source_ref` 只使用包内相对路径，不泄露本机绝对路径。

## 刷新语义

每个视图维护：

- 以 `(base_table, row_key)` 命名的可见行集合及其来源日志段。
- `count`、`sum`、`sum_sq`。
- 已应用日志水位、结果摘要、重算次数、最近触发和传播路径。

单次变更在一个 SQLite 事务中提交：base 当前行、所有视图水位、聚合状态、
`applied_changes` 幂等账本和失效决策同时落库。每个视图的聚合从排序后的可见行
重算，固定顺序为：

```text
rows sorted by (table, key)
then count -> sum -> sum_sq
```

因此增量刷新和从空状态全量重放使用完全相同的浮点运算序列。`verify` 深重放
整条日志链，并逐视图比较指标、可见行来源和结果摘要；不一致时输出视图名、
首个差异字段、期望/实际值和来源 `seq`、`segment_id`、`segment_file`。

`sum` 和 `sum_sq` 忽略 null，但拒绝非数值；`count(*)` 统计可见行数。多个
基表时，各表可见行使用表名命名空间后合并归约。`upstream_views` 是失效边；
引擎不会从上游读取数据，而是用该图传播“上游结果已变化”的失效信号。

## 失效传播

基表变更先判定直接使用该表的视图是否需要改写可见行。若该视图结果摘要变化，
再按稳定拓扑序向下游传播。决策动作有三种：

- `recomputed`：闭包内视图需要重算。
- `reused_unchanged`：直接绑定该基表，但本次变更没有改变其可见行。
- `skipped_unaffected`：不在本次影响闭包内，既不重算也不改聚合。

所有决策持久化到 `invalidation_decisions`，包含触发来源、传播路径和
`result_changed`。`status.recompute_count` 可直接验证未受影响视图没有被重算。

## 时间与查询可见性

当前逻辑时钟是已应用日志的最大逻辑时钟。查询时钟默认取该值。视图只有在
`clock_ms >= absolute_refresh_ms` 时可见；未到期时 `visible=false`，指标和行集
返回 null/空数组，但仍显示水位、代数和到期时间。

## 包替换、追加与回退

`replace` 比较：

- `package_version`。
- `view_generation_floor`。
- 合并索引摘要。
- v1 文件、v2 清单和全部分片哈希。
- 已提交日志段哈希前缀。

判定结果：

- `no_op`：版本、定义和日志身份均相同。
- `append`：定义身份相同，日志段保留已提交前缀并追加新段。
- `full_replace`：定义身份变化。
- `E_VERSION_ROLLBACK`：包版本或代数下限回退。
- `E_VERSION_CONFLICT`：同版本但定义/日志身份不同，或日志前缀被改写。

完整替换先把新定义写入不可变定义记录和临时 staging 表，所有视图状态准备完成后
只更新 `current_pointer`，不会逐条覆盖当前定义。视图执行语义摘要未变时保留原
聚合状态、结果摘要和重算次数；新视图或执行语义变化者才从当前 base 行重算。
SQLite 读事务和 WAL 保证查询方只能看到切换前或切换后的完整版本。

## 持久化、恢复与并发

SQLite 中的关键表包括：

- `definitions` / `view_specs` / `current_pointer`：不可变定义和当前版本指针。
- `package_history`：包分类和定义/日志哈希。
- `base_rows`：当前基表镜像及来源。
- `view_states`：视图水位、聚合、可见行、摘要和重算次数。
- `applied_changes`：全局变更序号幂等账本，主键为 `seq`。
- `invalidation_decisions`：失效触发与传播路径。
- `migration_decisions`：v1→v2 迁移决策。
- `refresh_runs`：运行中、成功和恢复状态。

刷新和替换使用 `<db>.lock` 跨平台文件锁，并以 SQLite `BEGIN IMMEDIATE`、
`busy_timeout` 和 WAL 做事务串行化。每个日志序号独立提交，因此强杀后：

- 已提交序号在 `applied_changes` 和 `watermark_seq` 中可见。
- 未提交事务由 SQLite 回滚。
- `resume` 从水位后继续，不重复累加。
- 遗留 `running` 运行记录被标记为 `recovered`。

替换事务在指针切换前中断时，下一次打开数据库会回滚到旧定义指针，可安全重试。

## CLI

```text
load     解析包并安装初始定义，可选 --with-logs
merge    只合并 v1/v2，输出或写出确定性统一索引
refresh  校验并应用日志，支持 --until-seq 与内部强杀故障点
query    查询已提交结果，支持 --view 和 --clock-ms
replace  分类并安装新包，支持 --with-logs
resume   从中断水位继续刷新
status   输出视图代数、日志水位、迁移决策数等的稳定排序 JSON
verify   深重放日志并校验历史快照
```

所有成功输出和业务错误均为稳定 JSON。错误以
`{"error":{"code","message","context"}}` 输出，进程退出码为 2；`verify` 发现
结果差异时输出完整差异但退出码为 1。

主要错误码：

| 错误码 | 含义 |
| --- | --- |
| `E_V1_DUPLICATE_VIEW` | v1 视图名重复 |
| `E_V1_MAPPING_MISMATCH` | 扁平映射与视图声明不一致 |
| `E_SHARD_HASH_MISMATCH` | v2 分片哈希不匹配 |
| `E_GENERATION_NOT_MONOTONIC` | v2 视图代数不递增或低于下限 |
| `E_DEPENDENCY_UNDEFINED` / `E_DEPENDENCY_CYCLE` | 上游依赖不闭合/有环 |
| `E_TOMBSTONE_INCONSISTENT` | 墓碑与 active/分片状态不相容 |
| `E_LOG_SEQUENCE` / `E_LOG_DIGEST` | 序号链或内容摘要非法 |
| `E_LOG_STATE_MISMATCH` | before 镜像不是当前基表状态 |
| `E_LOG_SEQ_CONFLICT` | 同一序号以不同摘要重复提交 |
| `E_VERSION_ROLLBACK` | 包版本或代数下限回退 |
| `E_VERSION_CONFLICT` | 同版本身份冲突或日志前缀被改写 |
| `E_STATE_CORRUPT` | 持久化状态摘要不自治 |

## 实际验收结果

在固定样本上已执行并记录：

- 两代定义合并：基础索引摘要
  `9d4b8374dcd3acb71993dbab3eb613d79bc0b9a1f18799d33a2fa4ccd423cfed`。
- `sales_east` 同钟同代由 v2 格式优先级胜出；`legacy_west` 由 v2 墓碑胜出。
- 增量刷新 12 条日志后 `sales_east` 为 `count=1,sum=32.0,sum_sq=1024.0`，
  `inventory_total` 为 `count=1,sum=7.0,sum_sq=49.0`。
- 深重放与快照（序号 3/6/9/12）全部一致；`verify.ok=true`。
- 强杀于刷新序号 4 后 `watermark=4`，恢复只应用 5..12，最终与无中断库逐字段一致。
- 两个并发刷新进程对同一库提交 12 条变更，恰好一个进程应用 12 条，另一个
  `no_op`；最终状态与单进程一致。
- 完整替换保留 `sales_east` 等未受影响视图的结果摘要与重算次数，只为新
  `east_high` 重算。
- 回退包返回 `E_VERSION_ROLLBACK`，状态摘要不变。
- `python -m pytest -q`：28 passed。
- `python scripts/demo.py` 连续两次 stdout 逐字一致，SHA-256 为
  `79e7249b392b518124933bfd0a17449ec20c0feedfe1a541b08b9f448a123434`。
