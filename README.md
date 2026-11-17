# 多集群配置分发账本诊断台

一个纯本地、无容器、仅依赖 Python 标准库的多集群配置分发诊断工具。它在 Windows 回环 TCP 端口上启动确定性集群模拟服务，并行分发配置任务，把每条结果原子写入 SQLite，并以独立本地重算结果作为权威真值。

## 功能概览

- 严格加载集群声明、配置键、版本历史、任务、配额、超时和重试设置；失败整体拒绝并给出 JSON 字段路径。
- 每集群一个 JSONL 回环服务子进程，按配置版本对本地样本做确定性聚合，返回摘要与计算耗时。
- 全局并发上限和每集群并发配额双重门槛；配额耗尽的单元只排队，不会绕过限制。
- SQLite 账本逐结果持久化摘要、校验和、计算耗时、客户端单调时钟起止时刻和交互延迟。
- Tkinter 快速视图按账本修订号做整版缓存；版本变化时替换整个视图，不混合新旧版本。
- 独立“权威重算”命令逐条校验账本校验和并按记录版本重算，给出 `E_LEDGER` / `E_VERIFY`。
- 查询和重算延迟按任务、集群、任务+集群聚合为整数定点均值/P95/最大值，保留所有超预算事件。
- 原子会话快照、强制终止后的账本恢复、声明版本不一致时的整库替换、恢复后的确定性重放。
- 可注入端口占用、版本回退、账本摘要损坏三类异常，分别报告 `E_PORT`、`E_VERSION`、`E_LEDGER`。

## 环境与运行

要求 Python 3.11 或更高版本。运行核心、服务和 CLI 不需要第三方包；Windows 官方 Python 安装包默认包含 Tkinter。

```powershell
py -3.11 --version
py -3.11 -c "import sqlite3, tkinter; print(sqlite3.sqlite_version, tkinter.TkVersion)"
```

当前 Linux 验证容器缺少 Tkinter，因此 GUI 在本容器只能验证导入与降级路径；在 Windows 上执行 `ui` 命令会打开真实 Tkinter 窗口。无 GUI 环境可使用同语义的 `query`、`verify` 命令。

### 快速演示

```powershell
py -3.11 -m cluster_ledger validate `
  --config data/clusters.json `
  --sample-data data/sample_data.json

py -3.11 -m cluster_ledger demo --workdir var/demo
```

也可使用 `scripts/run_demo.ps1` 或 `scripts/run_demo.bat`。演示会删除并重建 `var/demo` 下自己的运行文件，不会修改数据文件，然后依次执行：

1. 启动 v1 三个回环服务并分发 8 个任务/集群单元。
2. 快速查询、缓存命中、零预算延迟事件、权威重算、v1 快照。
3. 声明升级到 v2，观察旧结果陈旧化、视图整体替换、服务重启和重新分发。
4. 权威重算、最终快照、快照重放。
5. 用仅有一条已提交结果的账本模拟强制终止后恢复，7 个未完成单元标记为未知并要求重跑。
6. 用 v2 当前声明打开 v1 快照，验证声明不一致时整库替换而非合并。
7. 生成 `var/demo/report.json` 和结构化日志 `var/demo/events.jsonl`。

`demo` 故意使用 `--latency-probe-budget-ms 0` 发起一次探测查询，以确定地产生并记录 `E_LATENCY` 事件；正常界面默认预算来自声明 `limits.query_budget_ms`。

### 启动 GUI

先运行一次分发（例如完整 `demo`），再以对应声明打开最终账本：

```powershell
py -3.11 -m cluster_ledger ui `
  --config data/clusters.v2.json `
  --sample-data data/sample_data.json `
  --ledger var/demo/ledger.sqlite3 `
  --snapshot var/demo/session-final.json `
  --log var/demo/events.jsonl
