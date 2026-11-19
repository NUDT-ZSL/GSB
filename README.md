# 云原生可观测性事件时间窗降噪关联工作台

这是一个零第三方依赖、可重复验证的本地工作台。它只读取样本中的逻辑时钟，使用 SQLite 持久化事件时间窗、降噪结论、证据链、迟到簿记和补丁失效清单。

核心目标：

- 按事件发生时钟推进每个 `(instance_id, rule_name)` 的水位，而不是按到达时间。
- 乱序事件归属固定事件时间窗口；晚于允许迟到阈值后关闭窗口。
- 同一实例同一规则去重、统计静默抑制数、稳定选择代表事件并生成拓扑上游证据链。
- 拓扑补丁沿依赖反向闭包做增量失效，未受影响窗口保留原版本字节内容。
- 补丁传播采用“暂存版本 → 完成失效重算 → 原子发布指针”，查询不会看到新旧拓扑混合视图。
- 进程强制终止后由 SQLite 状态恢复，只处理未完成失效项，不重复计入已关闭窗口事件。

## 目录

```text
obs_workbench/
  __main__.py       python -m obs_workbench 入口
  cli.py            load/ingest/apply/resume/status/verify
  engine.py         事件时间引擎、失效传播、恢复与查询
  samples.py        JSON 解析与业务校验
  storage.py        SQLite schema、事务、文件锁与版本化读取
  timewin.py        水位、窗口边界、去重、代表事件和证据链纯函数
  topology.py       拓扑模型、DAG 校验、上游闭包/反向闭包
  canonical.py      稳定 JSON 与 SHA-256
  faults.py         仅用于恢复测试的确定性崩溃注入
workbench.py        Windows 控制台友好入口
samples/
  topology.json             拓扑 v1
  events.json               固定乱序事件桶
  patch.json                拓扑 v2 补丁
  anomalies/                异常样本和异常注入清单
tests/
  test_workbench.py         标准库 unittest 端到端测试
data/                       SQLite/WAL/lock 文件默认位置（运行时创建）
```

## 环境与运行

要求 Python 3.10+，仅使用标准库 `sqlite3`、`json`、`hashlib`、`argparse`。在项目根目录运行：

```powershell
python --version
python -m obs_workbench --db data\workbench.sqlite3 load --topology samples\topology.json
python -m obs_workbench --db data\workbench.sqlite3 ingest --events samples\events.json
python -m obs_workbench --db data\workbench.sqlite3 status --output data\status-v1.json
python -m obs_workbench --db data\workbench.sqlite3 apply --patch samples\patch.json
python -m obs_workbench --db data\workbench.sqlite3 status --output data\status-v2.json
python -m obs_workbench --db data\workbench.sqlite3 verify
```

也可以使用 `python workbench.py ...`。所有路径由 `pathlib.Path` 处理，命令默认使用相对路径 `data/workbench.sqlite3`。输出为稳定排序、紧凑 UTF-8 JSON；`status` 和 `verify` 支持 `--output` 写入文件。

退出码：

- `0`：成功或校验通过。
- `1`：未预期的内部错误。
- `2`：业务校验失败、补丁失效失败，或 `verify` 校验不通过。
- `97`：仅测试中通过 `OBS_FAILPOINT` 模拟强制终止。

## 样本格式

### 拓扑快照

```json
{
  "topology_version": 1,
  "effective_clock_ms": 0,
  "window_ms": 60000,
  "allowed_lateness_ms": 10000,
  "services": [
    {
      "service_name": "payment-api",
      "failure_domain": "core",
      "upstream_services": ["ledger-db"]
    }
  ],
  "instances": [
    {
      "instance_id": "pay-1",
      "service_name": "payment-api",
      "failure_domain": "core-a",
      "weight": 1.0
    }
  ],
  "silence_windows": [
    {
      "silence_id": "sil-1",
      "instance_id": "pay-1",
      "rule_name": "CPUThrottle",
      "start_clock_ms": 45000,
      "end_clock_ms": 52000,
      "kind": "maintenance"
    }
  ]
}
```

服务也可内嵌 `instances`；依赖字段兼容 `upstream`、`dependencies` 和对象形式。服务名、实例 ID 必须唯一；依赖两端闭合、无自环且不得成环。

