# 本地告警降噪与根因关联组件

这是一个仅依赖本地文件与 SQLite 的确定性实现。组件把固定 JSON 样本中的原始告警转换为：

1. 按 `(service, rule)` 与注入逻辑时钟聚类的事件簇；
2. 被静默/维护窗口粘滞抑制的事件与簇代表事件；
3. 沿上游依赖闭包计算的根因候选排序与可审计依据；
4. 规则补丁后的精确失效、复用和重建记录；
5. 可在进程被强制终止后恢复的提交状态。

结论计算不读取系统时间；所有时间边界、排序和依据文本只由输入中的 `logical_ts`、事件序号及持久化状态决定。

## 环境

- Python 3.11+
- NumPy 1.26+
- Linux/POSIX（并发一致性使用 `fcntl` 文件锁）

当前工作区已验证：

```text
Python 3.12.13
NumPy 2.5.5
SQLite 3.53.1
```

初始化并安装：

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
```

也可以直接使用已经创建的 `.venv`。

## 快速运行

```bash
DB=artifacts/manual/state.db

.venv/bin/python -m alarm_dedup --db "$DB" load \
  --topology samples/topology.json --window-ms 1000
.venv/bin/python -m alarm_dedup --db "$DB" ingest \
  --events samples/events.json
.venv/bin/python -m alarm_dedup --db "$DB" patch \
  --patch samples/patch.json
.venv/bin/python -m alarm_dedup --pretty --db "$DB" status
.venv/bin/python -m alarm_dedup --db "$DB" patch \
  --patch samples/patch_revert.json
