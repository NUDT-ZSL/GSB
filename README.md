# 本地多租户压测批次容量评估引擎

这是一个从零实现的确定性本地容量评估工具，覆盖：

- JSON 场景、批次计划、逻辑时钟和精度配置校验；
- 本地回环 HTTP 测试服务，可按租户/梯度返回状态码分布、慢响应、畸形响应、连接重置；
- 仅由逻辑时钟驱动的请求释放、观测窗口切分和梯度推进；
- 成功率与 P95 的定点换算、half-up 舍入、误差上界和可回查账本；
- `PASS / DEGRADED_PASS / ABORT` 状态机、提前终止和容量拐点；
- 精度敏感标记、参与窗口/登记序号/误差来源链和高标度翻转分析；
- SQLite WAL 原子提交、文件锁、多进程认领、预算/租户并发约束、崩溃恢复；
- `run / resume / status / report / verify` 五个 CLI 子命令。

详细设计见 `docs/DESIGN.md`。

## 环境

已在以下两个环境运行通过：

- Python 3.11.18、numpy 2.4.6、SQLite 3.53.1；
- Python 3.10.12、numpy 1.26.4、SQLite 3.37.2（兼容性回归）。

本机建议用 `uv` 创建 3.11 环境：

```bash
uv python install 3.11
uv venv --python 3.11 .venv311
uv pip install --python .venv311/bin/python -r requirements.txt
```

Windows 下可直接使用同样的 `python -m capacity_engine ...` 命令；`scripts/*.sh` 面向 Git Bash/WSL/POSIX shell。SQLite 文件、`.lock` 文件和结果目录只需位于可写目录中，不依赖 Docker 或外部服务。

## 快速运行

一条命令运行固定样本：

```bash
scripts/run_sample.sh
```

或手动执行：

```bash
.venv311/bin/python -m capacity_engine.mock_service \
  --config samples/mock_service.json --port 18080
```

另开终端：

```bash
.venv311/bin/python -m capacity_engine run \
  --scene samples/scene.json \
  --db work/demo/state.sqlite \
  --results work/demo/results

.venv311/bin/python -m capacity_engine verify --db work/demo/state.sqlite
.venv311/bin/python -m capacity_engine status --db work/demo/state.sqlite --format text
.venv311/bin/python -m capacity_engine report --db work/demo/state.sqlite --format text
```

`run` 返回稳定 JSON 摘要，`--results` 会写入 `report.json`。`verify` 只读 SQLite，重算窗口摘要、账本、梯度裁决和持久化结论。

## CLI

```text
capacity_engine run      --scene SCENE --db DB [--results DIR] [--crash-after-windows N]
capacity_engine resume   --db DB [--scene SCENE] [--results DIR] [--crash-after-windows N]
capacity_engine status   --db DB [--format json|text]
capacity_engine report   --db DB [--format json|text] [--output FILE]
capacity_engine verify   --db DB [--format json|text]
```

`--crash-after-windows N` 是测试钩子：第 N 个新窗口提交后直接 `os._exit(75)`，用于验证强制终止恢复。

## 场景文件

固定样本位于 `samples/scene.json`，测试服务配置位于 `samples/mock_service.json`。

核心字段：

```json
{
  "services": [{
    "name": "checkout",
    "address": "http://127.0.0.1:18080",
    "tenants": {
      "tenant-a": {
        "success_rate_min": 0.95,
        "p95_latency_max_ms": 100,
        "concurrency_limit": 16
      }
    }
  }],
  "batches": [{
    "batch_id": "batch-a",
    "tenant": "tenant-a",
    "concurrency_gradients": [1, 2, 4, 8, 16],
    "duration_logical_ms": 20,
    "request_pace_ms": 5,
    "allow_early_termination": true
  }],
  "clock": {
    "start_ms": 100000,
    "window_ms": 5,
    "step_table": [5],
    "gradient_gap_ms": 0
  },
  "precision": {
    "latency_scale": 1,
    "percent_scale": 10000,
    "compare_tolerance": {"success_rate": 0.05, "p95_latency_ms": 2.0},
    "degraded_margin": {"success_rate": 0.005, "p95_latency_ms": 0.1},
    "error_budget": {"success_rate": 0.001, "p95_latency_ms": 2.0},
    "request_logical_budget_ms": 500
  },
  "budget": {"global_logical_time_ms": 1000}
}
```

兼容字段别名包括 `batch_no`、`gradients`、`duration_ms`、`pace_ms` 和 `success_rate_min_percent`。

校验失败会在创建 SQLite 或结果目录前返回业务错误，例如：

- `E_DUPLICATE_SERVICE` / `E_DUPLICATE_TENANT`
- `E_INVALID_ADDRESS`
- `E_INVALID_GRADIENT`
- `E_INVALID_DURATION` / `E_INVALID_PACE`
- `E_INVALID_SLO`
- `E_INVALID_SCALE` / `E_INVALID_TOLERANCE` / `E_INVALID_BUDGET`

## 逻辑时钟与窗口

`clock.step_table` 是 wave 级的逻辑递增量，循环使用；为空时使用批次的 `request_pace_ms`。每个 wave 在确定的逻辑时刻释放 `concurrency` 个请求。