### 事件桶

```json
{
  "buckets": [
    {
      "bucket_id": "b1",
      "events": [
        {
          "event_seq": 10,
          "instance_id": "pay-1",
          "rule_name": "CPUThrottle",
          "severity": "critical",
          "occurred_clock_ms": 51000,
          "arrived_clock_ms": 25000,
          "metric_value": 88.0,
          "silenced_maintenance": false
        }
      ]
    }
  ]
}
```

严重度枚举为 `info`、`warning`、`critical`。`event_seq` 为唯一非负整数或非空字符串。事件按 `(arrived_clock_ms, event_seq)` 稳定排序后处理，因此 JSON 桶内部/桶之间的书写顺序不改变结果。

### 拓扑补丁

```json
{
  "parent_topology_version": 1,
  "patched_topology_version": 2,
  "effective_clock_ms": 220000,
  "changes": {
    "dependency_add": [
      {"service_name": "inventory-api", "upstream_service": "payment-api"}
    ],
    "dependency_remove": [
      {"service_name": "edge-api", "upstream_service": "inventory-api"}
    ],
    "instance_weights": [
      {"instance_id": "pay-1", "weight": 1.8}
    ],
    "silence_add": [
      {
        "silence_id": "sil-pay-late",
        "instance_id": "pay-2",
        "rule_name": "CPUThrottle",
        "start_clock_ms": 100000,
        "end_clock_ms": 110000,
        "kind": "silence"
      }
    ],
    "silence_remove": ["sil-pay-cpu-maint"]
  },
  "content_digest": "sha256:..."
}
```

`content_digest` 的规范内容为：

```json
{
  "changes": {
    "dependency_add": [],
    "dependency_remove": [],
    "instance_weights": [],
    "silence_add": [],
    "silence_remove": []
  },
  "effective_clock_ms": 220000,
  "parent_topology_version": 1,
  "patched_topology_version": 2
}
```

计算方式为对上述对象执行排序键紧凑 JSON 序列化后取 SHA-256。也接受对“去掉 `content_digest` 的原始对象”计算的摘要，便于手工构造样本。

## 确定性语义

### 窗口与水位

设窗口宽度 `W = window_ms`，允许迟到 `L = allowed_lateness_ms`：

```text
window_start = floor(occurred_clock_ms / W) * W
window_end   = window_start + W - 1
close when   watermark > window_end + L
late when    occurred_clock_ms < current_watermark
```

水位只取已到达且未迟到事件的发生时钟最大值。迟到事件不推进水位。迟到但目标窗口尚未关闭时进入 `LATE_OPEN` 并参与后续关闭；目标窗口已经关闭时进入 `LATE_CLOSED` 和迟到簿记，不修改当前已发布结论。

事件到达只决定处理顺序，不参与窗口边界或关闭裁决。实现中没有读取系统墙钟，也没有 `time.time()`、`datetime.now()` 或随机数。

### 去重、抑制和代表事件

去重指纹固定为：

```text
(occurred_clock_ms, severity, metric_value)
```

原始告警数保留在 `raw_event_count`，去重数写入 `deduplicated_event_count`。

事件被抑制的条件为“发生时刻落在当前拓扑版本的静默/维护区间内”。输入标记 `silenced_maintenance=true` 会在灌入时绑定到当时命中的静默区间；补丁撤销该区间后，事件可由新版本重新裁决。若灌入时没有任何拓扑区间能解释该标记，则它作为独立来源抑制保留。抑制原因以稳定排序参与结果计算；`suppressed_count` 统计原始告警数。

代表事件仅从非抑制事件中选择，排序键为：

```text
severity 降序 -> instance weight 降序 -> event_seq 升序
```

全部事件都被抑制时结论为 `SUPPRESSED_ONLY` 且代表事件为空。

### 证据链

从代表事件所在实例出发，沿服务 `upstream_services` 枚举全部确定性上游路径，并为路径上的每个上游实例生成证据项。每项包含：

- `topology_version`
- `kind=source|upstream`
- `depth`
- `path` 和 `edge_path`
- `source_instance_id`
- `instance_id`、`service_name`、`failure_domain`
- `representative_event_seq`

证据和结论分别保存稳定 JSON，并用 SHA-256 摘要绑定为同一窗口版本记录。

