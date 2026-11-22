# Mesh Drift Ledger

一个不依赖真实集群、容器或调度器的本地服务网格配置漂移裁决引擎。它把“声明期望配置”与“实例实际上报配置”的差异持久化为可解释、可裁决、可恢复的 SQLite 账本。

核心目标不是执行下发，而是回答四个问题：

- 哪个实例、哪个字段、发生了什么漂移？
- 依据哪些观测和持久化历史，裁决为自动收敛、人工介入还是暂时容忍？
- 重复漂移是否应冷却去重，何时应新建关联条目？
- 进程被强制终止后，哪些轮次已提交、哪些需要续算，如何保证不重复累加？

## 目录

```text
mesh_drift/
  validation.py     期望/上报/策略校验与规范化
  engine.py         结构化差异分类、容忍窗口和聚合裁决
  runner.py         并发拉取、逻辑预算、轮次日志、恢复
  store.py          SQLite 事务账本、条目版本、证据链、视图
  verify.py         逐轮独立复算与账本完整性校验
  mock_server.py    本地回环 HTTP 上报模拟服务
  cli.py            命令行入口
samples/
  expected.json     固定回环端口、确定性期望快照
  reports.json      离线分类样本
  policy.json       裁决策略
  anomalies.json    按逻辑轮次注入异常
scripts/
  make_samples.py   重新生成确定性样本
  demo.py           端到端演示
tests/              21 个 pytest 用例
```

## 环境

当前环境已实际验证：

```text
Python 3.11.18
pytest 9.2.0
requests 2.34.2
SQLite 3.53.1
```

从空环境复现：

```bash
uv python install 3.11
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -c 'import pytest, requests, sqlite3; print("imports ok")'
```

不使用 Docker、Kubernetes 或其他容器环境。所有网络样本仅访问 `127.0.0.1`，HTTP 客户端显式绕过环境代理。

## 快速开始

### 1. 只校验，不写状态

```bash
.venv/bin/python -m mesh_drift validate \
  --expected samples/expected.json \
  --policy samples/policy.json
```

### 2. 离线分类与裁决

```bash
.venv/bin/python -m mesh_drift analyze \
  --expected samples/expected.json \
  --policy samples/policy.json \
  --reports samples/reports.json \
  --service checkout \
  --clock 1000
```

`samples/reports.json` 覆盖版本落后、摘要篡改、实例缺少规则、实例多出规则、路由权重漂移和上报元数据缺失。

### 3. 启动回环上报服务并执行真实 HTTP 对账

终端 A：

```bash
.venv/bin/python -m mesh_drift serve-mock \
  --expected samples/expected.json \
  --scenario samples/anomalies.json
```

默认监听 `127.0.0.1:18101` 至 `127.0.0.1:18104`。

终端 B：

```bash
.venv/bin/python -m mesh_drift reconcile \
  --db /tmp/mesh-drift.db \
  --expected samples/expected.json \
  --policy samples/policy.json \
  --service checkout \
  --clock 1000

.venv/bin/python -m mesh_drift reconcile \
  --db /tmp/mesh-drift.db \
  --expected samples/expected.json \
  --policy samples/policy.json \
  --service orders \
  --clock 1000

.venv/bin/python -m mesh_drift verify --db /tmp/mesh-drift.db
```

### 4. 强杀后恢复

```bash
# 模拟在账本写入后、事务提交前收到 SIGKILL
MESH_DRIFT_CRASH_AFTER_WRITES=1 \
.venv/bin/python -m mesh_drift reconcile \
  --db /tmp/mesh-drift.db \
  --expected samples/expected.json \
  --policy samples/policy.json \
  --service checkout \
  --clock 2000

.venv/bin/python -m mesh_drift recover --db /tmp/mesh-drift.db
.venv/bin/python -m mesh_drift verify --db /tmp/mesh-drift.db
```

还存在 `MESH_DRIFT_CRASH_POINT=after_fetch`，用于模拟“拉取完成、轮次提交前”被强杀。钩子只由环境变量显式启用，默认完全不生效。

### 5. 完整演示

```bash
.venv/bin/python scripts/demo.py
```

演示按逻辑时钟 `1000 -> 2000 -> 6000 -> 9000` 对两个服务执行 8 个轮次，展示去重累加、冷却后新建关联条目、收敛关闭及逐轮复算。

## 数据契约

### 期望快照

```json
{
  "schema_version": 1,
  "snapshot_id": "expected-20261123-01",
  "logical_clock_ms": 10000,
  "services": [
    {
      "service": "checkout",
      "routes": [
        {
          "rule_id": "checkout-pay-stable",
          "path": "/pay",
          "version_label": "v1",
          "weight": 90
        }
      ],
      "instances": [
        {
          "instance_id": "checkout-a",
          "endpoint": "http://127.0.0.1:18101/report",
          "version": 14,
          "digest": "sha256:...",
          "version_label": "v1",
          "pull_cost_logic_ms": 100
        }
      ]
    }
  ]
}
```

校验规则：

