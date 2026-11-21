# capbench 本地多租户容量评估与诊断工作台

`capbench` 是一个完全本地、确定性的压测批次容量评估工作台。它把每个租户的计划拆成
“租户 -> 并发梯度 -> 观测窗口”三层单元，在每租户并行梯度和全局逻辑时间预算约束下
驱动本地 HTTP 目标，持久化每个窗口摘要与梯度裁决，并输出可追溯的容量证据链。

核心特性：

- 纯逻辑时钟驱动请求节奏、持续时间与窗口归属，不使用系统墙钟决定压测语义。
- 固定场景、固定种子、固定目标响应序列下，请求序列、窗口摘要和报告逐字节可复现。
- SQLite 原子提交窗口摘要、梯度结论、请求序号水位与逻辑时钟水位。
- 半提交窗口在进程强制终止后由孤儿恢复机制重跑，只补测未提交窗口。
- SQLite `BEGIN IMMEDIATE` + 跨进程文件锁保证预算不超用、窗口只提交一次。
- 稳定排序 JSON 报告，包含每租户容量、拐点证据、窗口摘要、异常分类和序号区间。
- 本地可编程 HTTP 服务支持状态分布、慢响应、故障窗口、畸形响应、连接关闭和端口拒绝。

## 环境准备

要求 Python 3.11 或以上，以及可用的 `pip`。不依赖 Docker 或任何容器运行时。

当前交付已在 Python `3.11.18` 虚拟环境中运行验证：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
python -m pip install -e .
```

Windows PowerShell / CMD：

```bat
py -3.11 -m venv .venv
.\.venv\Scripts\activate
python -m pip install -U pip
python -m pip install -r requirements.txt
python -m pip install -e .
python --version
```

如果在 Windows 上使用其他 Python 启动器，把 `py -3.11` 替换为实际 3.11+ 解释器即可。

## 一键演示

```bash
python demo.py
```

演示脚本会：

1. 在 `127.0.0.1` 的可用端口启动本地 HTTP 测试服务，并生成端口一致的临时场景。
2. 用固定样本和固定种子正常执行两次。
3. 第三次执行在 10 个窗口被声明后通过 `os._exit(137)` 强制退出，保留半提交窗口。
4. 执行 `resume`，再执行三次 `verify`。
5. 比较三份稳定 JSON 报告的 SHA-256。

实际验证结果（`artifacts/demo/comparison.json`）：

```json
{
  "ok": true,
  "repeat_run_equal": true,
  "resume_run_equal": true,
  "baseline_sha256": "aeef227af758599a902bb739eaedcc10c18eda08abfd4b0fff78f90af10c003c",
  "repeat_sha256": "aeef227af758599a902bb739eaedcc10c18eda08abfd4b0fff78f90af10c003c",
  "resumed_sha256": "aeef227af758599a902bb739eaedcc10c18eda08abfd4b0fff78f90af10c003c"
}
```

样本得到：

- `alpha`: 容量 15，20 并发首窗口 P95 `51.715512ms` 超过硬上限 `50.4ms`，提前终止。
- `beta`: 容量 30，30 并发 P95 处于降级区间，结论为降级容量。
- `gamma`: 容量 8，12 并发的第 2 个窗口注入目标服务中断，提前终止。
- 全局逻辑时间预算 `27000ms` 全部用于已启动窗口，未发生超用。

演示产物保存在 `artifacts/demo/`：

- `baseline.report.json`、`repeat.report.json`、`resumed.report.json`
- 三个 SQLite 数据库及 `interrupted.status.json`
- `scenario.http.json`、`comparison.json`

## 手动启动 HTTP 测试服务

样本场景监听 `127.0.0.1:18080`：

```bash
python -m capbench.test_service scenarios/demo.json
```

服务启动后输出一行 `{"event":"listening",...}`。健康检查：

```bash
curl http://127.0.0.1:18080/healthz
```

如果端口被占用，可以只对一个服务使用 `--port`，同时必须让场景中的 `port` 一致；
`demo.py` 会自动完成这种替换。服务不会访问任何非回环地址。

## CLI

```bash
# 新建或继续同一确定性运行
python -m capbench run \
  --scenario scenarios/demo.json \
  --db artifacts/run.sqlite \
  --report artifacts/report.json \
  --workers 4

# 强制中断后恢复；场景默认从数据库中的已验证快照读取
python -m capbench resume \
  --db artifacts/run.sqlite \
  --report artifacts/report.json

# 单元进度与逻辑时钟
python -m capbench status --db artifacts/run.sqlite --format json
python -m capbench status --db artifacts/run.sqlite --format text

