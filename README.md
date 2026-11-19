# 本地数据库主从复制延迟诊断组件

这是一个不连接真实主从服务的本地诊断实现。它把 v1 相对延迟位点和 v2
绝对时间戳位点放进同一条确定性延迟计算链路，使用本地有序事务流模拟从库回放
进度，并把合并结果、窗口、误差和快照指针原子写入 SQLite。

实现只使用 Python 3.10+ 标准库，不依赖 NumPy 或其他第三方包；不使用 Docker。
Windows 本地文件系统所需的 `msvcrt` 文件锁、`pathlib` 路径和 LF 字节输出均已
处理。

## 快速开始

```bash
python3 -m replication_diag load
python3 -m replication_diag merge
python3 -m replication_diag lag
python3 -m replication_diag status
python3 -m replication_diag verify
```

默认状态库是 `runtime/state.sqlite`，默认配置是 `config/sample.json`。`--db`
既可以放在子命令前，也可以放在子命令后：

```bash
python3 -m replication_diag --db runtime/a.sqlite load
python3 -m replication_diag merge --db runtime/a.sqlite
python3 -m replication_diag lag --db runtime/a.sqlite
```

`load` 只做解析、校验、跨版本合并试算并暂存；`merge` 把最老的暂存操作提交成
不可变快照并切换活动快照指针。`lag`、`status`、`verify` 只读取已提交状态。

固定样本的第一次基线结果：

- 12 个合并事务：v1 保留事务 1..4，v2 裁决事务 5..12。
- v1 迁移决策 4 条，v2 墓碑事务 1 条。
- 已提交水位事务 12，回放水位事务 10。
- 尚未回放：2 个事务、200 字节日志。
- 窗口大小 3、步长 1，共 10 个完整滑窗。
- 延迟分位与精度敏感标记见 `lag` JSON。

## 目录

```text
replication_diag/
  v1_parser.py        v1 JSONL 解析与校验
  v2_parser.py        v2 分片、文件哈希、SQLite 元数据校验
  merge.py            跨版本仲裁、断链与时钟回拨检查
  lag.py              延迟、回放落后量、R7 分位、精度诊断
  state.py            SQLite 快照、暂存、恢复、幂等、verify
  file_lock.py        跨平台独占文件锁
  injections.py       临时副本上的故障注入
  cli.py              load/merge/lag/resume/status/verify
samples/v1/           v1 单文件 JSON 行
samples/v2/           v2 分片 + metadata.sqlite + manifest
samples/logs/         有序回放事务流
samples/packages/     完整替换、增量、位置回退、版本回退包
config/sample.json    注入逻辑时钟与窗口参数
config/injections.json
tools/build_samples.py
tools/run_injections.py
tests/test_component.py
```

`tools/build_samples.py` 可重新生成样本、SQLite 元数据和所有摘要。生成的样本
哈希会同步回 manifest。

## 输入格式

### v1 JSON 行

每行一个 JSON 对象：

```json
{"tx_id":"1","relative_delay_ms":5,"payload_bytes":100,"committed":true,"logical_clock":1000,"source_node":"primary-v1","replica_node":"replica-v1"}
```

规则：

- `tx_id` 规范化为 20 位零填充十进制；接受正整数或十进制文本。
- 文件内事务号严格递增。
- `relative_delay_ms` 必须是非负整数。
- `payload_bytes` 必须是正整数。
- `committed` 必须是布尔值；记录必须先是连续已提交前缀，再是连续未提交后缀。
- `logical_clock` 可省略；省略时使用
  `v1_base_clock_ms + line_index * v1_clock_step_ms` 注入。
- v1 没有回放代数，统一迁移到 `generation: 0`。

### v2 manifest、分片与 SQLite

`manifest.json`：

