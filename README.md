# DrillBench：本地应急预案演练与异常注入工作台

DrillBench 是一个仅依赖 Python 标准库的本地工作台。它把预案推进、真实回环 HTTP 调用、故障注入、逻辑时钟超时、版本切换、强制终止恢复和一致性校验放在同一套可复现状态机里。

核心约束：

- 不访问外部网络，不使用 Docker，不依赖第三方包。
- 超时、步骤推进、恢复和注入消费判定只读取注入的逻辑毫秒，不读取系统墙钟。
- 真实 HTTP 调用、真实等待和 socket 主动中止仍然保留，避免把超时做成事后标注。
- 所有 JSON 输出使用 `sort_keys=True`，固定样本重复运行可逐字节比较。

## 快速开始

当前开发容器为 Linux + Python 3.10.12。代码面向 Python 3.11+ 编写，同时保持 3.10 可执行；Windows 兼容点包括 `msvcrt` 非阻塞文件锁和纯标准库 SQLite/http.server。本次未能在真实 Windows 环境执行，验证记录中的平台已明确标注。

Windows 建议：

```bat
py -3.11 -m venv .venv
.venv\Scripts\python run_demo.py
.venv\Scripts\python -m unittest discover -v
```

Linux/macOS：

```bash
python3 run_demo.py
python3 -m unittest discover -v
```

演示脚本会在 `artifacts/demo/` 下执行两条独立场景，并生成：

- `artifacts/demo_output.json`：完整验收输出。
- `artifacts/dashboard.html`：可直接用浏览器打开的独立诊断面板。
- `artifacts/demo/run-a|run-b|clean/`：隔离的 SQLite 状态与页面。

演示会实际验证：

1. 首次加载主样本快照。
2. 在第 2 步注入触发后，以 `os._exit(137)` 强制终止进程。
3. 通过 `resume` 识别 RUNNING 半提交，重放未完成步骤，注入只消费一次。
4. 恢复后的 `status` 与 `verify` 和无中断运行逐字节一致。
5. 加载 v2 得到 `FULL_REPLACEMENT`。
6. 请求回退到 v1 得到 `E_VERSION_ROLLBACK`。
7. 两次独立场景输出完全一致。

## 命令行

```bash
python -m drillbench load \
  --db .drill/state.db \
  --sample-dir samples \
  --plan payment-failover

python -m drillbench run \
  --db .drill/state.db \
  --run-id demo \
  --plan payment-failover \
  --skip-step 4

python -m drillbench inject --db .drill/state.db --run-id demo

python -m drillbench resume \
  --db .drill/state.db \
  --run-id demo \
  --plan payment-failover

python -m drillbench status \
  --db .drill/state.db \
  --run-id demo

python -m drillbench status \
  --db .drill/state.db \
  --run-id demo \
  --format html \
  --output artifacts/dashboard.html

python -m drillbench verify \
  --db .drill/state.db \
  --run-id demo \
  --samples-dir samples
```

`load` 也支持用 `--plans/--services/--clock/--injections` 分别传入四个文件。`run/resume --once` 只推进一条就绪分支，用于多进程并行验证。

## 固定样本

主样本位于 `samples/`：

- `plans.json`：9 个步骤的 `payment-failover` 预案。
- `services.json`：每个步骤对应的回环 HTTP 路由。
- `clock.json`：逻辑时钟 epoch、tick 表和每步时间窗。
- `injections.json`：数据损坏、服务拒绝、网络中断、时钟回拨注入。
- `SHA256SUMS`：全部固定样本的内容摘要。
- `v2/`：新增第 10 步的整体替换版本。

主样本时间窗（逻辑毫秒）：

| 步骤 | 窗口 | 演示结论 |
|---:|---:|---|
| 1 | 0–500 | `OBSERVED` |
| 2 | 500–900 | `E_DATA_CORRUPTION` |
| 3 | 900–1300 | 因步骤 2 中止而阻塞，保持 `NOT_STARTED` |
| 4 | 900–1200 | 主动跳过，`SKIPPED_MANUALLY` |
| 5 | 1300–1900 | `OBSERVED` |
| 6 | 1900–2400 | `E_STEP_TIMEOUT`，逻辑截止点真实中止 |
| 7 | 2500–2900 | `E_CONN_REFUSED` |
| 8 | 3000–3400 | `E_CLOCK_ROLLBACK` |
| 9 | 3500–3800 | `OBSERVED` |

`inj-promote-reject` 位于步骤 3。由于步骤 3 被失败前置阻塞，该注入保持 `PENDING`，用于验证“未消费水位”的表达。

## 输入约束

### 预案