```

界面每秒调用快速账本视图；只有账本修订号变化时才整版重建。修改版本后的缓存不会增量合并。按钮包括快速刷新、权威重算、保存快照和退出。

## 声明文件契约

`data/clusters.json` 是完整示例。根对象字段：

```json
{
  "schema_version": 1,
  "declaration_version": "decl-2026-11-17-v1",
  "limits": {
    "global_concurrency": 5,
    "per_cluster_concurrency": 2,
    "query_budget_ms": 25
  },
  "config_keys": ["app.timeout", "app.region", "app.replicas"],
  "clusters": [
    {
      "cluster_id": "edge-a",
      "host": "127.0.0.1",
      "port": 18201,
      "nodes": ["edge-a-n1", "edge-a-n2", "edge-a-n3"],
      "config_version": 1,
      "version_history": [1],
      "per_cluster_concurrency": 2
    }
  ],
  "tasks": [
    {
      "task_id": "runtime-rollout",
      "config_keys": ["app.timeout", "app.replicas"],
      "target_clusters": ["edge-a"],
      "aggregation": {"mode": "sha256"},
      "timeout_ms": 1200,
      "retries": 2
    }
  ]
}
```

强校验规则：

- 集群标识、端口、节点、配置键和任务标识均唯一；端口全局唯一且在 `1..65535`。
- `host` 必须是字面量 `127.0.0.1` 或 `::1`，不接受 `localhost` 或通配地址。
- `version_history` 必须严格递增且最后一项等于 `config_version`；账本中再拒绝跨声明回退。
- 任务配置键必须存在，目标集群必须已声明，聚合方式必须是 `sha256`、`sum` 或 `count`。
- 每个任务必须显式给出 `timeout_ms` 和 `retries`。
- 样本必须覆盖每个任务、目标集群、当前版本、节点和配置键；否则同样以 `E_CONFIG` 整体拒绝。

字段路径示例：`$.clusters[1].port`、`$.tasks[0].config_keys[2]`、`$.clusters[0].version_history[1]`。

## 确定性聚合

样本文件按 `clusters -> cluster_id -> version -> config_key -> node -> value` 组织。聚合前对配置键和节点排序；聚合过程中不使用随机数、壁钟或并发完成顺序。

- `sha256`：每个配置键生成节点条目的规范化 SHA-256 组件，再对排序后的组件生成总摘要。
- `sum`：要求整数样本，按配置键求和并生成总 sum。
- `count`：统计节点条目数。

真值始终是 `compute_aggregate(..., config_version=账本记录版本)` 的本地重算结果，不是服务自报结果。相同请求重复发送时，服务返回的 `summary` 和 `components` 逐字节一致；`calculation_duration_ms` 可以变化。

## 时钟与延迟口径

- 客户端：`time.perf_counter_ns()`，每个进程有独立单调原点。
- 服务端：另一个进程的 `time.perf_counter_ns()`；报告明确声明两个原点不可直接相减。
- 账本：按需求保存由客户端单调时钟截断的整数起止毫秒，同时保存纳秒原始值。
- 统计定点：`fixed = duration_ns // 1000`，单位为 1/1000 ms（即 1 微秒），JSON 中 `fixed_scale=1000`。
- P95 使用 nearest-rank，不做插值。
- 量化误差上界：0.001 ms / 1 个定点单位。预算判定直接比较纳秒差与 `budget_ms * 1_000_000`，不会因定点显示而漏报。
- 每次超预算查询同时写入 `interactions.exceeded_budget=1` 和 JSONL `E_LATENCY` 事件，事件包含起止毫秒/纳秒、数据版本和预算。

## 账本、版本与缓存

SQLite 使用 WAL 和 `synchronous=FULL`。`results` 以 `(task_id, cluster_id, config_version)` 唯一，包含摘要、组件、计算耗时、起止时刻和 `ledger_checksum`。版本升级事务会先标记该集群所有旧结果及旧版本交互为陈旧，再发布新修订号；查询只接受与当前声明版本一致且未陈旧的结果。

快速视图查询先只读取 `meta.revision`。修订号未变时直接返回完整缓存副本；变化时一次性读取一致事务快照并整版重建，绝不把新旧版本行拼接到同一视图。版本变化列表按集群标识稳定排序。

权威重算会：