窗口锚定梯度起点：

```text
window_index = (release_ms - gradient_start_ms) // window_ms
```

请求只属于半开窗口 `[start_ms, end_ms)`。窗口边界、请求 ID、梯度持续时间不读取系统墙钟。系统墙钟只用于多进程忙等待超时。

## 定点与账本

- 比率：`success_count / request_count * percent_scale`，half-up 到整数。
- 延迟样本：毫秒 Decimal × `latency_scale`，half-up 到整数。
- P95：稳定排序后按 `(n-1)*95/100` 做线性插值，再 half-up。

每条账本记录包含 `scope_key`、`local_seq`、报告中的确定性 `seq`、标度、精确有理数、结果、被舍弃余量、舍入方向、换算误差、累计误差上界、来源和细节。

报告中的 `seq` 是按 `(scope_key, local_seq)` 排序后的确定性登记序号；回查 SQLite 时使用 `scope_key + local_seq`，并行提交不会改变最终报告的登记编号。

## 容量状态机

- `PASS`：成功率和 P95 都满足 SLO。
- `DEGRADED_PASS`：SLO 有未达标，但违规量不超过 `degraded_margin`。
- `ABORT`：违规量超过 `degraded_margin`。

`allow_early_termination=true` 时，`ABORT` 会把同批次后续未认领梯度标为 `SKIPPED_EARLY_TERMINATION`。容量取最后一个通过梯度的并发数，拐点取第一个 `ABORT` 的并发数。

预算不足记录 `GLOBAL_LOGICAL_BUDGET_EXHAUSTED`；单租户并发上限不足记录 `TENANT_CONCURRENCY_LIMIT_EXHAUSTED`。

## 精度敏感与翻转

相邻梯度或同服务同位置租户比较成功率和 P95。当差值小于 `compare_tolerance` 且指标类别/容量边界在更高标度下发生变化时，输出敏感标记。

每个标记包含参与窗口、两侧指标登记引用、误差上界、差值误差上界、原始容量结论、100 倍标度下的指标和容量结论，以及：

- `higher_scale_flips`
- `capacity_conclusion_flips`

## SQLite 与恢复

- WAL、`synchronous=FULL`、`BEGIN IMMEDIATE`；
- 写事务外层文件锁；
- 单元认领、租户槽位、预算预留均在事务中完成；
- 窗口摘要、账本、请求错误诊断同事务提交；
- 同一窗口同一版本重复提交相同 payload 幂等返回，不同 payload 返回 `E_WINDOW_SUMMARY_MISMATCH`；
- 恢复时清理死进程 lease，已提交窗口跳过，只重放未提交窗口；
- 预算预留记录在单元上，恢复不会重复扣减。

## 请求异常码

| 错误码 | 场景 |
|---|---|
| `E_CONNECTION_REFUSED` | 连接拒绝/连接重置 |
| `E_REQUEST_BUDGET_EXCEEDED` | 模拟延迟超过单请求逻辑预算 |
| `E_STATUS_CODE_MISMATCH` | 非预期 HTTP 状态码 |
| `E_MALFORMED_RESPONSE` | 空/非 JSON/缺少模拟延迟字段 |
| `E_TARGET_INTERRUPTED` | 502/503/504、断连或超时 |
| `E_WINDOW_SUMMARY_MISMATCH` | 窗口摘要/payload 冲突或校验重算不一致 |

请求异常作为窗口内观测记录隔离，不会中断其他租户窗口；窗口仍会提交并参与失败率、P95 和梯度裁决。

## 实际验收结果

固定样本（`artifacts/acceptance_summary.json`）：

- 计划请求 130，提交窗口 24，账本登记 434；
- 逻辑预算使用 120 / 1000 ms；
- `batch-a`：原始标度容量 8、拐点 16；
- `batch-a` 高标度（100 倍）：容量 4、拐点 8，结论翻转；
- `batch-b`：容量 2，无拐点；
- 精度敏感标记 3 条：
  - P95 4→8，差值 0；
  - P95 8→16，差值 1 ms；
  - 成功率 8→16，差值 0.0313；
- `verify`：163 项检查，0 失败；
- 强制在第 3 个窗口后退出：恢复后 24 个窗口，报告 SHA-256 与无中断运行一致；
- 双进程共享 SQLite：两进程分别提交 4/20 个窗口，共 24 个窗口，重复窗口 0，预算 120 ms，最终报告哈希与顺序运行一致。

验收证据：

- `artifacts/demo_report.json`
- `artifacts/demo_verification.json`
- `artifacts/demo_status.json`
- `artifacts/demo_run_summary.json`
- `artifacts/recovery_summary.json`
- `artifacts/concurrency_summary.json`
- `artifacts/error_code_matrix.json`
- `artifacts/acceptance_summary.json`
- `artifacts/test_output.txt`

## 测试

```bash
scripts/run_tests.sh
```

实际结果：Python 3.11.18 下 22 个测试全部通过（另在 Python 3.10.12 下复跑通过）。覆盖场景校验、定点账本、P95 插值、容量拐点、精度翻转、五类请求异常、窗口冲突、预算/租户上限、中断恢复、跨租户并发和同租户不同梯度并发。