```json
{
  "schema_version": 1,
  "plans": [{
    "name": "payment-failover",
    "steps": [{
      "seq": 1,
      "name": "check-primary-health",
      "depends_on": [],
      "expected_event": "HTTP_2XX",
      "timeout_logical_ms": 500,
      "skippable": false
    }]
  }]
}
```

校验规则：

- 预案名唯一，步骤序号在预案内唯一。
- 前置步骤必须存在，不能自依赖或重复依赖。
- 使用 Kahn 拓扑排序检测环；有环返回 `E_CYCLE`。
- `timeout_logical_ms` 必须为正整数。
- `expected_event` 只能来自 `CONNECT_OK/HTTP_2XX/HTTP_3XX/HTTP_4XX/HTTP_5XX/BODY_VALID`。

### 服务路由

```json
{
  "step": 1,
  "path": "/health/primary",
  "status": 200,
  "delay_logical_ms": 150,
  "body": "{\"status\":\"ok\"}",
  "content_type": "application/json",
  "mode": "normal"
}
```

`mode` 支持 `normal/malformed/disconnect`。服务绑定 `127.0.0.1` 的临时端口，`http.server` 的 `Date` 头被固定，不读取墙钟。

### 逻辑时钟

```json
{
  "start_ms": 0,
  "tick": {"logical_ms": 50, "real_ms": 10},
  "step_windows": [{"step": 1, "start_ms": 0, "end_ms": 500}]
}
```

`end_ms - start_ms` 必须等于该步骤超时预算。`tick` 只决定真实节奏：推动 50 逻辑毫秒需要真实睡眠 10 毫秒；超时判断仍然比较逻辑值。

### 注入

```json
{
  "id": "inj-standby-corruption",
  "step": 2,
  "at_ms": 500,
  "type": "data_corruption",
  "description": "corrupt standby health payload"
}
```

支持 `network_outage/service_reject/data_corruption/clock_rollback`。注入时刻必须落在 `[window.start_ms, window.end_ms)`。`clock_rollback` 必须给出正整数 `rollback_ms`。

注入身分和消费记录分离：

- 快照保存注入定义。
- `injection_consumptions` 以 `(run_id, version, injection_id)` 为主键。
- 注入消费与步骤结局、观测记录、逻辑时钟水位在同一事务提交。
- 崩溃前若 HTTP 已收到注入响应但事务未提交，恢复时不会留下消费记录，重放后只消费一次。

## 状态机

| 阶段 | 含义 | 可推进后继 |
|---|---|---|
| `PENDING` | 待执行 | 否 |
| `RUNNING` | 已声明开始，结局尚未提交 | 否 |
| `OBSERVED` | 已观测且满足期望 | 是 |
| `SKIPPED` | 主动跳过 | 是 |
| `TIMED_OUT` | 已启动后达到逻辑截止点 | 否 |
| `ABORTED` | 传输、注入或业务观测失败 | 否 |

结局分类明确区分：

- 尚未启动：`PENDING/NOT_STARTED`。
- 已启动后超时：`TIMED_OUT/TIMED_OUT_AFTER_START`。
- 主动跳过：`SKIPPED/SKIPPED_MANUALLY`。
- 已观测：`OBSERVED/OBSERVED`。
- 异常中止：`ABORTED/ABORTED` 加业务错误码。

只有全部前置步骤达到 `OBSERVED` 或 `SKIPPED` 才可推进。单个步骤异常会作为已提交结局保存，主循环继续处理其他就绪分支，不会污染已有观测摘要。

## 超时实现

1. 步骤从时间窗起点开始，记录持久化的 `started_at_ms`。
2. 逻辑时钟按照 tick 表显式推进，不读取 `time.time()/monotonic()/datetime`。
3. HTTP 请求在线程中真实执行。
4. 逻辑值达到 `started_at_ms + timeout_logical_ms` 后，调用 `AbortController.abort()` 对真实 socket 执行 `shutdown + close`。
5. 记录 `E_STEP_TIMEOUT`、`active_abort=true`、`wall_clock_used_for_decision=false`。

因此超时既有真实等待，也有主动中止，但判定阈值来自注入逻辑时钟。

## 错误映射

| 场景 | 业务错误码 |
|---|---|
| TCP 连接拒绝/网络中断注入 | `E_CONN_REFUSED` |
| 服务端直接断开连接 | `E_CONNECTION_DROPPED` |
| 逻辑超时并主动中止 | `E_STEP_TIMEOUT` |
| HTTP 状态族不符合 `expected_event` | `E_BAD_STATUS` |
| JSON 畸形或响应体无效 | `E_MALFORMED_BODY` |
| 数据损坏注入或响应摘要不符 | `E_DATA_CORRUPTION` |
| 服务拒绝注入 | `E_SERVICE_REJECTED` |
| 逻辑时钟回拨 | `E_CLOCK_ROLLBACK` |
| 其他期望事件不匹配 | `E_EXPECTATION_MISMATCH` |
| 其他传输故障 | `E_TRANSPORT` |