# 稳定排序 JSON 或文本诊断树
python -m capbench report --db artifacts/run.sqlite --format json
python -m capbench report --db artifacts/run.sqlite --format text

# 校验数据库内部不变量，并可选地检查报告是否与持久化状态一致
python -m capbench verify \
  --db artifacts/run.sqlite \
  --report artifacts/report.json
```

多进程运行同一个数据库时，可以让多个实例并行执行 `run` 或 `resume`；实例之间通过文件锁
和 SQLite 事务竞争窗口。重复提交同一窗口相同摘要会幂等成功，不同摘要返回
`E_IDEMPOTENCY_CONFLICT`。

`--transport simulated` 可用于离线测试；默认 `http` 会真实访问场景指定的本地服务。
`--crash-after-claims` 和 `--crash-after-commits` 是故障注入钩子，用于验收恢复语义。

## 场景 JSON

完整样本见 `scenarios/demo.json`。核心结构：

```json
{
  "version": 1,
  "seed": 20261122,
  "global_time_budget_ms": 27000,
  "logical_clock": {
    "start_ms": 0,
    "global_budget_ms": 27000,
    "steps": [
      {"from_ms": 0, "to_ms": 5000, "step_ms": 5},
      {"from_ms": 5000, "to_ms": 30000, "step_ms": 10}
    ]
  },
  "services": [
    {
      "name": "local-capacity-target",
      "host": "127.0.0.1",
      "port": 18080,
      "expected_status": 200,
      "tenants": [
        {
          "name": "alpha",
          "max_parallel_gradients": 1,
          "slo": {
            "min_success_rate": 0.97,
            "max_p95_latency_ms": 45,
            "degraded_success_margin": 0.01,
            "degraded_latency_ratio": 1.12
          },
          "profile": {
            "status_distribution": {"200": 0.99, "500": 0.01},
            "latency": {
              "base_ms": 10,
              "jitter_ms": 4,
              "per_concurrency_ms": 1.9
            },
            "fault_windows": [{"gradient_index": 3, "window_index": 1}],
            "slow_windows": [],
            "malformed_windows": [],
            "refused_windows": []
          }
        }
      ]
    }
  ],
  "plans": [
    {
      "batch_id": "batch-alpha",
      "tenant": "alpha",
      "concurrency_gradients": [5, 10, 15, 20],
      "duration_ms": 3000,
      "pace_ms": 100,
      "window_ms": 1000,
      "request_timeout_ms": 250,
      "allow_early_termination": true
    }
  ]
}
```

字段语义：

- `host` 必须是字面回环地址：`127.0.0.0/8`、`::1`；`localhost` 会规范化为
  `127.0.0.1`。端口范围为 `1..65535`。
- 服务名全局唯一，租户名全局唯一；批次号唯一；当前每次运行每租户支持一条并发梯度阶梯。
- `concurrency_gradients` 必须严格递增且全部为正整数。
- `duration_ms`、`pace_ms`、`window_ms`、`request_timeout_ms` 可以是标量，也可以是
  与梯度数量等长的数组。
- `pace_ms` 表示每个虚拟用户的请求间隔；梯度并发为 `c` 时，用户起始偏移在一个节奏周期
  内均匀展开。每个窗口的请求数和全局请求序号在运行初始化时确定。
- 每个窗口必须至少有一个请求，且 `pace_ms`、`window_ms` 不得大于 `duration_ms`。
- 逻辑时钟步进表必须从 `0` 开始、分段连续、正步长，并覆盖场景使用的逻辑时间。
- SLO 要求 `0 < min_success_rate <= 1`、`max_p95_latency_ms > 0`。

兼容性字段别名也已支持，例如 `targets/services`、`batches/plans`、
`concurrency_ladder`、`observation_window_ms`、`request_pace_ms` 等。

## 确定性窗口驱动

每个梯度被拆成固定数量的观测窗口。窗口边界为：

```text
window_start = gradient_logical_start + window_index * window_ms
window_end   = min(gradient_logical_start + duration_ms,
                   window_start + window_ms)