- 服务名、实例 ID、规则 ID 唯一；实例只能归属已声明服务。
- 版本号、逻辑时钟、规则权重、单次拉取逻辑成本均为非负整数。
- 同一路由路径下权重和必须闭合为 `100`。
- 实例 `version_label` 必须闭合到服务已声明路由的版本标签。
- `endpoint` 在样本中使用回环地址。

### 实例上报

```json
{
  "service": "checkout",
  "instance_id": "checkout-a",
  "version": 14,
  "digest": "sha256:...",
  "logical_clock_ms": 9900,
  "healthy": true,
  "rules": [
    {
      "rule_id": "checkout-pay-stable",
      "path": "/pay",
      "version_label": "v1",
      "weight": 90
    }
  ]
}
```

上报字段缺失不会变成硬错误，而是产生 `REPORT_METADATA_MISSING` 漂移；字段类型错误、重复规则或身份不匹配属于上报错误，不转为普通漂移。

### 裁决策略

```json
{
  "schema_version": 1,
  "policy_id": "reconcile-policy-20261123",
  "tolerance_window_ms": 5000,
  "strict_digest": true,
  "max_concurrency": 2,
  "round_budget_ms": 1000,
  "dedup_cooldown_ms": 3000,
  "instance_timeout_ms": 600,
  "max_report_bytes": 65536
}
```

- `tolerance_window_ms`、`max_concurrency`、`round_budget_ms`、`instance_timeout_ms` 必须为正整数。
- `dedup_cooldown_ms` 必须为非负整数。
- 轮次逻辑时钟不得小于该服务已提交的逻辑时钟水位。

## 漂移分类

| 类型 | 判定 |
|---|---|
| `VERSION_BEHIND` | 实际上报版本小于期望版本 |
| `VERSION_AHEAD` | 实际上报版本大于期望版本 |
| `DIGEST_MISMATCH` | 规则内容摘要与期望不一致 |
| `EXTRA_RULE` | 实例生效了未声明规则 |
| `MISSING_RULE` | 实例缺少声明规则 |
| `ROUTE_WEIGHT_MISMATCH` | 关键路由权重不一致 |
| `ROUTE_CONTENT_MISMATCH` | 规则路径或版本标签不一致 |
| `REPORT_METADATA_MISSING` | 服务、实例、版本、摘要、逻辑时钟、健康标记或规则元数据缺失 |

每条差异包含 `drift_type`、`field_path`、`expected`、`actual`、`basis` 和 `evidence`。排序键固定为 `(drift_type, field_path, canonical(expected), canonical(actual))`；所有 JSON 使用稳定键序和紧凑/缩进两套确定性编码。

## 裁决语义

优先级：

```text
MANUAL_INTERVENTION > AUTO_CONVERGE > TOLERATE > CONVERGED
```

判定顺序：

1. 实例未自声明健康，或健康字段缺失：人工介入。
2. 结构漂移（元数据缺失、缺少/多出规则、路由内容变化）：人工介入。
3. 严格摘要开启时摘要不一致：人工介入。
4. 实例版本高于声明：人工介入。
5. 其余漂移按持久化首次观测逻辑时钟计算年龄：
   - 年龄不超过容忍窗口：暂时容忍。
   - 超过容忍窗口：自动收敛。

同一实例同时命中多种漂移时不会只保留第一条。每轮结果包含全部差异及聚合裁决，聚合结果记录主导裁决的漂移类型和原因码。

## 账本与状态机

账本条目保存：

- 来源：实例、回环 endpoint、期望快照 ID、策略 ID。
- 证据：字段路径、期望值、实际值、判定依据、上报摘要、裁决观测。
- 演进：出现次数、首次/末次逻辑时钟、冷却截止时钟、前序条目、条目版本。
- 状态：`OPEN`、`TOLERATED`、`CLOSED`、`REOPENED`。
- 每次变化同时写入 `ledger_revisions`、`ledger_events` 和 `ledger_evidence`。

去重与冷却：

- 同一实例、同一漂移类型、同一字段路径在冷却窗口内重复出现，不新增条目，累加 `occurrences` 并更新末次时钟。
- 冷却窗口结束后再次出现，新建条目并通过 `previous_entry_id` 关联前序条目。
- 成功上报不再包含某漂移时关闭对应条目。
- 已关闭条目在冷却窗口内再次出现，恢复为 `REOPENED`，不新建条目。

所有状态判断只读取 SQLite 中已提交的条目、次数和逻辑时钟，不使用进程内跨轮缓存。

## 并发、预算与错误隔离

- 每轮对账开始前按实例 ID 稳定排序并预留 `pull_cost_logic_ms`。预留总量超过 `round_budget_ms` 的实例不启动，记录 `E_ROUND_BUDGET_EXHAUSTED`。
- 逻辑预算只阻止“尚未启动”的拉取，不截断已启动 HTTP 请求。
- 实际并发数受 `max_concurrency` 限制，单请求超时受 `instance_timeout_ms` 限制。
- 放弃实例不写实例视图、不增加异常计数、不产生漂移。
- 已启动失败实例写入独立错误视图和异常计数，不影响同轮其他实例提交。