```json
{
  "format_version": 2,
  "package_version": "2.0.0",
  "package_type": "source",
  "logical_clock_floor": 1000,
  "tx_min": "00000000000000000005",
  "tx_max": "00000000000000000012",
  "shards": [
    {"path": "shards/shard-0001.json", "sha256": "..."}
  ],
  "metadata_db": {"path": "metadata.sqlite", "sha256": "..."}
}
```

每个分片：

```json
{
  "format": "v2-shard",
  "shard_id": "0001",
  "records": [
    {
      "tx_id": "00000000000000000005",
      "payload_bytes": 110,
      "logical_clock": 1004,
      "metadata_digest": "..."
    }
  ]
}
```

SQLite 表 `tx_metadata` 的列固定为：

```text
tx_id, tx_num, commit_time_ms, replay_time_ms, source_node, replica_node,
replay_generation, tombstone_applied, logical_clock, payload_bytes, committed
```

校验规则：

- 分片文件和 SQLite 文件 SHA-256 必须与 manifest 一致。
- 分片中的 `metadata_digest` 等于 SQLite 行上述 11 列规范 JSON 的 SHA-256。
- 分片记录集合与 SQLite 行集合必须完全一致。
- `replay_generation` 非负，并在同一 `replica_node` 内按事务号单调不减。
- 提交/回放时间戳为非负整数毫秒，回放时间不得早于提交时间。
- `tombstone_applied=1` 要求 `replay_generation >= 1`。
- 规范事务号是 20 位十进制文本。

### 有序回放流

目录中的 `*.jsonl` 按相对路径稳定排序，再按行读取：

```json
{"tx_id":"00000000000000000001","payload_bytes":100,"applied":true,"replay_time_ms":1005}
```

- 事务号全局严格递增且连续。
- `applied=true` 只能形成前缀，之后不得再出现已应用记录。
- 流内事务必须存在于合并位点中，而且字节量必须一致。
- 缺失时间戳不会被推断；延迟观测来自合并位点中的原始表示。

### 配置

关键字段：

```json
{
  "v1_scale": {"numerator": 1, "denominator": 1},
  "v1_rounding": "half_up",
  "v1_base_clock_ms": 1000,
  "v1_clock_step_ms": 1,
  "window_size": 3,
  "window_stride": 1,
  "tolerance_ms": 1,
  "observation_error_ms": 1,
  "v1_observation_error_ms": 0,
  "require_contiguous": true,
  "strict_clock_order": true
}
```

`v1_rounding` 支持 `half_up`、`half_even`、`toward_zero`、
`away_from_zero`、`floor`、`ceil`。所有换算内部使用 `fractions.Fraction`，
不经过浮点数累计。

## 合并与迁移语义

同一事务号的多版本记录按以下降序键裁决：

```text
(logical_clock, replay_generation, source_priority)
```

`v2` 的格式优先级高于 `v1`。如果前三个字段完全相同但内容摘要不同，返回
`E_AMBIGUOUS_TX`，不会用文件加载顺序打破平局。结果按数值事务号升序排列。

每条保留记录都包含：

- `source_version`、`original_time`、`original_delay`。
- v2 的绝对提交/回放时间保持原值。
- v1 的注入逻辑时钟、相对延迟、换算标度、舍入模式和舍入方向。
- v1 的 `unversioned -> generation:0` 迁移映射。
- 每条延迟的误差上界与误差来源链。

合并后检查事务号连续性和全局逻辑时钟不倒退；跨版本文件加载/枚举顺序不会改变
裁决或 JSON 排序。

## 延迟与窗口

单事务延迟：

- v2：`replay_time_ms - commit_time_ms`。
- v1：`round(relative_delay_ms * scale, rounding_mode)`；
  `replay_time = injected_logical_clock + rounded_delay`。

滑窗只包含已提交事务，并记录：

- 所依赖的 `committed_watermark`。
- 事务区间 `tx_start`、`tx_end`、参与事务列表。
- 固定 R7 线性插值规则：
  `position = p * (n - 1)`，在相邻排序值间线性插值。
