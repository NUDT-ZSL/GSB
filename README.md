# 推理批处理定点账本与中断续算裁决引擎

本项目在 Linux/WSL Ubuntu 上仅使用 Python 标准库实现，提供：

- 固定种子、离线可重复的模型/副本/请求/精度/注入样本。
- 基于注入逻辑时钟的窗口切分、请求唯一归属与批组成裁决。
- 算力与显存双预算、最大批大小、截止逻辑时钟约束。
- 每次定点换算的原始浮点、标度、舍入方向、误差上界与来源链。
- SQLite 持久化生命周期、逐批原子提交、真实 `SIGKILL` 中断续算。
- 文件锁 + SQLite 事务下的多进程幂等提交。
- `load`、`run`、`inject`、`resume`、`status`、`verify` 六个验收子命令。

运行环境：Python 3.10+，标准库 `sqlite3` 可用。无第三方依赖、无容器、无网络访问。

## 快速开始

```bash
python3 --version
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"

# 固定种子生成 samples/default（也可直接使用仓库内已生成样本）
python3 generate_samples.py --output samples/default --seed 20261126

# 校验并加载
python3 ledger.py load samples/default --db /tmp/batchledger.db

# 一次性完整执行
python3 ledger.py run --db /tmp/batchledger.db

# 稳定排序 JSON：窗口、批组成、跳过项、误差谱、精度敏感标记、请求状态
python3 ledger.py status --db /tmp/batchledger.db --pretty

# 在临时库中重跑一次性基线并逐字段比对
python3 ledger.py verify --db /tmp/batchledger.db --pretty
```

`verify` 成功时退出码为 `0`，并输出 `"bitwise_match": true`、
`"difference_count": 0`、`"internal_checks": {"passed": true}`。

入口也可写作 `python3 -m batchledger ...` 或 `./bin/batchledger ...`。

## 中断续算演示

```bash
python3 ledger.py load samples/default --db /tmp/resume.db

# 注册真实 SIGKILL：在 window 0 的第 0 批提交后杀进程
python3 ledger.py inject \
  --db /tmp/resume.db \
  --window-index 0 \
  --phase batch_commit \
  --batch-index 0

python3 ledger.py run --db /tmp/resume.db
echo $?   # 137（shell 观测值；Python subprocess 为 -SIGKILL）

python3 ledger.py status --db /tmp/resume.db --pretty
# OPEN 且已有批次的窗口会显示 "half_committed": true

python3 ledger.py resume --db /tmp/resume.db
python3 ledger.py verify --db /tmp/resume.db --pretty
```

一键演示：

```bash
./demo.sh
```

## 子命令

| 命令 | 作用 |
| --- | --- |
| `load FIXTURE --db DB [--reset]` | 完整校验夹具，通过后才创建/替换状态库；相同夹具重复加载幂等 |
| `run --db DB [--clock-ms N] [--no-injections]` | 推进逻辑时钟；只关闭 `end_ms <= N` 的窗口，缺省推进到最晚窗口或此前目标时钟 |
| `inject --db DB --window-index N --phase P [--replica-id ID] [--batch-index N]` | 注册确定性 `SIGKILL` 检查点 |
| `resume --db DB [--clock-ms N]` | 从 SQLite 已提交状态继续，未完成批/半提交窗口重放 |
| `status --db DB [--pretty]` | 输出稳定排序 JSON |
| `verify --db DB [--pretty]` | 临时重跑一次性基线，逐字段比较窗口、批、账本、请求状态、生命周期 |
| `generate --output DIR [--seed N]` | 生成固定种子样本 |

错误以 JSON 写到 stderr，退出码约定：

- `0`：成功。
- `2`：输入/业务校验错误，含稳定业务错误码。
- `3`：状态或幂等冲突。
- `4`：结果未完成或 `verify` 不一致。
- `137`：被注入的 `SIGKILL` 终止，按设计用于中断演示。

## 固定种子样本

`samples/default` 由 `random.Random(20261126)` 确定性生成：

- 8 层模型，总参数量约 18 亿。
- 3 个副本：各窗口 100/100/80 逻辑毫秒，双预算不同。
- 48 个突发请求：覆盖 1–5 请求批、优先级跳过、容量边界和截止超期。
- 精度配置：算力/显存/延迟定点标度、浮点容差和误差预算。
- 注入清单默认为空；演示时用 `inject` 子命令注册。

样本目录包含 `manifest.json`、`model.json`、`replicas.json`、
`requests.json`、`accuracy.json`、`injections.json`。`load` 同时支持目录、
manifest 和单文件聚合 JSON。

## 数据契约

模型：

```json
{
  "model_id": "ledger-demo-1.8b",
  "layers": [{"name": "attention_0", "params": 100000000}],
  "flops_per_parameter_per_token": 2.0,
  "memory_base_mib": 24.0,
  "kv_mib_per_token": 0.0625,
  "activation_mib_per_token": 0.00048828125,
  "batch_overhead_ms": 1.5
}
```

副本：

```json
{
  "replica_id": "replica-alpha",
  "compute_capacity": 3000.0,
  "memory_limit_mib": 160.0,
  "window_ms": 100,
  "max_batch_size": 4,
  "max_sequence_length": 2048
}
```

请求：

