# 本地 ML 推理服务弹性伸缩决策工作台

面向本地 Windows 环境的确定性模型服务调度工作台。用户用 JSON 声明推理服务集群；工作台按分钟并行估算各服务所需副本，在全局副本预算和每服务并发配额下统一裁剪，核算可承载请求与拒绝量，并把每个完整分钟、声明版本、快照和重放结果持久化到本地 SQLite。

运行时不访问网络、不使用 Docker/容器，也不需要回环端口。固定顺序的本地样本、SQLite 账本和 Tkinter 工作台即可完成全量推进、版本重推、强制中断恢复、快照恢复与逐字段重放。

## 快速开始

### Windows 环境

建议使用 Python 官方 Windows x64 安装包（3.11 或更高版本），安装时保留 `Tcl/Tk` 与 `pip` 组件。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -c "import sys, sqlite3, tkinter, numpy; print(sys.version); print(sqlite3.sqlite_version, tkinter.TkVersion, numpy.__version__)"
```

如果 `tkinter` 导入失败，请改用包含 Tcl/Tk 的官方 Python 安装包；它不是通过 `pip` 安装的扩展。NumPy 用于整数/浮点辅助计算，核心整数决策在 NumPy 缺失时也有纯 Python 回退，但正常部署应按 `requirements.txt` 安装。

### 运行

```powershell
python -m unittest discover -s tests -v
python scripts\demo.py
python scripts\gui_smoke.py
python scripts\gui.py
```

其他入口：

```powershell
python -m model_service_workbench validate --declaration samples\cluster.json
python -m model_service_workbench run --declaration samples\cluster.json --ledger artifacts\workbench\ledger.sqlite
python -m model_service_workbench gui --check
```

## 项目结构

```text
model_service_workbench/
  declaration.py   声明解析、强校验、语义差异与版本范围
  demand.py        并行整数需求估算与 E_DEMAND
  scheduler.py     统一裁剪、请求分配与 E_ALLOC 自检
  ledger.py        SQLite 账本、版本失效、恢复、快照与 E_REPLAY
  workbench.py     应用编排 API
  ui.py            Tkinter 决策工作台
  cli.py           本地命令行入口
samples/
  cluster.json             基准声明，版本 1
  cluster_modified.json    beta 并发上限 9 -> 6，重新推进后版本 2
scripts/
  demo.py                  三场景确定性演示
  gui.py                   Windows 图形入口
