# Experiment Trace Workbench

本地、确定性的实验追踪与版本管理诊断工作台。它把两代追踪快照合并为一个带完整裁决依据的统一视图，以注入的逻辑时钟推进指标窗口刷新，在实验配置/数据/父实验变化时计算依赖失效闭包，并用“原始谱系 + 轨迹全量重算”校验增量状态。

业务时间只来自输入 JSON 和命令参数中的 `*_ms` 逻辑时钟；代码不调用系统墙钟。SQLite 使用 Python 标准库 `sqlite3`，所有命令可在 Windows 命令提示符或 PowerShell 中运行。

## 安装

要求 Python 3.11 或更高版本。PowerShell 示例：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

命令提示符可将激活命令替换为 `.\.venv\Scripts\activate.bat`。

## 快速验证

仓库包含可直接运行的确定性样本。以下命令均使用同一个 `workbench.db`：

```powershell
python -m expworkbench --db workbench.db load `
  --package samples/packages/package-001.json `
  --experiments samples/data/experiments.json `
  --metrics samples/data/metrics.json

python -m expworkbench --db workbench.db refresh --now-ms 1761000005000 --window-ms 1000
python -m expworkbench --db workbench.db status
python -m expworkbench --db workbench.db verify --now-ms 1761000005000

python -m expworkbench --db workbench.db patch `
  --file samples/patches/patch-tune-hyperparameters.json

python -m expworkbench --db workbench.db load `
  --package samples/packages/package-002-append.json

python -m expworkbench --db workbench.db refresh --now-ms 1761000009000
python -m expworkbench --db workbench.db diagnose --now-ms 1761000009000
python -m expworkbench --db workbench.db web
```

浏览器访问 `http://127.0.0.1:5000/`。界面提供谱系、指标窗口、基线偏差、失效来源链、被否决复用候选和真值分歧展示。

## 命令

| 命令 | 作用 |
| --- | --- |
| `load` | 校验并装载 v1/v2 快照包，构建不可变统一视图并原子切换指针 |
| `refresh` | 用注入的逻辑时钟推进已闭合指标窗口，提交摘要与基线对照 |
| `patch` | 提交配置/数据/父实验补丁，计算失效闭包并重算受影响指标 |
| `diagnose` | 输出状态并执行增量路径与全量真值的逐窗口对照 |
| `resume` | 重放强制终止后遗留的未完成失效事件 |
| `status` | 稳定排序 JSON：快照版本、指标摘要、精度标记、失效链 |
| `verify` | `diagnose` 的校验模式；不一致时退出码为 1 |
| `web` | 启动本地 Flask 可视化工作台 |

所有命令都输出 JSON。业务错误形如：

```json
{
  "ok": false,
  "error": {
    "code": "E_SNAPSHOT_ROLLBACK",
    "message": "Snapshot version rollback is forbidden",
    "context": {}
  }
}
```

## 输入契约

### 实验谱系

```json
{
  "experiments": [
    {
      "id": "exp-tune",
      "parent_id": "exp-base",
      "hyperparameters": {"learning_rate": 0.05, "batch_size": 32},
      "dataset_version": "ds-v1",
      "code_commit": "commit-tune-001",
      "baseline_metrics": {"loss": 1.8},
      "logical_clock_ms": 1761000000200,
      "metric_references": {
        "loss_delta": {"experiment_id": "exp-base", "metric": "loss"}
      },
      "aliases": ["tune-loss"],
      "fingerprint": "sha256:..."
    }
  ]
}
```

实验 ID 全局唯一；父实验必须存在且无环；超参值仅允许标量 JSON 类型，数值必须有限；基线指标必须为有限数。可选 `fingerprint` 会与规范化声明比对。

规范化指纹的 SHA-256 输入字段固定为：

```text
id, parent_id, hyperparameters, dataset_version, code_commit,
baseline_metrics, logical_clock_ms, metric_references
```

对象键递归排序，数值使用精确十进制规范形式，字符串使用 UTF-8。v1 条目指纹可以是裸 SHA-256 十六进制或带 `sha256:` 前缀；补丁修改后，历史指纹仍可合法校验旧快照。

### 指标轨迹

```json
{
  "records": [
    {
      "record_id": "rec-0001",
      "seq": 1,
      "experiment_id": "exp-base",
      "metric": "loss",
      "step": 1,
      "value": 2.0,
      "logical_clock_ms": 1761000000500,
      "record_digest": "sha256:..."
    }
  ]
}
```

`seq` 全局唯一且记录按它排序；同一 `(experiment_id, metric)` 的 `step` 严格递增、逻辑时钟非递减；值为有限 JSON 数。缺失 `record_id` 时使用 `seq:<n>`。

记录摘要的规范化输入固定为 `record_id, seq, experiment_id, metric, step, value, logical_clock_ms`，其 SHA-256 为 `sha256:<hex>`。

