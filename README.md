# 本地故障隔离与容灾切换诊断工作台

这是一个只依赖本机文件和 SQLite 的确定性诊断服务。它用注入的逻辑时钟驱动隔离域冻结、主备指针切换和回切阶梯，并用不可变快照保证查询侧不会看到冻结/主备混用状态。

核心能力：

- 严格解析服务拓扑、容灾策略和指标事件流。
- 同一轮次并行执行冻结探测与切换探测，并按逻辑时间预算和并发上限预先裁决。
- 查询绑定策略版本、主副本指针版本和不可变快照。
- 主备数据副本确定性派生，切换前后聚合查询逐位一致；不一致时给出首个分歧字段、双侧原始值和指针版本。
- SQLite 事务原子提交轮次、冻结状态、主副本、查询摘要和时钟水位。
- 域级检查点支持进程强杀后的细粒度恢复。
- 文件锁、唯一键和轮次幂等支持多进程并发。

## 目录

```text
diagnostic_workbench/   核心实现与 FastAPI 应用
scripts/generate_samples.py
                        确定性样本重建脚本
scripts/run_acceptance.py
                        端到端验收与报告生成脚本
samples/                拓扑、策略、事件流、合并样本和摘要清单
tests/                  校验、切换、查询隔离、预算、恢复、并发、API 测试
artifacts/diagnostic_verification.json
                        最近一次验收报告
```

## Windows 快速开始

要求 Python 3.11+、可写的仓库目录和本地 SQLite。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-test.txt
python scripts\generate_samples.py
uvicorn app:app --host 127.0.0.1 --port 8000
```

默认状态目录是 `./state`，也可显式指定：

```powershell
$env:WORKBENCH_STATE_DIR = "C:\workbench-state"
uvicorn app:app --host 127.0.0.1 --port 8000
```

OpenAPI 文档位于 `http://127.0.0.1:8000/docs`。

## 样本格式

`samples/sample.json` 是完整样本，另提供拆分后的 `topology.json`、`policy.json`、`events.json`。可由脚本稳定重建：

```powershell
python scripts\generate_samples.py
python scripts\generate_samples.py --check
```

拓扑要点：

- 每个服务有 `name`、`domain`、`upstream`、`data_seed`、`row_count` 和实例列表。
- 每个实例有全局唯一的 `instance_id`、`health_weight`、`replica_seed`。
- 可选 `data_variant` 只用于故障注入样本；正常情况下所有副本的逻辑行一致。

策略要点：

- `freeze_order` 必须恰好包含全部隔离域。
- `freeze_batch_size` 控制冻结批次。
- `probe_cost_ms` 给出冻结/切换探测的逻辑耗时。
- 每个服务有 `mode`（`active_passive` 或 `dual_active`）、触发指标、整数阈值、`cooldown_ms` 和严格递减且以 `0` 结尾的 `failback_ladder`。

事件按 `seq` 严格递增，`logical_time_ms` 在序列内不回退。只有当前主副本上的策略触发指标会参与阈值判定。

## API

所有成功和业务错误响应都使用稳定排序、无多余空白的 JSON。

### `POST /load`

加载文件、目录或内联样本。目录模式会读取 `sample.json` 和 `manifest.json` 摘要。

```json
{
  "sample_path": "samples"
}
```

也支持：

```json
{
  "sample": {
    "schema_version": 1,
    "sample_id": "x",
    "topology": {},
    "policy": {},
    "events": []
  },
  "expected_sample_digest": "sha256..."
}
```

非法样本会在任何业务状态写入前失败。

### `POST /switch`

```json
{
  "round_id": 1,
  "logical_time_ms": 130,
  "event_seq": 5,
  "max_concurrency": 6,
  "time_budget_ms": 120,
  "expected_policy_version": 1,
  "expected_pointer_version": 1
}
```

除 `round_id`、`logical_time_ms` 外均可省略。省略 `event_seq` 时，选择逻辑时钟不晚于请求时钟的全部未处理事件。

同一 `round_id` 重复提交相同输入会返回已提交结果；不同输入返回 `IDEMPOTENCY_CONFLICT`。存在半提交轮次时须先调用 `resume`。

### `POST /resume`

```json
{}
```

或只恢复指定轮次：

```json
{"round_id": 1}
```

已完成的域直接复用检查点，只重新执行缺失域。已提交切换不会重复写入账本。

### `GET /status`

返回当前已提交快照、冻结域、服务主副本、回切步骤、轮次水位、逻辑时钟和半提交轮次进度。响应稳定排序。

### `POST /verify`

```json
{"round_id": 1}
```

重新执行轮次前后同一批聚合查询，输出每个服务的前后摘要、首个分歧字段、主备原始值、指针版本、全部副本比对和已提交摘要校验。

### 辅助端点

- `GET /query/{service}?pointer_version=1`：按指定不可变快照执行查询，用于证明在途查询隔离。
- `GET /ledger?round_id=1`：查看切换账本及唯一键去重结果。
- `GET /health`：存活检查。

## 关键语义

### 逻辑时钟与确定性

业务判定不使用系统墙钟：

- 事件选择只使用事件 `logical_time_ms` 和请求时钟。
- 冷却窗口只比较注入时钟毫秒差。
- 回切阶梯只在冷却满足时按轮次推进。
- 时钟回退超过策略容差返回 `CLOCK_REGRESSION`。

同一策略版本、事件流和轮次输入会得到相同的切换序列、快照摘要和查询摘要。

### 冻结与切换