每条观测均保留：步骤、尝试序号、逻辑起止时刻、注入时刻、注入 ID、HTTP 状态、响应体摘要、期望摘要和诊断详情。

## 版本与快照

`load` 先在内存中完成全部校验，非法输入不会创建或写入状态库。

变化分类：

- 内容摘要相同：`NOOP`，不产生新版本。
- 步骤、服务和时钟相同，仅注入条目增量追加：`INCREMENTAL_APPEND`。
- 其他变化：`FULL_REPLACEMENT`。

`load` 输出的 `comparison` 同时给出内容/步骤/注入/服务/时钟摘要是否变化，以及新增/移除的步骤和注入 ID。

快照写入流程：

1. `BEGIN IMMEDIATE`。
2. 将新快照写入 SQLite 临时表 `drill_load_staging`。
3. 插入不可变 `snapshots(version)` 行。
4. 原子切换 `pointer(id=1)` 到新版本。
5. 提交事务。

不逐条覆盖旧步骤。运行开始时绑定其 `version`，之后即使当前快照指针切到 v2，进行中的运行仍使用 v1 直至结束。显式请求旧版本，或新提交内容摘要与更早快照相同，均返回 `E_VERSION_ROLLBACK`。

## 崩溃恢复

步骤执行前先提交 RUNNING 声明和开始逻辑时刻，然后才发起 HTTP 调用。结局提交是单事务，包含：

- 步骤结局；
- 观测记录；
- 注入消费；
- 逻辑时钟水位；
- 注入消费水位；
- 观测摘要；
- 哈希提交链。

恢复流程：

1. 通过步骤非阻塞文件锁区分活跃执行和遗留半提交。
2. 活跃执行返回 `E_RUN_ACTIVE`，避免并发 `resume` 重复执行。
3. 遗留 RUNNING 且无观测记录：记录 `recovery_events`，恢复为 PENDING，释放 claim，重放该步骤。
4. RUNNING 但已有完整观测：从观测记录完成裁决。
5. 已提交的 OBSERVED/SKIPPED/TIMED_OUT/ABORTED 不再执行。

业务提交链只包含 `INIT/SKIP/STEP`，不包含半提交 claim 或恢复动作，因此恢复后的 `status/verify` 与无中断运行逐字段一致。

## 并发和幂等

- SQLite 使用 WAL、外键和 `FULL` 同步。
- 步骤声明、注入消费和结局提交使用 `BEGIN IMMEDIATE`。
- `step_claims(version, step_seq)` 保证同一步骤同一版本只推进一次。
- 每步另有跨平台非阻塞文件锁，锁忙返回 `E_LOCKED`，不同分支可并行执行。
- SQLite 内部短写冲突最多等待 `DRILLBENCH_DB_BUSY_MS`（默认 50ms），仍忙则明确返回 `E_LOCKED`；设置 `DRILLBENCH_DB_BUSY_MS=0` 可关闭该短等待。
- 相同结局重复提交返回 `idempotent=true`；不同结局返回 `E_CONFLICT`。
- 读取方只查询已提交行，不会读取事务中间态。

## 校验与可视化

`status` 输出稳定排序 JSON，包含：

- 每步阶段、结局码、结局分类、依赖、逻辑起止时刻；
- 观测诊断字段和注入时刻；
- 每条注入的消费状态与消费水位；
- 逻辑时钟当前位置和快照指针。

`verify` 校验：

- SQLite `integrity_check`；
- 当前指针目标存在；
- 最终步骤均有匹配观测；
- 观测摘要可重算且一致；
- 提交哈希链连续；
- 注入消费引用合法；
- `SHA256SUMS` 固定样本摘要（传入 `--samples-dir` 时）。

`status --format text` 提供终端诊断表；`status --format html --output ...` 生成不依赖外部资源的 HTML 时间线。

## 项目结构

```text
drillbench/
  validation.py   输入解析、业务错误码、依赖环检测
  clock.py        显式逻辑时钟
  http_service.py 标准库回环测试服务
  http_client.py  原始 socket HTTP 客户端和主动中止
  store.py        SQLite 快照、事务、提交链、恢复、verify
  engine.py       依赖推进、状态机、注入、超时、并发分支
  report.py       JSON/文本/HTML 诊断
  cli.py          load/run/inject/resume/status/verify
samples/          确定性主样本、v2 样本和摘要
tests/            标准库 unittest
run_demo.py       端到端演示与验收自检
```