错误码：

| 错误码 | 含义 |
|---|---|
| `E_BAD_EXPECTED` | 期望快照非法 |
| `E_BAD_REPORT` | 上报字段或类型非法 |
| `E_BAD_POLICY` | 策略非法 |
| `E_BAD_FIXTURE` | 离线上报夹具非法 |
| `E_REPORT_CONNECTION` | 连接拒绝或无法建立连接 |
| `E_REPORT_TIMEOUT` | 超过单实例请求预算 |
| `E_REPORT_MALFORMED` | 非 JSON、非对象或非 UTF-8 响应 |
| `E_REPORT_HTTP` | 非 200 响应 |
| `E_REPORT_TOO_LARGE` | 响应超过 `max_report_bytes` |
| `E_REPORT_IDENTITY` | 上报服务/实例身份不匹配 |
| `E_VERSION_REGRESSION` | 上报版本低于该实例上一次成功上报版本 |
| `E_ROUND_BUDGET_EXHAUSTED` | 逻辑预算不足，实例未启动 |
| `E_CLOCK_REGRESSION` | 轮次逻辑时钟早于服务水位 |
| `E_ROUND_CONFLICT` | 同一轮次 ID 绑定了不同输入 |

非法期望或策略在打开数据库前失败，因此不会创建数据库、锁文件或轮次状态。

## 原子提交与恢复

每轮先写 `round_runs` 预提交日志，状态为 `PREPARED -> FETCHING -> READY -> COMMITTED`。拉取结果逐实例落盘，因此强杀后可以复用已完成的上报。

最终提交在单个 SQLite `BEGIN IMMEDIATE` 事务内完成：

- 账本条目、条目版本、事件和证据链。
- 实例最新上报视图与异常计数。
- 服务逻辑时钟水位、账本水位和实例视图摘要。
- 轮次摘要、参与/已启动/放弃实例集合、统计和复算输入。
- `round_runs.state = COMMITTED`。

读取方通过 SQLite 事务只能看到完整已提交轮次。若进程在事务中收到 `SIGKILL`，未提交写入由 SQLite 回滚；恢复时识别未完成 `round_runs`，复用已落盘观测，只重做未完成部分。相同 `round_id`、`entry_id`、`entry_version` 的重复提交由主键、唯一约束、文件锁和事务共同保证幂等。

不同服务使用不同文件锁，可并行拉取；SQLite 写事务串行化短提交阶段。同一服务同一轮次由服务锁串行化，后到进程直接返回已提交轮次。

## 校验与复算

`verify` 对每个已提交轮次执行：

- 从持久化输入重新执行差异分类与聚合裁决，和已提交分析逐字比较。
- 复算输入快照摘要、期望快照摘要、策略摘要和轮次摘要。
- 校验条目版本连续、最终版本与当前条目一致。
- 校验每次修订摘要、事件序列、证据链和出现次数。
- 校验参与实例、已启动实例、放弃实例集合。
- 校验服务水位、轮次计数和未完成轮次。

## 实际验证结果

执行：

```bash
.venv/bin/pytest -q
```

结果：

```text
.....................                                                    [100%]
21 passed
```

覆盖内容：

- 期望/上报/策略非法输入及“不创建数据库”。
- 全部漂移类型、稳定排序、逐字确定性输出。
- 健康标记、严格摘要、容忍窗口、聚合优先级。
- 回环 HTTP 分类、超时、连接拒绝、畸形响应、版本回退。
- 逻辑预算放弃、已启动错误隔离、异常计数。
- 冷却去重、冷却后新条目关联、关闭与重新打开。
- 事务内强杀、拉取后强杀、恢复与不中断运行账本摘要逐字段一致。
- 多进程不同服务并发、同服务同轮次幂等、逻辑时钟回退拒绝。
- 篡改修订摘要后 `verify` 失败。

独立 CLI 冒烟结果：

```text
checkout round-582652973ed4a18a4b3e9cb0
  VERSION_BEHIND: 1, TOLERATE: 1
orders round-c4946feb9e943c7112c096c8
  DIGEST_MISMATCH: 1, REPORT_METADATA_MISSING: 2, MANUAL_INTERVENTION: 2
verify ok, rounds=2, entries=4
```

`scripts/demo.py` 结果：

```text
verify ok, rounds=8, entries=12
database_digest=sha256:7d964cba58fe0579523d697f72329d0751787f5e548df7fbebd36789dd2303bd
```

## 设计边界

- 回环 HTTP 模拟器用于确定性测试，不实现真实服务发现或配置下发。
- 轮次预算使用实例声明的逻辑拉取成本做确定性预留，而不是使用非确定性的墙钟耗时决定“启动/放弃”。
- 单实例请求超时仍是实际网络超时，用于覆盖真实 HTTP 失败链路。
- 账本只记录治理事实和裁决，不执行自动下发动作。