```

请求调度规则：

1. 每个虚拟用户在 `pace_ms` 周期内产生请求。
2. 用户偏移为 `floor(worker * pace_ms / concurrency)`。
3. 到期请求只由 `ClockSpec` 的步进表推进到实际逻辑 tick。
4. 请求序号在初始化时按租户名、梯度序号、窗口号稳定分配，并写入 SQLite。

因此请求序列、窗口归属和序号范围不依赖线程完成顺序、进程重启或请求调用顺序。
客户端不再用系统墙钟计算逻辑延迟；本地 HTTP 服务必须返回
`X-Capbench-Latency-Ms`，否则该响应被归为 `malformed_response`。

## 窗口摘要

每个窗口固定输出：

- `request_count`、`success_count`、`success_rate`
- `p95_latency_ms`
- 五类异常计数：
  - `status_mismatch`
  - `connection_refused`
  - `request_timeout`
  - `malformed_response`
  - `target_service_interrupted`
- 每类异常的租户、梯度号、窗口号、请求序号区间和最多 3 个稳定样例
- 最小/最大延迟、状态码计数、请求调度摘要 `schedule_sha256`

P95 规则固定为：延迟升序排序后，在秩 `0.95 * (n - 1)` 上进行线性插值。

响应分类优先级：

1. 显式 `connection_refused` 或连接被关闭/拒绝
2. 显式 `target_service_interrupted`
3. 畸形响应或身份字段不匹配
4. 逻辑延迟超过该梯度单请求预算
5. 状态码不等于 `expected_status`
6. 成功

窗口之间独立汇总；一个窗口异常不会写入其他窗口摘要。

## 梯度裁决与容量拐点

窗口裁决：

- `pass`: 成功率与 P95 同时满足 SLO。
- `degraded_pass`: 未落入硬失败区，但成功率或 P95 落在配置的降级容差内。
- `abort`: 出现目标服务中断、成功率低于硬下限或 P95 高于硬上限。

硬边界：

```text
hard_success_floor    = min_success_rate - degraded_success_margin
hard_latency_ceiling  = max_p95_latency_ms * degraded_latency_ratio
```

梯度裁决取该梯度所有已提交窗口：任一窗口 `abort` 则梯度 `abort`；全部窗口完成且存在
`degraded_pass` 则梯度 `degraded_pass`；否则全部 `pass` 才为 `pass`。

当 `allow_early_termination=true` 时：

- 首个 `abort` 窗口立即触发终止。
- 记录触发梯度号、窗口号和完整阈值证据。
- 该梯度剩余窗口标记 `skipped_early_termination`。
- 同租户后续未启动梯度标记 `skipped_after_knee`，不计为失败。

容量结论始终从最低并发梯度开始连续扫描：

- 连续 `pass` / `degraded_pass` 更新容量。
- 遇到 `abort`、预算放弃、失败或未完成立即停止。
- 即使后续诊断性梯度继续执行，已中止租户的容量结论也不会被更高梯度改写。

## 预算与调度

- 每租户 `max_parallel_gradients` 限制同时活动的梯度数。
- 全局预算按窗口逻辑跨度计费：`window_end_ms - window_start_ms`。
- 预算预留、窗口声明与状态变更处于同一个 SQLite 立即事务，并持有运行级文件锁。
- 无法容纳当前窗口时，该租户剩余未启动单元被标记为 `not_started_budget`，原因包含
  已用、需求、剩余和总预算。
- 预算内但被失败/未完成状态阻塞的单元标记为 `not_started_incomplete`。

结局分类明确区分：

- `budget_unstarted`
- `started_failed`
- `early_terminated`
- `not_started_within_budget`
- `not_started_after_knee`
- `started`

没有把已启动单元简单截断成超时；单个 HTTP 请求超时是窗口内的一类异常计数。

## SQLite 原子性与恢复

数据库核心表：

- `meta`: 场景哈希、种子、预算、逻辑时钟水位、请求序号水位、运行状态。
- `gradients`: 每梯度状态、裁决、终止证据和窗口计数。
- `windows`: 固定请求序号区间、状态、摘要和摘要 SHA-256。
- `budget_entries`: 每个窗口唯一的预算预留。
- `events`: 初始化、声明、提交、孤儿恢复和预算放弃等审计事件。

窗口提交事务原子写入：

1. 窗口摘要及摘要哈希
2. 梯度窗口计数、裁决和证据
3. 全局请求序号水位
4. 全局逻辑时钟水位
5. 审计事件

强制退出后可能留下 `running` 窗口。`resume` 会检查记录中的所有者 PID：

- 所有者为当前进程或已死亡进程：视为孤儿，重置为 `pending`，保留原预算预留。
- 所有者进程仍存活：不抢占，等待其完成或继续处理其他可运行单元。

已提交窗口永不重跑。恢复后的最终报告与未中断运行逐字节一致；`attempt_count` 只出现在
`status` 诊断中，不写入稳定报告。

## 报告与诊断

报告顶层：

```text
report_schema_version
run
policies
tenants[]
diagnostics
```

每租户包含：

- SLO 和目标端点
- `capacity_conclusion`: 容量、最高通过梯度、降级标志、终止位置、基线依赖
- 每梯度窗口摘要、错误计数、窗口裁决链
- `evidence_windows`: 支撑容量或触发中止的具体窗口和请求序号范围

`diagnostics.anomalies` 扁平汇总所有异常，稳定排序为：

```text
tenant -> gradient_index -> window_index -> fixed_error_category_order
```

文本模式示例：

```text
alpha: capacity_found capacity=15
  g1 c=5 completed/pass w0=pass(1.000,23.225123ms) ...
  ...
  evidence: abort at g4 w0 threshold={"reasons":["p95_above_hard_latency_ceiling"],...}