tests/                     30 项 unittest 测试
artifacts/demo_output/     演示实际输出
VALIDATION.md              实际验证记录与当前环境限制
```

## 声明格式

```json
{
  "schema_version": 1,
  "cluster_name": "local-ml-inference",
  "declaration_version": 1,
  "global_replica_budget": 10,
  "services": [
    {
      "name": "beta",
      "requests": [0, 9, 27, 45, 63, 54, 36, 18],
      "concurrency_per_replica": 9,
      "request_latency_ms": 55,
      "scale_threshold": 0.8,
      "min_replicas": 1,
      "max_replicas": 7,
      "concurrency_quota": 6
    }
  ]
}
```

字段语义：

- `requests`：从第 0 分钟开始连续排列的非负整数负载序列。
- `concurrency_per_replica`：每副本可同时承载的请求量，也是请求容量上限。
- `concurrency_quota`：本工作台中按“副本单位”解释的每服务分配上限，最终副本数为 `min(max_replicas, concurrency_quota)` 以下。
- `scale_threshold`：扩容压力阈值，写入决策记录并计算 `threshold_triggered`；验收要求的需求公式仍以 `concurrency_per_replica` 为可承载量，不把阈值混入硬容量。
- `global_replica_budget`：一分钟内全部服务最终副本数之和的硬上限。

运行前会强校验服务名唯一、负载为非负整数、并发上限/耗时/预算为正整数、阈值范围、配额为正整数及 `min_replicas <= max_replicas`。任一错误整体拒绝加载，返回 `E_DECL` 和 JSON 字段路径，例如：

```json
{
  "code": "E_DECL",
  "path": "$.services[1].requests[3]",
  "message": "minute request count must be a non-negative integer"
}
```

## 决策规则

### 1. 并行需求估算

对服务 `s` 和分钟 `m`：

```text
capacity(s)        = concurrency_per_replica(s)
unclamped(s,m)     = ceil(requests(s,m) / capacity(s))
demand(s,m)        = clamp(unclamped(s,m), min_replicas(s), max_replicas(s))
```

负载为 0 时未夹取需求为 0，再按最小副本数夹取。各服务在 `ThreadPoolExecutor` 中并行估算；结果按服务名排序后交给统一裁剪阶段，因此线程完成先后不影响任何输出。缺失负载分钟、并发上限等必要输入时返回 `E_DEMAND` 并列出 `missing_inputs`。

### 2. 统一裁剪

所有服务估算完成后只进行一次提交式裁剪：

1. 先应用每服务分配上限：`quota_cap = min(demand, max_replicas, concurrency_quota)`；若被压低，记录 `concurrency_quota` 裁剪原因。
2. 若全部 `quota_cap` 之和不超过全局预算，则直接提交。
3. 若超过预算，按服务名升序逐层分配副本：第一轮尽量给每个未达上限的服务 1 个副本，第二轮再各加 1 个，直到预算耗尽。该规则只依赖稳定服务名键和目标值，与估算完成顺序、JSON 服务数组顺序无关。
4. 对低于 `quota_cap` 的服务记录 `global_budget` 原因及裁剪前后副本数。

全局预算始终优先于最小副本数：如果预算小于所有服务最小副本数之和，最终分配仍不会突破预算，裁剪原因会明确显示这一点。

### 3. 请求分配与拒绝

```text
capacity_requests = final_replicas * concurrency_per_replica
accepted_requests = min(requests, capacity_requests)
rejected_requests = requests - accepted_requests
```

调度器会逐项复核请求中间项、最终副本、容量、接受量、拒绝量、裁剪原因链、分钟内总计和全局预算；任何不自洽立即返回 `E_ALLOC`，并带有 `minute`、`service` 和字段路径。

## 声明版本与陈旧结论

- 声明文件内容指纹基于原始 UTF-8 文本 SHA-256。
- 若当前最新声明内容未变，重复加载复用当前版本。
- 若文件被修改，新版本号为 `max(文件中声明的版本号, 当前最大版本 + 1)`，因此即使文件内版本号未改或改回旧内容，版本仍严格递增。
- 新版本注册时，所有旧运行、旧分钟记录和服务记录立即标记 `is_stale=1` 且取消 `is_current`；当前视图变为空，直到新版本完整推进。视图不混入旧分钟。
- 失效报告稳定列出语义差异路径、受影响服务和分钟范围，例如 beta 并发上限修改会得到 `affected_services=["beta"]`、`affected_minute_range=[0,7]`。

## SQLite 账本

默认数据库为 `artifacts/workbench/ledger.sqlite`，主要表：

- `declarations`：不可变声明版本、原始文本、指纹、作者版本。
- `runs`：运行状态、声明版本、总分钟数、连续完成分钟、当前分钟、当前/陈旧标记。
- `minute_decisions`：分钟级预算、请求、接受、拒绝和完整 JSON 结论。
- `service_decisions`：服务级需求中间项、最终副本、容量、拒绝量、裁剪原因。
- `snapshots`：整轮快照摘要、SHA-256 和本地文件路径。
- `version_events`：声明版本差异与失效范围。

每完成一分钟，分钟行、全部服务行和运行进度在同一个 SQLite 事务中提交。提交前先把 `in_flight_minute` 持久化为当前分钟，因此进程被强制终止时，SQLite 只保留完整分钟。

### 恢复

`recover` 只信任从 0 开始连续且服务集合完整的分钟前缀。恢复报告包含：

- `recovered_minutes` 与恢复范围；
- `pending_minutes` / `unknown_minutes`；
- `first_unknown_minute`；
- `requires_rerun=true`；
- `exit_report_available=false`，不会把中断恢复伪装成正常退出报告。

`resume` 会先按持久化声明版本重放已完成分钟，逐项校验成功后再从未知分钟继续；续跑完成后再次执行全量重放。

### 快照与重放

整轮快照要求所有分钟都完整。快照同时写入 SQLite 和 `snapshots/<snapshot_id>.json`，记录声明原文、声明版本、指纹、全部分钟结论和 SHA-256。恢复时：

1. 核对快照 JSON 与摘要；
2. 核对声明版本、指纹和原文；
3. 按声明重新计算全部分钟，与快照逐服务比较；
4. 全部一致后才创建新的当前运行并写入账本；
5. 恢复完成后再次执行账本重放。

逐项比较字段包括 `request_count`、`capacity_per_replica`、`unclamped_demand`、`demand_replicas`、`final_replicas`、`accepted_requests`、`rejected_requests`、`clip_reasons`，以及分钟预算总计。发现差异返回 `E_REPLAY`，定位到首个不一致的分钟、服务和字段。声明版本或负载序列不一致时，报告会明确给出 `declaration_version_mismatch`、`load_sequence_mismatch` 或声明指纹依据。

## 业务码

| 业务码 | 触发点 | 定位信息 |
|---|---|---|
| `E_DECL` | 声明读取、JSON 解析或强校验失败 | JSON 字段路径，整体拒绝 |
| `E_DEMAND` | 需求估算缺少分钟负载或必要输入 | 服务、分钟、`missing_inputs` |
| `E_ALLOC` | 容量、接受/拒绝、裁剪原因或预算不自洽 | 分钟、服务、字段路径 |
| `E_REPLAY` | 快照/账本重放首个差异或声明不可复现 | 分钟、服务、字段和依据 |

CLI 成功返回 0，业务错误返回 2，内部错误返回 3。

## Tkinter 工作台

启动：

```powershell
python scripts\gui.py
```

界面包含：

- 声明路径、加载、全量推进、单分钟推进、恢复、恢复并续跑、重放、快照保存/恢复按钮；
- 当前声明版本、运行编号、进度、全局预算和指纹；
- 按分钟升序、服务名升序渲染的决策表：请求、未夹取需求、需求副本、最终副本、容量、接受、拒绝、裁剪原因；
- 全局副本预算占用柱状曲线，并在每分钟下方标注拒绝总量；
- 当前版本快照列表，按快照 ID 稳定排序；
- 版本差异、恢复报告、重放结果和异常业务码的 JSON 报告区。

每次刷新都会完整清理并重建当前版本视图，不追加、不合并旧版本分钟。

## 演示场景

`python scripts/demo.py` 默认清理并重建 `artifacts/demo_state`，顺序执行：

1. **基准全量推进**：版本 1，8 分钟，全局预算 10，展示 beta 在高峰分钟的预算裁剪。
2. **修改并发上限后重推进**：beta 的 `concurrency_per_replica` 从 9 改为 6；旧运行变陈旧，自动生成版本 2 并全量重推。
3. **快照恢复重放**：保存版本 2 的整轮快照，恢复为新运行，逐项比较 8 分钟全部服务字段并再次账本重放。

稳定输出保存在 `artifacts/demo_output/demo_report.txt`。实际运行结果为 `result=OK`。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试覆盖声明异常路径、版本回退仍递增、旧结论失效、并行顺序无关、预算/配额双重裁剪、拒绝量边界、四类业务码、强制中断恢复、快照恢复和首个字段差异定位。详细实测结果见 `VALIDATION.md`。