.venv/bin/python -m alarm_dedup --db "$DB" verify
```

`load` 的参数顺序也可写成 `load --topology ... --window-ms ...`；全局参数 `--db` 和 `--pretty` 位于子命令之前。

## 子命令

| 子命令 | 作用 |
| --- | --- |
| `load` | 校验拓扑和初始窗口，初始化版本化 SQLite 状态 |
| `ingest` | 校验完整事件文件，原子写入新事件并增量推进簇 |
| `patch` | 提交依赖、实例权重或静默/维护窗口补丁 |
| `resume` | 重放持久化但尚未提交的 `ingest`/`patch` 任务 |
| `status` | 输出稳定排序 JSON：拓扑版本、簇数、失效计数、根因摘要 |
| `verify` | 从原始事件与持久化抑制账本全量重算，逐簇/逐候选校验摘要 |

所有成功和业务错误输出均为单行 JSON；`--pretty` 仅改变缩进。业务错误退出码为 `2`，`verify` 失败或 `resume` 有失败任务时退出码为 `1`。

## 输入

### 拓扑

规范格式见 `samples/topology.json`。`upstream_dependencies` 表示“本服务调用的上游/依赖服务”。

```json
{
  "topology_version": 1,
  "services": [
    {
      "name": "payment",
      "upstream_dependencies": ["db"],
      "instances": [
        {"instance_id": "payment-1", "fault_domain": "az-a", "weight": 1.5}
      ]
    }
  ]
}
```

同时兼容：

- 顶层 `instances` 列表，实例自带 `service`；
- 服务对象中使用 `service` 代替 `name`、`dependencies` 代替 `upstream_dependencies`；
- 单个服务对象内直接携带一个 `instance_id`/`id` 的扁平记录；
- 初始 `silence_windows` 或 `windows` 列表。

### 事件

规范格式见 `samples/events.json`。事件文件可写成 `{"events": [...]}`、`{"event_stream": [...]}` 或直接列表。

```json
{
  "event_seq": 1,
  "instance_id": "db-1",
  "rule": "LatencyHigh",
  "severity": "critical",
  "logical_ts": 1000,
  "metric_value": 0.95,
  "silenced": false,
  "maintenance": false
}
```

约束：事件序号在输入顺序和已提交水位上严格递增；`logical_ts >= 0`；严重度为 `info/warning/critical`；指标值为有限数；抑制标记为布尔值。

### 补丁

规范格式见 `samples/patch.json`。

```json
{
  "patch_id": "patch-002",
  "base_topology_version": 1,
  "topology_version": 2,
  "changes": [
    {
      "type": "dependency",
      "service": "inventory",
      "action": "set",
      "upstream_dependencies": ["db"]
    },
    {"type": "weight", "instance_id": "db-1", "weight": 3.0},
    {
      "type": "silence",
      "action": "add",
      "window": {
        "window_id": "silence-db-latency",
        "kind": "silence",
        "service": "db",
        "rule": "LatencyHigh",
        "start_ts": 900,
        "end_ts": 1300
      }
    }
  ]
}
```

- 窗口区间为 `[start_ts, end_ts)`。
- `kind` 为 `silence` 或 `maintenance`；`instance_id`、`rule` 可省略表示更宽作用域。
- 依赖 `action` 支持 `set/add/remove`。
- 补丁也可用顶层 `dependencies`、`weights`、`silence_windows`、`remove_silence_windows` 的简写形式。

## 确定性规则

### 聚类与代表事件

1. 按 `(service, rule)` 分组，组内按 `(logical_ts, event_seq)` 排序。
2. 相邻事件时间差 `<= window_ms` 时归入同簇；边界只使用注入的逻辑毫秒戳。
3. 已抑制事件仍属于原簇，但只能计入 `suppressed_count`。
4. 候选代表事件按以下字典序选择：

```text
severity_desc, instance_weight_desc, logical_ts_asc, event_seq_asc
```

5. 全部事件被抑制时，代表事件为 `null`。
6. NumPy 仅用于 `sum(weight)` 与 `dot(severity_rank, weight)` 聚合。

### 抑制粘滞

新增静默/维护窗口会扫描已持久化事件并写入 `event_suppressions` 抑制账本。账本一旦把事件标记为抑制，撤销窗口不会清除该标记；撤销后会重新裁决簇，但不会让历史被抑制告警重新成为代表事件。重启后只依据 SQLite 账本裁决。

### 根因候选

对每个簇的目标服务 `S`：

- 候选服务集合为 `{S}` 与全部上游依赖闭包服务；
- `causal_distance` 为到 `S` 的最短依赖距离，`S` 自身为 `0`；
- `affected_instance_ids` 为 `S` 及上游闭包内全部实例的稳定排序集合；
- 每个候选附带未被抑制的自身告警观测；
- 排序字典序为：

```text
has_own_unsuppressed_alert_desc,
earliest_alert_ts_asc,
instance_weight_desc,
causal_distance_asc,
instance_id_asc
```

每个候选的 `basis_text`、`observations` 与顶层 `aggregation_order` 都持久化，可稳定复现。

## 增量失效与复用

| 变更 | 事件簇 | 根因候选 |
| --- | --- | --- |
| 依赖关系 | 不重算 | 变更服务及其下游闭包重算 |
| 实例权重 | 含该实例事件的簇重算 | 该服务及下游闭包重算 |
| 新增/修改/撤销窗口 | 被旧/新窗口覆盖到事件的簇重算 | 窗口作用服务及下游闭包重算 |
| 新事件 | 仅对应 `(service, rule)` 组重算，摘要相同则复用 | 事件服务及下游闭包重算 |

复用的簇和候选保留原 `computed_topology_version`，新结果行记录 `reused_from_version`。每次操作把每个簇/候选的 `reuse`、`recompute` 或 `add` 决定及原因写入 `invalidation_log`。

## 事务、并发与恢复

- 所有写操作先获取 `<db>.lock` 排他文件锁，再进入 `BEGIN IMMEDIATE`。
- 新簇和新候选先写入 `stage_clusters`、`stage_candidates`，随后按版本号写入正式表，并在同一事务中整体切换 `meta.current_result_version`。
- `jobs` 表先持久化待执行任务；操作与 `jobs.status='done'` 在同一事务提交。若进程在提交前被终止，SQLite 回滚，任务仍为 `pending`，`resume` 仅重放该任务。
- 读取方使用 SQLite 读事务；WAL 保证只能看到完整提交的拓扑版本与结论。
- `patch_history`、`event_batches` 与唯一主键共同保证重复补丁/事件批次/结果只提交一次。

更完整的设计见 `docs/DESIGN.md`。

## 错误码

| 场景 | 错误码 |
| --- | --- |
| 依赖成环 | `TOPOLOGY_CYCLE` |
| 补丁引用不存在服务 | `PATCH_UNKNOWN_SERVICE` |
| 事件引用未声明实例 | `EVENT_UNKNOWN_INSTANCE` |
| 静默窗口区间非法 | `PATCH_INVALID_SILENCE_WINDOW` |
| 拓扑版本回退 | `PATCH_TOPOLOGY_VERSION_ROLLBACK` |
| 补丁基版本不匹配 | `PATCH_BASE_MISMATCH` |
| 版本被不同内容占用 | `PATCH_VERSION_CONFLICT` |
| 事件序号冲突 | `EVENT_SEQUENCE_CONFLICT` |
| 状态未初始化 | `STATE_NOT_INITIALIZED` |
| SQLite/文件锁超时 | `DB_BUSY` |

完整列表见 `alarm_dedup/errors.py`。非法输入在写事务前被拒绝；已有服务状态不会被部分破坏。

## 测试与演示

```bash
.venv/bin/python -m unittest discover -s tests -v
bash scripts/demo.sh artifacts/demo
```

`scripts/demo.sh` 在两个独立目录和独立 SQLite 文件中执行同一命令链，并逐文件比较 JSON 输出；比较失败即退出非零。当前固定样本验证结果：

- 15 条事件、12 个初始事件簇；
- `patch-002` 后依赖闭包与静默导致 3 个簇重算、9 个簇复用；
- 9 个根因候选重算、3 个根因候选复用；
- `patch-003` 撤销窗口后，事件 1 仍保持抑制且代表事件保持 `null`；
- 11 项单元测试全部通过，覆盖混合并发、重复提交、两种崩溃点恢复、幂等与异常隔离；
- 两次独立运行的所有命令输出逐字节一致；
- 最终 `verify` 为 `ok: true`，全量重算与持久化结论无差异。