### v1 单文件快照

```json
{
  "snapshot_version": "legacy-001",
  "tracking_generation": 1,
  "logical_clock_ms": 1761000000100,
  "entries": [
    {
      "alias": "base-loss",
      "experiment_id": "exp-base",
      "metric": "loss",
      "relative_refresh_seconds": 1,
      "fingerprint": "sha256:..."
    }
  ]
}
```

v1 别名唯一，相对刷新秒数必须是非负整数。装载时按 `base_clock + seconds * 1000` 换算，并持久化 `conversion_scale=1000`、`conversion_remainder_ms=0`、`rounding_direction=floor` 与原始时间表示。

### v2 分片与 SQLite 元数据库

manifest 示例：

```json
{
  "snapshot_version": "trace-v2.001",
  "version_number": 2,
  "tracking_generation": 2,
  "tracking_generation_floor": 2,
  "logical_clock_ms": 1761000002000,
  "metadata_db": "metadata.sqlite3",
  "shards": [
    {"path": "shards/shard-000.json", "sha256": "sha256:...", "trace_generation": 2}
  ]
}
```

每个分片声明数值型绝对刷新毫秒、代数、指标摘要和墓碑：

```json
{
  "snapshot_version": "trace-v2.001",
  "tracking_generation": 2,
  "logical_clock_ms": 1761000002000,
  "entries": [
    {
      "entry_id": "v2-base-loss",
      "alias": "base-loss",
      "experiment_id": "exp-base",
      "metric": "loss",
      "absolute_refresh_at_ms": 1761000001800,
      "trace_generation": 2,
      "metric_digest": "sha256:...",
      "tombstone": false
    }
  ]
}
```

装载器校验分片 SHA-256、代数上下限和分片代数单调性。SQLite 元数据库可以只含元数据表，也可以是空占位文件；若存在 `tracking_metadata`、`tracking_shards` 或 `tracking_entries`，其内容必须与 manifest/分片一致。

### 快照包

`package-001.json` 是同时引用两代的清单：

```json
{
  "base_dir": "..",
  "snapshot_version": "pkg-001",
  "version_number": 1,
  "tracking_generation_floor": 1,
  "logical_clock_ms": 1761000002000,
  "v1": {"path": "tracking/v1.json", "sha256": "sha256:..."},
  "v2": {"manifest": "tracking/v2/manifest.json", "sha256": "sha256:..."}
}
```

`base_dir` 内的所有引用路径必须位于声明根目录内。

## 合并与裁决

同一 `(experiment_id, metric)` 的候选条目按以下顺序取最新：

1. 快照声明的逻辑时钟或条目来源时钟降序；
2. 追踪代数降序；
3. 来源格式优先级降序（v2 = 2，v1 = 1）；
4. 条目键升序，作为完全并列时的确定性裁决。

每个统一视图条目保存选中来源、原始时间表示、绝对刷新时刻、换算标度、余量、舍入方向、全部候选及其排名，以及 `decision_json` 裁决依据。

## 窗口与数值确定性

- 窗口边界为 `floor(record_clock / window_ms) * window_ms`，半开区间 `[start, end)`。
- 只提交 `end <= injected_now_ms` 的窗口；边界上的新记录进入下一窗口。
- 一个 `(snapshot_version, record_id)` 只能绑定一个窗口，由数据库主键约束。
- 聚合顺序固定为 `(logical_clock_ms, seq, record_id)`。
- 使用 `Decimal` 精确求和，均值以 12 位小数、`ROUND_HALF_EVEN` 固定舍入。窗口与指标摘要均保存规范 JSON 和 SHA-256。
- 逻辑时钟水位只能前进；回退返回 `E_CLOCK_REGRESSION`。

补丁失效闭包按以下关系传播：

- 被修改实验的全部指标；
- 该实验所有后代实验的全部指标；
- `metric_references` 指向已失效指标时，引用方指标失效；
- 引用方指标失效后，其所有后代的指标一并失效。

每个失效项保存原因、来源链、旧指标摘要和所有旧窗口摘要作为“被否决的复用候选”。未受影响窗口、指标摘要和基线结论不会被重写。

## 版本与持久化

装载时先完整校验输入，再创建/修改 SQLite。非法输入不会留下状态文件。

快照比较清单版本号、追踪代数、分片哈希集合和指标摘要集合，分类为：

- `noop`：同版本同内容；
- `incremental_append`：新哈希和指标集合均包含旧集合；
- `full_replace`：其余合法升级；
- 版本号或代数回退：`E_SNAPSHOT_ROLLBACK`；
- 同版本不同内容：`E_SNAPSHOT_CONFLICT`。

