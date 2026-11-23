# LB State Drift Workbench

一个完全本地运行的负载均衡健康检查状态工作台，用于滚动升级期间同时读取 v1 单文件快照和 v2 分片 JSON + SQLite 元数据，合并为统一状态视图，按注入逻辑时钟回放回环 HTTP 探测，并给出可追溯的漂移归因。

## 已实现能力

- v1/v2 全量解析校验：实例唯一性、阈值、状态枚举、v2 分片哈希、世代单调、墓碑一致性、SQLite 元数据交叉校验。
- 跨版本合并：逻辑时钟、来源优先级、状态世代裁决；保留两代原始值、时间表示、换算标度、舍入方向与裁决依据。
- 无法唯一裁决时输出 `pending_manual`，同时保留两代原值，不静默择一。
- 逻辑时钟驱动的多轮检查：HTTP 成功、状态码不符、超时、连接拒绝/重置、畸形 JSON 分别映射业务错误码；`status.probe_errors` 保留目标、预算、HTTP 状态与详情。
- 窗口状态机：`healthy -> suspect -> unhealthy`，连续失败/成功计数持久化，轮次提交幂等。
- 四类漂移归因：`real_state_change`、`time_conversion_error`、`stale_tombstone`、`generation_conflict`。
- 快照 MVCC：版本标记写入、临时表暂存、事务切换当前指针；旧版本保留，进行中轮次绑定启动时版本。
- 中断恢复：探测观察先幂等落库，整轮状态在一个事务中推进；`resume` 重放缺失观察并继续未完成计划。
- 多进程一致性：SQLite WAL + `BEGIN IMMEDIATE` + 跨进程文件锁；唯一键防止同一实例/轮次/版本重复提交。
- Flask 稳定排序 JSON API 与无外部依赖的离线 Web 诊断页面。

## 环境与安装

目标环境为 Windows 10/11 + Python 3.11/3.12。本实现同时兼容 Python 3.10+，本次自动验证环境为 Python 3.10.12、Flask 3.0.3、requests 2.32.3。

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

代码对文件锁做了 Windows `msvcrt` 与 POSIX `fcntl` 双分支；不使用 Docker，不访问外部目标。

## 快速开始

### 1. 生成确定性样本

```powershell
python samples/generate_samples.py
```

生成内容：

- `samples/v1/snapshot.json`
- `samples/v2/manifest.json`、`samples/v2/shards/*.json`、`samples/v2/meta.sqlite`
- `samples/package-2026.11.1.json`：基础包
- `samples/package-2026.11.2.json`：完整替换包
- `samples/check_plan.json`
- `samples/drift_injections.json`

### 2. 载入快照

```powershell
python -m lbworkbench --workdir work load --package samples/package-2026.11.1.json
```

再次载入同一个包会返回 `no_op`；较低版本返回 `E_VERSION_ROLLBACK`。

### 3. 启动本地回环探测服务

终端 A：

```powershell
python -m lbworkbench --workdir work probe-server --ready-file probe-ready.json
```

服务监听 `127.0.0.1` 的随机高位端口，并输出：

```json
{
  "base_url": "http://127.0.0.1:<random-port>",
  "ok": true,
  "port": 12345
}
```

`/ok` 返回 200，`/fail` 返回 503，`/slow` 超过预算，`/malformed` 返回非法 JSON，`/reset` 主动断开连接。

### 4. 运行三轮检查

终端 B：

```powershell
$base = (Get-Content .\probe-ready.json -Raw | ConvertFrom-Json).base_url
python -m lbworkbench --workdir work check --plan samples/check_plan.json --probe-base-url $base
python -m lbworkbench --workdir work status
python -m lbworkbench --workdir work verify
python -m lbworkbench --workdir work merge
```

单独运行某轮或指定实例：

```powershell
python -m lbworkbench --workdir work check `
  --plan samples/check_plan.json `
  --probe-base-url $base `
  --round 2 `
  --instance api-01
```

中断后恢复：

```powershell
python -m lbworkbench --workdir work resume --probe-base-url $base
```

`resume` 会先完成已开始轮次，再继续该计划后续未创建轮次；已持久化的探测结果会复用，不会重复推进计数。

### 5. 启动 Web 工作台

```powershell
python -m lbworkbench --workdir work serve --host 127.0.0.1 --port 8765
```

浏览器打开 `http://127.0.0.1:8765/`。页面展示统一状态、两代来源、有效权重、首个漂移证据，并可触发 `verify`。

