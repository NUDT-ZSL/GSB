# vmpool-arbiter

纯 Python 标准库实现的本地虚机池容量伸缩与成本预算裁决引擎。所有输入来自固定 JSON 文件，所有状态写入本地 SQLite 文件，不访问网络、容器或外部服务。

## 环境

- Python 3.11+
- NumPy（按任务环境要求安装；运行时定点运算不依赖 NumPy）
- SQLite3（Python 标准库自带）

已验证环境：

```text
Python 3.11.15
NumPy 2.4.6
SQLite (Python sqlite3)
```

Linux/WSL：

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python numpy
```

Windows PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install numpy
```

代码中的路径使用 `pathlib.Path`，文件锁在 Windows 上使用 `msvcrt.locking`，在 POSIX 上使用 `fcntl.flock`。数据库提交使用 SQLite `BEGIN IMMEDIATE` 与 WAL。当前实际验证运行在 WSL2 Ubuntu 上；Windows 分支按 `msvcrt` API 实现，未改变任何 POSIX 专有状态格式。

## 快速运行

以下命令可从项目根目录执行。`--db` 可放在子命令前或后。

```bash
python -m vmpool load --sample samples/canonical --db state.db
python -m vmpool apply --db state.db --seq 13
python -m vmpool status --db state.db
python -m vmpool diagnose --db state.db
python -m vmpool verify --db state.db
python -m vmpool resume --db state.db
```

未指定 `--seq` 时，`apply` 裁决下一条缺失请求；指定 `--seq N` 时，按请求序号补齐并裁决到 N。已提交请求重复提交时直接返回原裁决结果，不产生新轮次。

测试：

```bash
.venv/bin/python -m unittest discover -v
```

Windows：

```powershell
.\.venv\Scripts\python -m unittest discover -v
```

## 固定样本

`samples/canonical/` 包含：

- `services.json`：`db -> cache -> api -> edge` 依赖链，初态分别为 3/2/4/4 副本。
- `pool.json`：64 vCPU、131072 MiB，初始已占用 32 vCPU、49152 MiB。
- `policy.json`：预算 110.00，容量安全余量 10%，定点单位、容差与逻辑时钟参数。
- `requests.json`：13 条固定请求。
- `manifest.json`：四个输入文件 SHA-256 与场景综合摘要。

请求覆盖：

| 序号 | 场景 | 结果 |
|---|---|---|
| 1 | `db` 扩容 | 接纳 |
| 2 | `api` 扩容但 `cache` 成为上游瓶颈 | 接纳，有效容量受限 |
| 3 | `cache` 扩容 | 接纳，下游 `api/edge` 重算 |
| 4 | `edge` 扩容 | 接纳 |
| 5 | `api` 超预算 | 拒绝，`E_BUDGET`，预算差值精度敏感 |
| 6 | `db` 反向缩容 | 接纳，反转并失效下游闭包 |
| 7 | 撤销 `db` 第 6 条请求 | 接纳，撤销并失效下游闭包 |
| 8 | `db` 再次缩容 | 接纳，反转 |
| 9 | 撤销 `edge` 第 4 条请求 | 接纳，因有效容量未变只重算 `edge` |
| 10 | `edge` 扩容 | 接纳 |
| 11 | `edge` 容量安全余量不足 | 拒绝，`E_SAFETY_MARGIN` |
| 12 | `edge` 扩容 | 接纳，仍受依赖链限制 |
| 13 | `edge` 明显超池核数 | 拒绝，`E_POOL_QUOTA` |

重建样本摘要：

```bash
python tools/build_manifest.py samples/canonical
```

## 定点与数值语义

`policy.units` 决定全部换算：

- `cpu_fixed_per_core = 1000`：1 vCPU 换算为 1000 定点单位。
- `memory_fixed_per_mib = 1`：1 MiB 换算为 1 定点单位。
- `money_fixed_per_currency = 1000000`：1 货币单位换算为 1000000 定点单位。
- `percent_fixed_per_percent = 100`，`ratio_denominator = 10000`：10% 表示成 1000/10000。

换算使用 `Decimal`，默认 `ROUND_FLOOR`。每个换算记录包含字段路径、原始文本值、原始浮点数值/表示、标度、舍入方向、定点值、精确残差与定点误差上界。资源乘法和成本聚合均为整数运算。

预算下限包含服务清单初态成本；`used_fixed` 初始为所有 `current_replicas * unit_price_fixed` 之和。池初始占用可包含非本清单的工作负载，差额保存为 `baseline_other_*`，后续只对服务副本变化施加增量。每次请求的 `target_replicas` 是绝对副本数，不是增量。

容量安全检查按固定顺序执行：

1. `pool_cpu`
2. `pool_memory`
3. `budget`
4. `service_cpu_safety`
5. `service_memory_safety`

拒绝裁决返回首个失败约束、可用/需求/差值定点量、容差、错误上界与换算来源链。安全余量使用向上取整：

```text
required = ceil(service_usage * (denominator + safety_ratio) / denominator)
```

## 依赖与失效

`services[].depends_on` 指向更早的上游服务。拓扑排序在多个候选节点间按服务名排序，保证确定性。

样本未提供依赖副本倍率，因此语义固定为“下游 1 个副本需要每个直接上游至少 1 个有效副本”。

有效容量：

```text
effective(service) = min(accepted(service), effective(upstream) for every upstream)
```