```json
{
  "request_id": 1,
  "arrival_ms": 0,
  "priority": 2,
  "input_tokens": 128,
  "max_output_tokens": 64,
  "deadline_ms": 1200
}
```

精度：

```json
{
  "compute_scale": 4096,
  "memory_scale": 1024,
  "latency_scale": 1000,
  "float_tolerance": 6.0,
  "error_budget": 0.02,
  "rounding_mode": "half_up"
}
```

注入：

```json
{
  "injection_id": "kill-batch-commit",
  "replica_id": "replica-alpha",
  "window_index": 0,
  "phase": "batch_commit",
  "batch_index": 0,
  "signal": "SIGKILL",
  "enabled": true
}
```

支持的注入阶段：`window_open`、`batch_adjudication`、`batch_commit`、
`window_commit`、`batch_execute`。`replica_id`、`batch_index` 可省略为任意匹配。

校验失败返回明确错误码，例如：

`E_REPLICA_ID_DUPLICATE`、`E_REPLICA_COMPUTE_INVALID`、
`E_REPLICA_MEMORY_INVALID`、`E_REPLICA_WINDOW_INVALID`、
`E_REPLICA_MAX_BATCH_INVALID`、`E_REQUEST_ID_DUPLICATE`、
`E_REQUEST_ARRIVAL_INVALID`、`E_REQUEST_DEADLINE_INVALID`、
`E_REQUEST_SEQUENCE_EXCEEDED`、`E_ACCURACY_SCALE_INVALID`、
`E_ACCURACY_TOLERANCE_INVALID`、`E_ACCURACY_BUDGET_INVALID`。

所有校验在打开/创建 SQLite 前完成；非法输入不会创建数据库、锁文件或临时状态。

## 裁决与精度语义

- 估算公式：
  `request_compute = (input+max_output) * total_params * flops_per_parameter_per_token / 1e9`；
  `request_memory = (input+max_output) * kv_mib_per_token + input * activation_mib_per_token`；
  `batch_memory = memory_base_mib + sum(request_memory)`；
  `duration_ms = batch_overhead_ms + batch_compute / compute_capacity * 1000`。
- `max_sequence_length` 校验覆盖 `input_tokens + max_output_tokens`。
- 窗口索引为 `arrival_ms // window_ms`，区间为
  `[index*window_ms, (index+1)*window_ms)`。
- 请求只使用夹具中的逻辑时间，不读取系统墙钟。
- `status.logical_clock_ms` 是已完整推进到的窗口边界；
  `status.target_clock_ms` 是最近一次注入的目标时钟，SIGKILL 时两者可不同。
- 路由按投影完成时间选择副本，平局按 `replica_id` 排序。
- 窗口在 `end_ms <= injected_clock` 时关闭；未满批也关闭。
- 批内按 `(priority, arrival_ms, request_id)` 排序；优先级数字越小越高。
- 依次检查 `max_batch_size`、`compute_budget`、`memory_budget`、`deadline`；
  被跳过请求记录首个触发约束、试算观测与精度事件。
- 单请求在批开始时刻已无法满足截止时间时进入 `EXPIRED`，不会被重启重纳入批。
- 生命周期状态：`PENDING -> BATCHED -> RUNNING -> COMPLETED`，另有独立的
  `EXPIRED` 和 `FAILED`。
- 定点换算记录 `scale`、`fixed_value`、`raw_float`、`raw_text`、
  `rounding`、`quantization_error`、`error_upper_bound`、`source_chain`。
- 加法误差上界为各输入上界之和；乘法误差上界包含输出量化误差及因子传播误差。
- 两个副本候选差值小于 `float_tolerance`，或预算/截止边界进入容差时，
  输出 `precision_sensitive` 与来源链。

## 持久化、并发与恢复

- 状态库启用 WAL、`synchronous=FULL`、`busy_timeout` 和 `BEGIN IMMEDIATE` 事务。
- 调度进程额外持有 `DB.lock` 的 `fcntl` 排他锁；多进程结果与单进程一致。
- 批次、账本、请求归属、状态迁移在同一事务内提交。
- `assignments.request_id` 主键保证请求不可重复入批；
  `windows(replica_id, window_index, version)` 与批次复合主键保证同一版本只提交一次。
- 提交前重新检查双预算，防止并发超卖；相同 `plan_hash` 的重复提交幂等，
  不同内容触发 `E_IDEMPOTENCY_CONFLICT`。
- 中断恢复只跳过已提交窗口和已持久化批次；`OPEN` 窗口中的缺口按确定性重放补齐。

数据库表：`meta`、`replicas`、`requests`、`windows`、`batches`、
`assignments`、`window_decisions`、`ledger_entries`、`injections`、
`lifecycle`、`replica_state`。

## 测试

```bash
./run_tests.sh
# 或
python3 -m unittest discover -s tests -v
```

测试覆盖：非法输入零持久化副作用、定点账本、窗口唯一归属、双预算与优先级跳过、
超期状态、逻辑时钟分段、真实 `SIGKILL` 五个检查点、半提交窗口、并发调度、
重复提交幂等、`verify` 差异检测和稳定 JSON。

详细设计见 `docs/DESIGN.md`，实际验收记录见 `docs/VALIDATION.md`。