## 样本与预期结果

基础包包含 8 个规范实例（v2 独有 `edge-01`，人工判定 `manual-01`）。三轮检查后预期：

| 实例 | 选中来源 | 最终状态 | 失败/成功 | 权重 | 首个归因 | 轮次 |
|---|---|---:|---:|---:|---|---:|
| `api-01` | v2 | suspect | 0 / 1 | 50 | real_state_change | 2 |
| `api-02` | v2 | healthy | 0 / 3 | 100 | generation_conflict | 1 |
| `cache-01` | v1 | unhealthy | 3 / 0 | 0 | time_conversion_error | 1 |
| `db-01` | v2 | suspect | 0 / 1 | 50 | real_state_change | 1 |
| `edge-01` | v2 | suspect | 0 / 1 | 50 | real_state_change | 2 |
| `manual-01` | — | pending_manual | 0 / 0 | 0 | — | — |
| `tie-01` | v2 | suspect | 1 / 0 | 50 | real_state_change | 3 |
| `worker-01` | v2 | suspect | 0 / 1 | 0 | stale_tombstone | 1 |

探测错误码全集：

```json
[
  "E_PROBE_MALFORMED",
  "E_PROBE_REFUSED",
  "E_PROBE_STATUS_MISMATCH",
  "E_PROBE_TIMEOUT"
]
```

样本特意将 v1/v2 的 `source_priority` 都设为 `2`，以演示来源优先级也相同时出现冲突值的 `pending_manual`。生产输入可使用默认值：v1 为 `1`、v2 为 `2`。

## 合并裁决规则

同一规范实例在两代中都存在时，按以下稳定顺序排序：

1. `logical_clock_ms` 较大者。
2. `source_priority` 较大者；默认 v2 高于 v1。
3. `state_generation` 较大者。
4. 若三者完全相同且语义值冲突，则 `pending_manual`；不静默选择。

每个合并实例保留：

- 来源格式、包版本、快照 ID、原始实例 ID；
- v1 原始相对秒数与 v2 原始绝对毫秒；
- v1 换算公式、`scale_ms_per_second=1000`、`rounding=exact_integer`、舍入方向；
- 各候选的时钟、优先级、世代、状态与最终裁决依据。

## 漂移归因顺序

归因只针对“合并快照状态”与“回放探测健康结论”不一致的实例，并保存首个触发证据：

1. `stale_tombstone`：墓碑仍存在，但探测成功或另一来源仍为非墓碑。
2. `generation_conflict`：两代状态不一致，且状态世代不同。
3. `time_conversion_error`：选中 v1 且相对秒数换算出的绝对失效时刻已不晚于当前逻辑时钟。
4. `real_state_change`：快照内部一致，但回放探测给出不同当前状态。

证据包含实例、快照版本、轮次 ID/序号、逻辑时钟、涉及字段、两代原始值、换算中间值、探测预算、HTTP 状态与业务错误码。证据以规范 JSON 排序后持久化，相同输入重复运行逐位一致。

## 事务、恢复与并发模型

- `snapshot_records`、`merged_instances`、`instance_state` 都按 `snapshot_version` 保留行。
- 加载先完成全部文件/哈希/字段校验，再用临时表暂存并按版本整体写入；单事务更新 `current_snapshot_version` 指针。
- 检查轮次启动时绑定当时快照版本；快照替换不会改变进行中轮次。
- 每个探测结果以 `(round_id, instance_id)` 唯一键幂等写入。
- 所有实例探测齐全后，在单个事务中更新全部实例计数、健康状态、漂移证据、轮次状态和逻辑时钟水位。
- 读者通过当前版本指针过滤，配合 SQLite WAL 快照隔离，不会看到新旧版本混合状态。
- 写入由跨进程文件锁和 `BEGIN IMMEDIATE` 串行化；并发 HTTP 探测仍可按实例并行。
- 强制终止只可能留下已提交观察或完全不存在的状态；恢复不会重复推进连续计数。

网络套接字本身需要真实时间超时才能阻塞等待，因此 requests 客户端使用 `timeout_ms` 执行 socket 预算；轮次间隔、窗口边界、状态水位和顺序裁决均不读取系统墙钟。

## CLI