1. 检查账本行校验和；损坏报告 `E_LEDGER`。
2. 按行内配置版本重算组件和摘要；差异报告 `E_VERIFY`，并给出任务、集群和首个差异键。
3. 比对当前界面结果、版本和陈旧标志；缺失、混版或摘要差异也报告 `E_VERIFY`。

## 快照、恢复与重放

快照包含声明版本/指纹、完整声明、任务、账本结果、交互、声明状态、版本迁移、延迟统计口径、已完成集群和未完成单元。写文件使用同目录临时文件、`fsync`、`os.replace`，并带快照校验和。

强制终止后恢复只把完整提交在 SQLite 中的结果视为完成；未完成单元写入 `recovery_state(status='unknown')` 并标记 `rerun_required`。报告固定 `clean_shutdown=false`、`exit_report=null`，不会伪造退出报告。快照声称完成但账本没有对应行时会单独列出。`snapshot recover` 会自动执行快照重放，重放失败时返回非零并以 `E_REPLAY` 写明任务和集群。

打开声明不同的快照时，当前账本被快照状态整体替换，返回 `replacement_mode=wholesale`、`merged=false`，并记录声明差异。重放按快照中的声明和每条结果的配置版本重新计算，同时比较摘要、组件、版本、陈旧标志、任务级聚合摘要和延迟统计口径；任何不可复现项均报告 `E_REPLAY`。

## 常用命令

```powershell
# 分发（默认自行启动服务子进程）
py -3.11 -m cluster_ledger dispatch --config data/clusters.json --sample-data data/sample_data.json --ledger var/live.sqlite3 --log var/events.jsonl

# 快速视图
py -3.11 -m cluster_ledger query --config data/clusters.json --ledger var/live.sqlite3 --log var/events.jsonl

# 权威重算
py -3.11 -m cluster_ledger verify --config data/clusters.json --sample-data data/sample_data.json --ledger var/live.sqlite3 --log var/events.jsonl

# 延迟统计
py -3.11 -m cluster_ledger stats --ledger var/live.sqlite3

# 快照
py -3.11 -m cluster_ledger snapshot save --config data/clusters.json --sample-data data/sample_data.json --ledger var/live.sqlite3 --snapshot var/session.json --log var/events.jsonl
py -3.11 -m cluster_ledger snapshot open --config data/clusters.json --ledger var/open.sqlite3 --snapshot var/session.json --log var/events.jsonl
py -3.11 -m cluster_ledger snapshot replay --snapshot var/session.json --sample-data data/sample_data.json --log var/events.jsonl
py -3.11 -m cluster_ledger snapshot recover --config data/clusters.json --sample-data data/sample_data.json --ledger var/live.sqlite3 --snapshot var/session.json --log var/events.jsonl
```

异常注入：

```powershell
py -3.11 -m cluster_ledger diagnose --anomaly port --config data/clusters.json --sample-data data/sample_data.json --log var/events.jsonl
py -3.11 -m cluster_ledger diagnose --anomaly version_rollback --config data/clusters.json --sample-data data/sample_data.json --log var/events.jsonl
py -3.11 -m cluster_ledger diagnose --anomaly ledger --config data/clusters.v2.json --sample-data data/sample_data.json --ledger var/demo/ledger.sqlite3 --log var/events.jsonl
```

业务码：

| 业务码 | 含义 |
|---|---|
| `E_CONFIG` | 声明/样本/字段路径校验失败，整体拒绝 |
| `E_PORT` | 回环端口占用或服务未就绪 |
| `E_VERSION` | 版本回退、服务版本不匹配、声明不一致 |
| `E_LEDGER` | 账本行或快照校验和损坏 |
| `E_VERIFY` | 权威重算与账本/界面结果不一致 |
| `E_LATENCY` | 单次界面查询超过显式预算 |
| `E_REPLAY` | 快照重放不可复现 |

## 测试

```powershell
py -3.11 -m unittest discover -s tests -v
```

测试使用真实回环子进程和 SQLite，覆盖配置失败路径、确定性摘要、双配额调度、账本/视图/重算、超预算事件、版本整体替换、快照重放、强制 `kill` 恢复以及三类异常日志。详细实际结果见 `docs/validation.md`，设计说明见 `docs/architecture.md`。