- p50、p95、误差上界、误差来源链及精度敏感标记。

所有窗口仅取完整窗口：`0, stride, 2*stride, ...`，直到不能容纳
`window_size` 个事务。排序、插值、汇总顺序固定。

落后量字段：

- `unreplayed_transaction_count`：未应用日志事务数加上未出现在回放流中的已提交
  位点事务数。
- `unreplayed_log_bytes`：日志中 `applied=false` 的字节数。
- `unobserved_committed_transaction_count/bytes`：有已提交位点但回放流尚未覆盖
  的事务和字节。
- `unreplayed_total_bytes`：上述两类字节之和。

## 精度诊断

- v2 的提交、回放时间戳各按整数毫秒观测；默认合计误差上界为 1 ms。
- v1 的换算误差是
  `abs(rounded_delay - exact_scaled_delay) + v1_observation_error`。
- 精确值以有理数文本输出，例如 `"1/3"`，不会隐式丢弃。
- 任意两个事务或任意两个窗口的延迟差严格小于 `tolerance_ms` 时，双方标记为
  `precision_sensitive`，并输出比较双方、差值、容差和合并后的误差来源链。
- `truth_source` 明确声明结论来自按事务号升序的全量重算，不使用增量累计作为
  延迟真值，也不从缺失时间信息推断延迟。

## 位点包与回退

包 manifest 与 v2 manifest 同构，额外使用：

- `package_type: "full"`：完整替换。
- `package_type: "incremental"`：增量追加，要求 `tx_min` 恰好是当前
  `tx_max + 1`。

分类顺序：

1. manifest 摘要、版本、逻辑时钟下限、事务区间和分片哈希完全相同：`noop`。
2. 包事务上界低于当前已提交水位：`E_POSITION_ROLLBACK`。
3. 包版本低于活动版本：`E_VERSION_ROLLBACK`。
4. 按 `package_type` 判为 `full_replace` 或 `incremental_append`。

完整替换不会逐条覆盖活动表。它在 `staging_entries` 中写入完整新代位点，再在同一
事务中创建新 `snapshot_id` 并翻转 `active_snapshot.snapshot_id`。旧快照和旧指针
值仍保留，可审计。

位点包只替换位点，不替换回放流；回放流沿用当前活动快照记录，并在提交前重新检查
事务存在性与字节一致性。包中新增但尚未进入回放流的已提交事务会以
`unobserved_committed_*` 计入落后量。

## SQLite 持久化与恢复

状态库使用 WAL、`synchronous=FULL` 和 `BEGIN IMMEDIATE`。写入方通过
`state.sqlite.lock` 独占文件锁串行化；读取方也只在活动指针已提交后读取快照。

主要表：

- `operations`：`staged/committed/noop` 操作与输入摘要。
- `raw_records`、`raw_logs`：`load` 暂存输入。
- `staging_entries`、`staging_package`：完整替换/增量包的临时表。
- `snapshots`：不可变快照头、水位、输出摘要。
- `snapshot_entries`、`snapshot_logs`、`snapshot_migrations`、
  `snapshot_windows`、`snapshot_diagnostics`：快照内容。
- `active_snapshot`：唯一活动快照指针。

强制终止测试：

```bash
python3 -m replication_diag load --db runtime/crash.sqlite
RD_CRASH_BEFORE_COMMIT=1 python3 -m replication_diag merge --db runtime/crash.sqlite
python3 -m replication_diag status --db runtime/crash.sqlite
python3 -m replication_diag resume --db runtime/crash.sqlite
python3 -m replication_diag verify --db runtime/crash.sqlite
```

`RD_CRASH_BEFORE_COMMIT=1` 使用 `os._exit(137)` 模拟进程被强制终止。此时只有
`staged` 操作，活动指针为空；`resume` 只重放未完成提交，不生成虚假退出报告。