仅当上游有效容量真正成为更小值时，派生记录保存 `upstream_limit` 与 `bottleneck_path`。如果下游的有效容量结论未变，该下游记录不重算，并在结果中列入 `reused_services`。

撤销或反转时，从被修改服务出发沿下游依赖边传播。只有有效容量实际变化的节点进入重算闭包；每个失效事件记录：

- 触发请求序号与源服务
- 目标服务与传播路径
- 失效字段
- 是否已重算、是否可安全复用

## 持久化与恢复

SQLite 表：

- `snapshots`：完整版本快照。
- `current_pointer`：唯一当前快照指针。
- `rounds`：轮次状态 `in_progress/committed/abandoned`。
- `round_work`：已计算但尚未提交的轮次工作区。
- `decisions`：每请求唯一裁决结果。
- `invalidations`、`conversions`、`scenario`：失效链、定点记录和场景摘要。

提交流程：

1. 在文件锁内以 `BEGIN IMMEDIATE` 创建 `in_progress` 轮次。
2. 计算纯函数裁决，并把候选状态写入 `round_work`。
3. 校验基础版本仍是当前版本。
4. 同一事务写入新快照、裁决、失效事件并整体替换 `current_pointer`。

进程在步骤 1 或 2 之后被强制终止时，指针仍指向旧快照。`status` 显示 `recovery_required`，`resume`：

- 轮次启动后终止：重算缺失工作。
- 工作区已写后终止：复用 `round_work`，不重复计算。
- 已提交轮次：永不重复执行或回退。

调试用终止点（隐藏参数）：

```bash
python -m vmpool apply --db crash.db --seq 13 --fault-point round-started
python -m vmpool apply --db crash.db --seq 13 --fault-point work-written
```

两者均以退出码 75 终止子进程，随后可用 `resume` 恢复。

## 并发与幂等

- 外部文件锁串行化写者，Windows/POSIX 分别使用 `msvcrt`/`fcntl`。
- SQLite `BEGIN IMMEDIATE`、版本检查和唯一请求裁决提供第二层保护。
- 提交事务同时替换快照与指针，读取方通过单个 JOIN 只能看到完整版本。
- 同一 `seq` 的重复并发提交返回同一已存 JSON。
- 不同目标 `seq` 的并发实例在锁内按固定样本顺序补齐，不会超卖池资源。

## 业务错误码

| 错误码 | 含义 |
|---|---|
| `E_SAMPLE_MISSING` | 样本目录或文件缺失 |
| `E_JSON_MALFORMED` | JSON 畸形 |
| `E_NAME_DUPLICATE` | 服务名重复 |
| `E_DEPENDENCY_MISSING` | 依赖边端点不存在或自依赖 |
| `E_DEPENDENCY_CYCLE` | 依赖图成环 |
| `E_INVALID_NUMBER` | 数值非法、非有限或超出定义域 |
| `E_INVALID_REQUEST` | 请求或字段结构非法 |
| `E_PENDING_CONFLICT` | 同服务同逻辑时刻存在两条未裁决请求 |
| `E_CLOCK_ROLLBACK` | 逻辑时钟回拨超过策略阈值 |
| `E_DIGEST_MISMATCH` | 文件或场景摘要不匹配 |
| `E_WITHDRAW_INVALID` | 撤销引用非法 |
| `E_WITHDRAW_NOT_ACCEPTED` | 撤销目标未形成已接纳历史 |
| `E_POOL_QUOTA` | 池核数或内存配额不足 |
| `E_BUDGET` | 成本预算不足 |
| `E_SAFETY_MARGIN` | 服务容量安全余量不足 |
| `E_DEPENDENCY_CAPACITY` | 已接纳副本受上游有效容量限制 |
| `E_SCENARIO_CONFLICT` | 数据库已属于另一场景 |
| `E_REQUEST_CONFLICT` | 同请求序号但内容不同 |
| `E_VERSION_CONFLICT` | 提交基础版本变化 |
| `E_DB_BUSY` | 文件锁或 SQLite 等待超时 |
| `E_DB_INTEGRITY` | 持久化完整性错误 |

解析类错误发生在打开/写数据库之前，因此不会创建状态库。命令错误输出 `{"ok":false,"error_code":...}` 并以非零码退出。

## 实际验证结果

```text
$ .venv/bin/python -m unittest discover -v
Ran 24 tests in 5.022s
OK
```

固定样本最终演示（裁决到第 13 条请求）：

```text
scenario_digest = sha256:c826e92a938dc15ff9e2253f20a2231174743ce1165458465df1f065fa4977f1
request_13      = rejected / E_POOL_QUOTA / pool_cpu
final_state     = sha256:b61c31b3a14191555cf76b8775b0ca2b570efcfe2673fc6ec9fc06785c304fc4
verify.ok       = true (21 checks)
```

覆盖项：

- 固定样本解析与全部主要错误码
- 定点舍入、误差上界与安全余量向上取整
- 接纳/预算拒绝/安全余量拒绝/池配额拒绝
- 依赖有效容量传播与瓶颈路径
- 反转/撤销下游失效闭包及未受影响记录逐字复用
- 两套全新数据库的 `load/apply/status/diagnose/verify/resume` 输出逐字一致
- 轮次启动后和候选工作写完后强制终止的恢复
- 8 进程同目标并发幂等、6 进程不同目标并发串行化
- 池占用、预算、快照链和派生摘要 `verify` 全绿