```

## 本地 HTTP 测试服务

`capbench.test_service` 使用 `ThreadingHTTPServer`，只监听场景中的回环地址：

```text
GET /healthz
GET /v1/tenants/{tenant}/gradients/{g}/windows/{w}/requests/{seq}
    ?concurrency=...&seed=...&run_id=...
```

同一请求序号始终得到同一模拟响应，行为由场景租户 profile 决定：

- `status_distribution`: 加权状态码分布。
- `latency.base_ms/jitter_ms/per_concurrency_ms`: 与并发相关的逻辑延迟。
- `slow_windows`: 增加慢响应逻辑延迟，可触发单请求预算异常。
- `fault_windows`: 返回故障标记和 HTTP 503，分类为目标服务中断。
- `malformed_windows`: 返回非法 JSON，分类为畸形响应。
- `refused_windows`: 服务直接关闭连接，分类为连接拒绝。

访问无监听端口时，客户端也会把真实 `ConnectError` 分类为 `connection_refused`。

## 校验与错误码

场景在创建数据库前完成全部解析和校验。非法输入不会创建数据库或写入任何状态。

主要业务错误码：

- `E_BAD_PLAN`: 梯度、节奏、持续时间、窗口、预算或批次计划非法。
- `E_BAD_SERVICE`: 服务、租户、监听地址、端口或响应 profile 非法。
- `E_BAD_SLO`: SLO 阈值或降级参数非法。
- `E_BAD_CLOCK`: 逻辑时钟表非法或覆盖不足。
- `E_BAD_JSON`: JSON 无法解析或存在重复键。
- `E_RUN_CONFLICT`: 同一 `run_id` 对应不同场景或种子。
- `E_IDEMPOTENCY_CONFLICT`: 同一窗口第二次提交了不同摘要。
- `E_LEASE_CONFLICT`: 窗口所有者已变化。
- `E_RUN_NOT_FOUND` / `E_RUN_AMBIGUOUS` / `E_DB_MISSING`: 运行或数据库定位失败。

`verify` 检查：

- 全局预算未超用
- 请求序号水位等于已提交窗口最大序号
- 逻辑时钟水位等于已提交窗口最大逻辑结束时间
- 所有摘要哈希可重算一致
- 非提交窗口不暴露部分摘要
- 梯度窗口计数与已提交窗口数一致
- 可选报告与当前持久化状态重新生成的报告完全相同

## 测试

```bash
pytest -q
```

当前结果：`31 passed`。

测试覆盖：

- 场景校验、业务错误码和非法输入零落盘
- 逻辑时钟请求序列、三层窗口归属和 P95 插值
- 五类异常计数、诊断字段与窗口隔离
- 容量拐点、降级通过、提前终止和后续梯度解耦
- 预算耗尽分类与全局预算不超用
- 半提交进程恢复与报告逐字节一致
- 幂等提交、冲突提交与报告篡改检测
- 真实 HTTP 状态分布、故障、畸形、慢响应、连接关闭和 TCP 拒绝
- 两个进程共享同一 SQLite 预算与幂等窗口
- `run/status/report/verify` CLI 和文本诊断输出

## 关键实现文件

- `capbench/scenario.py`: 场景解析、别名、业务校验
- `capbench/clock.py`: 注入式逻辑时钟与步进表
- `capbench/workload.py`: 请求调度、窗口摘要、P95
- `capbench/probe.py`: 真实 HTTP 与离线模拟探针
- `capbench/decision.py`: 窗口/梯度裁决
- `capbench/storage.py`: SQLite、文件锁、原子提交、孤儿恢复
- `capbench/evaluator.py`: 预算竞争、并行窗口调度、故障注入
- `capbench/report.py`: 稳定报告、状态文档、verification
- `capbench/test_service.py`: 本地可编程 HTTP 服务
- `capbench/cli.py`: `run/resume/status/report/verify`
- `scenarios/demo.json`: 固定三租户样本
- `demo.py`: 可重复 HTTP 演示