所有快照条目按版本不可变保存。写入使用 SQLite TEMP 表暂存，完成后在同一事务内切换 `active_snapshot_version` 指针。刷新运行绑定开始时的快照版本；`status`/`diagnose` 使用单个读事务，读取指针、窗口和失效结论时不会混合新旧版本。

SQLite 使用 WAL、`BEGIN IMMEDIATE` 写事务、唯一约束和跨平台 `.lock` 文件。补丁、刷新与快照更新进程串行提交；同一刷新、补丁或快照版本重复提交是幂等的。

## 中断恢复

持久化表原子保存快照指针、逻辑时钟水位、数据代、刷新水位、窗口摘要、基线对照、补丁和失效事件。

测试可通过环境变量主动在事务边界强制退出（仅用于验证，不参与业务时间判断）：

| 环境变量值 | 中断点 |
| --- | --- |
| `after_patch_commit` | 补丁与待处理失效事件已提交、指标尚未重算 |
| `before_invalidation_commit` | 失效重算事务提交前，整个重算回滚 |
| `after_refresh_commit` | 刷新事务已提交 |
| `before_snapshot_pointer_switch` | 新快照临时数据写入后、指针切换前 |

PowerShell 示例：

```powershell
$env:EXPWORKBENCH_FAULT = "after_patch_commit"
python -m expworkbench --db workbench.db patch --file samples/patches/patch-tune-hyperparameters.json
Remove-Item Env:EXPWORKBENCH_FAULT
python -m expworkbench --db workbench.db resume
python -m expworkbench --db workbench.db verify --now-ms 1761000006000
```

`resume` 只重放 `status != complete` 的失效事件。窗口提交、刷新水位和事件完成状态处于单个事务中，因此不会重复计入记录或改写已提交结论。快照切换中断后旧指针保持有效，重试会重新走相同的版本判定。

## 真值诊断

`diagnose` 同时运行两条路径：

- 增量路径：读取当前快照下已提交窗口、指标摘要和基线对照；
- 真值路径：从实验谱系、当前配置、原始轨迹和统一快照视图全量重算。

每个指标按窗口比较摘要、均值、和、极值、精度标记、谱系指纹和基线比较摘要。首个分歧包含：

```json
{
  "experiment_id": "exp-base",
  "metric": "loss",
  "window_number": 1761000000,
  "window_start_ms": 1761000000000,
  "incremental_value": {"mean": "..."},
  "truth_value": {"mean": "..."},
  "depends_on_snapshot_version": "pkg-001"
}
```

未完成失效事件、缺失/多余窗口、缺失指标摘要也会作为分歧输出。

## 错误码

常用稳定业务错误码：

`E_DUP_EXPERIMENT_ID`、`E_PARENT_MISSING`、`E_LINEAGE_CYCLE`、`E_HYPERPARAM_TYPE`、`E_METRIC_REFERENCE`、`E_METRIC_SEQ`、`E_METRIC_ID_DUP`、`E_METRIC_STEP`、`E_METRIC_VALUE`、`E_METRIC_DIGEST`、`E_V1_ALIAS_DUP`、`E_V1_FINGERPRINT`、`E_V1_REFRESH`、`E_V2_HASH`、`E_V2_GENERATION`、`E_V2_METADATA`、`E_SNAPSHOT_ROLLBACK`、`E_SNAPSHOT_CONFLICT`、`E_CLOCK_REGRESSION`、`E_WINDOW_ASSIGNMENT`、`E_PATCH_CONFLICT`、`E_PENDING_RECOVERY`。

## 测试

```powershell
python -m unittest discover -s tests -v
```

当前实现实际验证结果：

- 16 个测试全部通过，覆盖合并裁决、换算溯源、固定窗口、刷新幂等、三类补丁闭包（含非后代指标引用传播）、增量追加、完整替换、回退拒绝、两处补丁中断、刷新中断、快照切换中断、双进程刷新/补丁/快照更新并发、真值篡改检测与 Web API。
- 初始样本刷新提交 25 个窗口；重复刷新返回同 `run_id` 和 `duplicate=true`，记录与窗口均不重复。
- `exp-tune` 超参补丁只失效 `exp-tune.loss`、`exp-tune.accuracy`、`exp-eval.loss_delta`；`exp-base` 的指标对象逐字段保持不变。
- `exp-base` 数据版本补丁使全部 5 个活动指标失效，父实验补丁只使 `exp-eval.loss_delta` 失效。
- 在补丁提交后、失效提交前、刷新提交后和快照指针切换前强制终止，恢复后的 `status` 与未中断运行逐字段一致，`verify` 返回 `consistent=true`。
- 最终样本 `load -> refresh -> patch -> incremental_append -> full_replace -> refresh -> verify` 的退出码为 0，窗口真值对照无分歧。

同一结果的机器可读摘要位于 `artifacts/verification-summary.json`。

样本可重新生成：

```powershell
python samples/build_samples.py
```