1. 根据事件识别当前主副本上的触发服务。
2. 按 `freeze_order` 和目标域生成冻结批次。
3. 每批为每个域同时规划冻结探测和切换探测。
4. 预算在探测执行前按逻辑耗时和并发槽位确定：
   - `completed`：启动并成功。
   - `failed_after_start`：启动后失败。
   - `abandoned_budget`：预算不足，未启动。
   - `not_started_within_budget`：预算内但同域必需探测已主动放弃，因此不启动。
5. 已准入探测全部提交到线程池并等待结束。
6. 一个域失败不会阻止其他域；该域本次不应用任何冻结或切换。

`dual_active` 服务仍保留一个用于线性一致查询和写入指针的唯一主副本；模式字段用于表达双活策略语义，主指针切换逻辑保持同一不变量。

### 查询一致性

每个副本从服务 `data_seed` 派生相同逻辑行，`replica_seed` 只改变物理行顺序。固定聚合查询包括计数、金额和、数量、分片聚合、类目聚合与逻辑行指纹。

切换规划会比较旧主副本和新主副本的查询值，并用稳定 JSON 摘要逐位比对。任何分歧都会使该域失败，并在错误上下文输出：

- `field_path`
- `primary_value` / `standby_value`
- `primary_instance` / `standby_instance`
- `primary_pointer_version` / `standby_pointer_version`

### 快照与版本绑定

- `policy_version` 标识整体策略文档；策略变更必须递增版本，回退被拒绝。
- 每次提交生成新的不可变 `pointer_version`，`current_snapshot` 在单条事务中整体切换。
- 查询从单个快照读取策略、冻结域、主副本和查询摘要，不会拼接新旧状态。
- 旧快照保留，因此在途查询可继续读取开始时的版本。

### 持久化和恢复

SQLite 使用 WAL、`FULL` 同步和 `BEGIN IMMEDIATE` 事务。主要表：

- `policy_versions`
- `snapshots`
- `current_snapshot`
- `rounds`
- `round_domains`
- `switch_ledger`
- `query_summaries`

轮次先以 `prepared` 状态持久化探测计划和预算准入结果，再按域写入终态检查点，最后在一个事务中写入新快照、查询摘要、切换账本和当前指针。

故障注入环境变量 `WORKBENCH_CRASH_AFTER_DOMAIN_COMMITS=N` 会在第 N 个域检查点提交后执行 `os._exit`，用于验证强杀恢复。恢复时只补缺失域，切换账本只在最终事务写入，因此不会重复计数。

## 业务错误码

| 错误码 | 含义 |
| --- | --- |
| `FREEZE_ORDER_CONFLICT` | 冻结顺序遗漏、重复或引用未声明域 |
| `SWITCH_TARGET_MISSING` | 触发服务无其他可切换实例 |
| `INVALID_FAILBACK_LADDER` | 回切比例非严格递减、越界或不以 0 结尾 |
| `POLICY_VERSION_ROLLBACK` | 策略版本低于当前版本 |
| `POLICY_VERSION_CONFLICT` | 同一版本试图替换为不同内容 |
| `SAMPLE_DIGEST_MISMATCH` | 样本与期望或清单摘要不一致 |
| `CLOCK_REGRESSION` | 逻辑时钟回退超过容差 |
| `QUERY_CONSISTENCY_VIOLATION` | 主备聚合查询不一致 |
| `IDEMPOTENCY_CONFLICT` | 同一轮次使用不同输入 |
| `PREPARED_ROUND_EXISTS` | 存在半提交轮次，需先恢复 |
| `STATE_CONFLICT` | 轮次基快照已过期或状态不允许操作 |
| `INVALID_TOPOLOGY_CYCLE` | 上游依赖存在环 |
| `INVALID_EVENT_SEQUENCE` | 事件序号非法 |
| `EVENT_SEQUENCE_REGRESSION` | 事件序号低于已提交水位 |

错误响应结构：

```json
{
  "error": {
    "code": "SWITCH_TARGET_MISSING",
    "context": {
      "domain": "zone-east",
      "round_id": 1,
      "service": "orders"
    },
    "field_path": "topology.services.orders.instances",
    "message": "No eligible standby instance exists for the triggered service."
  }
}
```

## 验证

```powershell
python -m pytest
python scripts\run_acceptance.py
```

最近一次实际结果：

- `pytest`：18 passed。
- 样本摘要：`b80b0587f2bd29b4678095b15dcc65ee394d7d78305830f5cf1294bcbf1cc6ed`。
- 第 1 轮冻结 `zone-core`、`zone-east`、`zone-west`，切换 `auth`、`orders`、`payments`、`catalog` 共 4 个服务。
- 第 1 轮 5 个服务切换前后查询摘要全部成对相同，`verify.verified=true`。
- 旧快照查询仍为 `auth-core-a` 且未冻结，新快照查询已为 `auth-core-b` 且冻结。
- 重复提交第 1 轮响应逐位相同，切换账本仍为 4 条。
- 强杀进程返回 77，强杀时仅有 1 个域检查点；`resume` 后轮次提交，账本仍为 4 条。
- 4 个并发进程返回码均为 0，4 份响应摘要一致，账本仍为 4 条。

详细机器可读结果见 `artifacts/diagnostic_verification.json`。

## 已知边界

- 同一时刻只允许一个 `prepared` 轮次；不同域可由多个进程并行完成，不同逻辑轮次需等前一轮恢复或提交。
- 仅诊断本地合成数据，不连接外部目标，不使用 Docker。
- 预算模型使用确定性逻辑探测耗时；线程池负责真实并发执行，不让系统墙钟参与预算裁决。