## 增量失效与版本发布

补丁触发字段分三类：

1. 依赖增删：从被修改服务沿反向依赖边计算闭包。闭包内服务的窗口需要重建证据链；若补丁同时改变静默/权重，则重算结论。
2. 实例权重变更：只触发该实例的窗口，重算代表事件和相关证据。
3. 静默增删：只触发实例/服务、规则和发生时间实际重叠的窗口，重算抑制数、代表事件，并将该窗口已有迟到簿记纳入再裁决。

失效动作明确区分：

```json
{
  "conclusion": "REUSE",
  "evidence_chain": "REBUILD",
  "late_ledger": "REUSE"
}
```

或 `REBUILD` / `RECHECK` / `DEFER_TO_CLOSE`。每条失效记录保存：

- `patch_version`
- `triggers`
- `propagation_paths`
- `actions`
- 受影响的 `window_key`

未命中闭包或静默区间的窗口不创建失效记录，也不改写其结论或证据。补丁版本中新增的窗口修订以 `staged=1` 暂存；全部失效项完成后，在一个事务中：

1. 将暂存修订切换为当前修订；
2. 用临时表 `publish_pointer_stage` 整体替换已发布版本指针；
3. 翻转 `topologies.published`；
4. 标记 job 为完成。

因此查询事务在补丁传播期间仍读取旧的完整版本，不会看到“新拓扑 + 部分旧结论”。

## 持久化与恢复

SQLite 主要表：

- `state`：schema 版本、窗口配置、已发布/最大已见拓扑版本。
- `topologies`、`topology_services`、`topology_instances`、`topology_silences`
- `events`、`watermarks`、`windows`、`window_events`、`window_revisions`
- `late_ledger`
- `jobs`、`invalidations`

并发控制：

- 每个数据库旁使用 `<db>.lock` 文件锁串行化写者；POSIX 用 `fcntl`，Windows 用 `msvcrt`。
- SQLite 使用 WAL、`BEGIN IMMEDIATE`、唯一约束和事务。
- 同一拓扑版本、同一窗口同一版本结论均有唯一键。
- 读者显式开启快照事务，只读取一个完整提交版本。
- 重复补丁、重复事件文件、重复窗口结果均幂等。

恢复：

- 补丁意图、拓扑暂存、job 和全部失效项先在单个事务中提交。
- 每个失效窗口的“暂存修订 + 失效完成标记”在独立事务中提交；崩溃后 `resume` 只处理 `PENDING` 项。
- 窗口关闭事件处理在单事务中完成；中途终止时 SQLite 回滚全部事件和窗口写入。重新灌入相同事件不会重复计入。
- `resume` 在全部失效项完成后才原子发布版本。

## 命令

```text
load --topology PATH
ingest --events PATH [--isolate-buckets]
apply --patch PATH
resume
status [--output PATH]
verify [--output PATH]
```

默认 `ingest` 对整份事件文件原子校验：任一事件非法则整份文件不写入。`--isolate-buckets` 按桶隔离，坏桶不写入，其他桶继续推进，便于演示“单个异常不阻断其他实例”。

`status` 稳定输出：

- 当前完整拓扑版本和摘要；
- `(instance_id, rule_name)` 水位；
- 每个窗口状态、当前结论、当前版本证据链和全部历史修订；
- 迟到簿记；
- 已发布补丁的失效清单；
- 未完成 job 摘要。

`verify` 会重新计算：

- 拓扑规范摘要；
- 所有已发布窗口修订的内容摘要；
- 每个历史/当前窗口结论与证据链；
- 水位与已接受的 `ON_TIME` 事件最大值；
- 迟到簿记合理性；
- 事件是否落在其声明窗口内。

## 业务错误码