| 命令 | 说明 |
|---|---|
| `load --package PATH` | 校验并原子载入快照包 |
| `check --plan PATH [--probe-base-url URL] [--round N] [--instance ID]` | 运行检查 |
| `resume --probe-base-url URL` | 恢复未完成轮次并继续计划 |
| `merge` | 输出当前持久化统一合并视图 |
| `status` | 输出稳定排序统一状态 |
| `verify` | 输出持久化状态/摘要差异报告 |
| `snapshots` | 列出保留快照版本 |
| `rounds` | 列出轮次及缺失观察实例 |
| `serve --host HOST --port PORT` | 启动 Flask UI/API |
| `probe-server --ready-file PATH` | 启动确定性回环探测服务 |

全局参数 `--workdir` 必须放在子命令之前：

```powershell
python -m lbworkbench --workdir work status
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/status` | 统一状态，稳定排序 JSON |
| `GET` | `/api/merge` | 合并视图 |
| `GET` | `/api/verify` | 差异报告；存在差异时返回 409 |
| `GET` | `/api/snapshots` | 快照版本列表 |
| `GET` | `/api/rounds` | 轮次与缺失观察 |
| `POST` | `/api/load` | `{"package_path": "..."}` |
| `POST` | `/api/check` | `{"plan_path": "...", "probe_base_url": "..."}` |
| `POST` | `/api/resume` | `{"probe_base_url": "..."}` |
| `GET` | `/health` | 进程健康检查 |

示例：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/api/load `
  -ContentType application/json `
  -Body '{"package_path":"samples/package-2026.11.1.json"}'
```

## 主要业务错误码

| 错误码 | 含义 |
|---|---|
| `E_BAD_SNAPSHOT` | 快照字段、枚举、阈值、世代或墓碑不一致 |
| `E_HASH_MISMATCH` | 分片、元数据或实例集合摘要不匹配 |
| `E_VERSION_ROLLBACK` | 新快照版本低于当前版本 |
| `E_VERSION_CONFLICT` | 相同版本但内容不同 |
| `E_BAD_PLAN` | 计划字段、轮次序号、间隔或探测定义非法 |
| `E_ROUND_ORDER` | 轮次顺序或时钟顺序违规 |
| `E_CLOCK_REGRESSION` | 轮次逻辑时钟低于已持久化水位 |
| `E_MANUAL_REVIEW` | 检查计划引用了待人工判定实例 |
| `E_PROBE_REFUSED` | 连接被拒绝或重置 |
| `E_PROBE_TIMEOUT` | 探测超过单次预算 |
| `E_PROBE_STATUS_MISMATCH` | HTTP 状态码不符合预期 |
| `E_PROBE_MALFORMED` | 响应声明为 JSON 但解析失败 |
| `E_PROBE_TRANSPORT` | 其他本地传输错误 |
| `E_BAD_PROBE_TARGET` | 探测目标不是本地回环地址 |
| `E_STATE_CORRUPT` | 持久化状态内部引用不一致 |

非法快照在进入写事务前失败，不写入快照、实例状态或版本指针。

## 测试与验收

```powershell
python -m pytest -q
```

测试覆盖：

- 两代快照合法/非法输入、哈希不匹配、墓碑与世代一致性；
- 合并裁决、v1 时间换算、人工判定、确定性；
- 三轮真实回环探测、状态迁移、四类漂移、四类探测错误码；
- 同输入重复运行状态逐字节一致；
- 轮次重复提交幂等；
- 探测中途崩溃、finalize 前崩溃、快照加载崩溃后的恢复等价性；
- 并行实例探测、并发同包加载、版本 no-op/增量/替换/回退；
- 快照替换期间读者隔离；
- Flask API、Web 页面与 CLI 全链路。

本次实际运行结果：

```text
29 passed in 9.86s
```

进程级验收也已实际执行：独立 `probe-server` + CLI `load -> check(3 rounds) -> status -> verify`，以及真实 `serve` 后的 `/` 页面和 `/api/status`。结果为 8 个实例、3 个已提交轮次、时钟水位 `1764000003000`、`verify` 差异为 0、四类漂移与四类探测错误码齐全，Web 页面和 API 均返回 200。

## 目录结构

```text
lbworkbench/
  api.py           Flask 应用与稳定 JSON 响应
  cli.py           命令行入口
  db.py            SQLite 模式与事务
  drift.py         四类漂移归因与证据构造
  engine.py        检查轮次、状态机、恢复
  merge.py         跨版本合并裁决
  plans.py         检查计划校验
  probes.py        本地回环测试服务与 HTTP 探测
  snapshots.py     v1/v2 包解析与校验
  store.py         原子加载、状态视图、verify
  web/             离线诊断页面
samples/
  generate_samples.py
tests/
```