并发 `load`/`merge` 由文件锁和 `op_id`/`snapshot_key` 唯一约束保证同一输入只
提交一个快照。读取方只会看到完整快照行，不会看到半替换的表。

## 注入与业务错误码

运行全部注入：

```bash
python3 tools/run_injections.py
```

只运行单个注入：

```bash
python3 -m replication_diag load --inject merged_clock_rollback
```

注入在系统临时目录中的副本上执行，不写状态库。覆盖：

- 非法事务号、v1 断序、负延迟、零字节、提交标记回退。
- 分片哈希、SQLite 哈希、元数据摘要不匹配。
- 回放代数回退、负代数、负时间戳、回放早于提交、墓碑无代数。
- 合并逻辑时钟回拨、位点断链。
- 回放流重复、断链、已应用后缀、字节不一致。

业务错误码按域划分：

- 输入：`E_IO`、`E_JSON`、`E_CONFIG`、`E_MANIFEST`、`E_TX_ID`。
- v1：`E_V1_ORDER`、`E_V1_DELAY`、`E_V1_BYTES`、`E_V1_COMMIT`、
  `E_V1_NODE`。
- v2：`E_V2_HASH`、`E_V2_SCHEMA`、`E_V2_GENERATION`、`E_V2_TIMESTAMP`、
  `E_V2_REPLAY_BEFORE_COMMIT`、`E_V2_TOMBSTONE`、
  `E_V2_METADATA_DIGEST`、`E_V2_BYTES`。
- 合并/回放：`E_DUPLICATE_TX`、`E_AMBIGUOUS_TX`、`E_POSITION_GAP`、
  `E_CLOCK_ROLLBACK`、`E_LOG_ORDER`、`E_LOG_DUPLICATE`、`E_LOG_GAP`、
  `E_LOG_COMMIT`、`E_LOG_BYTES`、`E_LOG_ORPHAN`、`E_MISSING_TIME`。
- 位点包：`E_POSITION_ROLLBACK`、`E_VERSION_ROLLBACK`、
  `E_PACKAGE_INCOMPLETE`、`E_PACKAGE_OVERLAP`。
- 状态/恢复：`E_STATE`、`E_RECOVERY`、`E_LOCK`、`E_VERIFY`、
  `E_INJECTION`。

## 验证结果

在一个全新状态库上执行：

```bash
python3 -m unittest discover -s tests -v
```

实际结果：11 个测试全部通过（约 5 秒）。

最终基线 `lag` 的 SHA-256 为
`24726dad8544be18761f6a2501fcd6d52b1c42a9427e76be190f29dca31cafa5`。
同一状态连续两次输出以及两个全新状态库的输出版本均为该摘要。基线中有 12 个
事务、4 条迁移决策、1 个墓碑、10 个窗口；8 个事务和 4 个窗口被标记为精度敏感。

已验证：

- v1/v2 重叠区间裁决：tx 5 按来源优先级选 v2，tx 6 按回放代数选 v2。
- 反转 v2 输入顺序后合并 JSON 不变。
- 连续两次全新运行的 `lag` 输出逐字节相同。
- 同状态库重复 `lag` 输出逐字节相同。
- 4 进程并发 `load` + 4 进程并发 `merge` 只产生 1 个快照、1 个已提交 source
  操作和 1 个活动指针。
- `RD_CRASH_BEFORE_COMMIT=1` 退出码 137 后，`resume` 恢复并 `verify` 通过。
- 完整替换、`noop`、增量分类、位置回退和版本回退均通过。
- 19 个注入场景全部返回预期 `E_xxx`。
- 非 1:1 v1 换算保留 `"1/3"` 形式舍入误差，不丢失精度。

`verify` 会重算 lag，并检查活动快照、条目连续性、配置未漂移、输出摘要、
窗口摘要、迁移决策、墓碑计数和误差记录数量。