| 场景 | 错误码 |
|---|---|
| 拓扑结构非法 | `E_TOPOLOGY_INVALID` |
| 服务名重复 | `E_TOPOLOGY_DUPLICATE_SERVICE` |
| 实例 ID 重复 | `E_TOPOLOGY_DUPLICATE_INSTANCE` |
| 依赖引用未声明服务 | `E_TOPOLOGY_UNKNOWN_DEPENDENCY` |
| 服务自依赖 | `E_TOPOLOGY_SELF_DEPENDENCY` |
| 依赖成环 | `E_TOPOLOGY_CYCLE` |
| 拓扑版本回退 | `E_TOPOLOGY_VERSION_ROLLBACK` |
| 拓扑版本号非法 | `E_TOPOLOGY_VERSION_INVALID` |
| 拓扑生效逻辑时钟非法 | `E_TOPOLOGY_CLOCK_INVALID` |
| 同一版本内容冲突 | `E_TOPOLOGY_VERSION_CONFLICT` |
| 窗口配置在初始化后变化 | `E_TOPOLOGY_CONFIG_MISMATCH` |
| 事件结构非法 | `E_EVENT_INVALID` |
| 文件内事件序号重复 | `E_EVENT_SEQUENCE_DUPLICATE` |
| 已持久化事件序号内容冲突 | `E_EVENT_SEQUENCE_CONFLICT` |
| 发生/到达时钟非法 | `E_EVENT_CLOCK_INVALID` |
| 严重度非法 | `E_EVENT_SEVERITY_INVALID` |
| 事件引用未声明实例 | `E_EVENT_UNKNOWN_INSTANCE` |
| 补丁版本不连续 | `E_PATCH_VERSION_SEQUENCE` |
| 补丁父版本不是已发布版本 | `E_PATCH_PARENT_MISMATCH` |
| 补丁内容摘要不匹配 | `E_PATCH_DIGEST_MISMATCH` |
| 补丁引用未声明实例 | `E_PATCH_UNKNOWN_INSTANCE` |
| 补丁引用未声明服务 | `E_PATCH_UNKNOWN_SERVICE` |
| 删除不存在的依赖 | `E_PATCH_UNKNOWN_DEPENDENCY` |
| 删除不存在的静默窗口 | `E_PATCH_UNKNOWN_SILENCE` |
| 静默区间端点倒置 | `E_SILENCE_RANGE_INVALID` |
| 窗口修订摘要不匹配 | `E_WINDOW_DIGEST_MISMATCH` |
| 存在未完成补丁传播 | `E_RESUME_REQUIRED` |

错误输出格式：

```json
{
  "error": {
    "code": "E_PATCH_UNKNOWN_INSTANCE",
    "details": {"instance_id": "ghost", "target_topology_version": 2},
    "message": "weight change references an undeclared instance"
  },
  "ok": false
}
```

## 异常样本

`samples/anomalies/manifest.json` 列出异常注入用例及前置条件。异常补丁的摘要已按规范计算，可直接用于：

```powershell
python -m obs_workbench --db data\workbench.sqlite3 apply --patch samples\anomalies\patch_unknown_instance.json
python -m obs_workbench --db data\workbench.sqlite3 apply --patch samples\anomalies\patch_silence_reversed.json
python -m obs_workbench --db data\workbench.sqlite3 apply --patch samples\anomalies\patch_dependency_cycle.json
python -m obs_workbench --db data\workbench.sqlite3 apply --patch samples\anomalies\patch_version_rollback.json
python -m obs_workbench --db data\workbench.sqlite3 apply --patch samples\anomalies\patch_digest_mismatch.json
python -m obs_workbench --db data\workbench.sqlite3 ingest --events samples\anomalies\events_unknown_instance.json
```

注意 `patch_version_rollback.json` 的前置条件是主样本补丁 v2 已发布。

## 测试

```powershell
python -m unittest discover -s tests -v
python -m compileall -q obs_workbench tests workbench.py
```

测试覆盖：

- 乱序窗口归属、水位、迟到簿记、去重、抑制、代表事件和证据链；
- 补丁反向闭包、结论复用/重建、静默撤销后的迟到事件再裁决；
- 输入静默标记与拓扑区间的绑定，以及撤销区间后的抑制释放；
- 未受影响窗口的历史字节复用；
- 重复补丁、重复事件和重复窗口结果幂等；
- 依赖成环、未知实例、静默倒置、版本回退、摘要不匹配等独立错误码；
- 坏桶隔离；
- 补丁传播中断、窗口关闭中断后的恢复；
- 补丁后新迟到事件不重写已关闭版本；
- 两进程并发 ingest/apply；
- 完整 `verify`。

实际验证结果、输出摘要和哈希见 [docs/VERIFICATION.md](docs/VERIFICATION.md)。
