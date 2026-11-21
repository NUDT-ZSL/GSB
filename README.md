# 本地动态批处理调度内核

这是一个只依赖 Python 标准库（`sqlite3`、`fcntl`）的本地推理动态批处理内核。所有时间判定都来自调用方注入的逻辑时钟毫秒值，不读取系统墙钟；窗口、批次、请求状态、资源占用、误差谱和逻辑时钟水位全部落盘到工作区内的 SQLite。

## 快速开始

要求 Python 3.10+、`sqlite3`、`numpy`（本实现不把 numpy 作为运行时依赖，但当前环境已确认 `numpy 1.26.4` 可用）。

```bash
python3 -m batchkernel.cli --state-dir .runtime/state load \
  --model samples/model.json \
  --replicas samples/replicas.json \
  --requests samples/requests.json \
  --precision samples/precision.json \
  --reset

python3 -m batchkernel.cli --state-dir .runtime/state schedule --clock 1200
python3 -m batchkernel.cli --state-dir .runtime/state status --pretty
python3 -m batchkernel.cli --state-dir .runtime/state verify --pretty
```

一键运行固定样本、强杀恢复对比并生成结果工件：

```bash
./scripts/demo.sh
```

结果文件位于 `artifacts/demo-status.json`、`artifacts/demo-verify.json`、`artifacts/demo-summary.json`。

## 输入文件

所有输入均为本地 JSON。解析、字段校验和跨文件约束检查都发生在任何状态写入之前；输入非法时返回业务错误码，且不会创建或修改 SQLite 状态。

### 模型描述

模型总参数量用于算力估算，`per_request_tokens` 是模型侧的参考 token 画像，具体请求仍以轨迹中的 token 数为准。

```json
{
  "model_id": "tiny-local-llm",
  "layers": [
    {"name": "embedding", "parameters": 2000000},
    {"name": "transformer_0", "parameters": 4000000}
  ],
  "per_request_tokens": {"input": 6, "output": 4},
  "compute_units_per_million_parameters_per_token": 1.0,
  "memory_mib_per_million_parameters": 2.0,
  "memory_mib_per_token": 0.125
}
```

后三个估算因子可省略，默认值分别为 `1.0`、`2.0`、`0.125`。层名必须非空且唯一，每层参数量为非负整数，总参数量必须为正。

### 副本快照

```json
{
  "version": 1,
  "replicas": [
    {
      "replica_id": "r0",
      "compute_units": 200.0,
      "memory_limit_mib": 64,
      "batch_window_ms": 100,
      "max_batch_size": 4,
      "max_sequence_length": 512
    }
  ]
}
```

约束：`replica_id` 唯一；算力为有限正数；显存上限、批窗口、最大批大小、最大序列长度均为正整数；每个副本的最大序列长度不得小于任一请求的输入与最大输出 token 数之和。

### 请求轨迹

```json
{
  "requests": [
    {
      "request_seq": 1,
      "arrival_ms": 0,
      "priority": 3,
      "input_tokens": 6,
      "max_output_tokens": 4,
      "deadline_ms": 5000
    }
  ]
}
```

`request_seq` 唯一；`arrival_ms`/`deadline_ms` 为非负整数，且截止不早于到达；token 数非负且总和为正。可选的 `replica_id` 可显式绑定副本；可选的 `simulate_failures` 用于确定性执行失败/连续失败演示。

### 精度配置

```json
{
  "compute_scale": 1000,
  "memory_scale": 1000,
  "queue_scale": 1000,
  "relative_tolerance": 0.0001,
  "error_bound_budget": 0.01,
  "clock_step_ms": 10
}
```

三个定点标度必须为正整数；相对容差、误差上界预算必须为有限正数；逻辑时钟步进为正整数。调度主循环按 `clock_step_ms` 逐个逻辑 tick 推进，并在每个 tick 处理所有已经到期的窗口；窗口事件本身仍严格在副本窗口边界关闭。`status.clock.logical_clock_ticks` 给出当前水位对应的逻辑 tick 数。

## 调度语义

### 逻辑时钟与窗口

- 时间单位是注入的逻辑毫秒，代码中不存在 `time.time()`、`datetime.now()` 等墙钟读取。
- 初始时副本窗口从 `[0, w)` 开始；每次只处理 `end_ms <= target_clock_ms` 的窗口，因此窗口期满即关闭，即使没有请求也会留下 `committed` 的空窗口。
- 窗口边界由“副本窗口宽度 + 已提交的时间水位”决定，不依赖 wall clock。
- 同一轨迹无论一次推进到 `T`，还是先推进到中间时钟再推进到 `T`，最终窗口和批次结果一致。
- 时钟只能单调前进；回退返回 `E_CLOCK_REGRESSION`，不修改状态。

### 请求到副本的确定性归属

请求若声明 `replica_id`，直接使用该副本。否则按排序后的当前副本 ID 列表取：

```text
replica_id = sorted(replica_ids)[request_seq % len(replica_ids)]
```

归属在请求加载时持久化。副本版本变更后，已绑定窗口的请求不迁移；尚未进入任何窗口且所属副本被移除的请求，会按同一规则确定性重分配并写一条 `REQUEST_REASSIGNED` 事件。

### 批组成与优先级插队

每个窗口内按以下稳定键排序候选请求：

```text
priority DESC, arrival_ms ASC, request_seq ASC
```

随后逐批扫描并检查 `max_batch_size`、可用算力、显存上限和序列长度。高优先级请求因算力或显存放不进当前批时，会写入 `skipped`，记录：

- `request_seq`、优先级、输入/输出/总 token 数；
- 尝试前的剩余算力与显存；
- 本请求算力/显存与尝试后的聚合值；
- `first_trigger`（`compute` 或 `memory`）；
- 观测顺序。

批次裁决 JSON 还保存候选顺序、每个观测项和聚合顺序。相同持久化状态上的重复裁决逐位一致。

## 定点换算与误差谱

估算公式（所有浮点运算都记录操作顺序和误差传播）：

```text
params_m       = total_parameters / 1_000_000
compute_raw    = params_m * (input_tokens + max_output_tokens) * compute_factor
memory_raw_mib = params_m * memory_per_million + token_sum * memory_per_token
queue_raw_ms   = batch_compute_raw / replica.compute_units * 1000
```

每个批次的 `compute`、`memory`、`queue` 各有一条精度记录，包含：

- 标度、原始浮点值、定点整数值；
- `rounding_direction`（`exact` / `up` / `down`）；
- 量化误差上界、算术误差上界、总误差上界、误差预算和是否在预算内；
- 每个加法/乘法/除法的输入、结果、本次误差和累计误差；
- 来源链与聚合顺序。

误差传播使用 IEEE-754 double 的机器 epsilon：

```text
add:     e = ea + eb + eps * |result|
mul:     e = |a| * eb + |b| * ea + eps * |result|
div:     e = ea / |b| + |a / b^2| * eb + eps * |result|
quant:   e = 0.5 / scale
```

当“剩余预算 vs 请求成本”的差落在相对容差内，或实际候选批与替换一个请求后的候选批估算差落在容差内且选择依赖该差值时，批次标记 `precision_sensitive=true`，并输出误差来源链。样本中的 `200/200` 算力边界会产生 `absolute_gap=0.0` 的敏感记录。

任一定点项的累计误差上界超过 `error_bound_budget` 时，会追加 `error_budget_exceeded` 敏感决策；`verify` 保留该批的可运行性，但以 warning 显示超预算记录数。

## 请求生命周期

状态迁移全部从 SQLite 读取，不使用内存残留：

```text
pending -> batched -> executing -> completed
                   \-> expired
                   \-> failed -> executing -> failed   (确定性重试演示)
pending -> expired
```

- `pending`：已加载，尚未进入窗口。
- `batched`：窗口和批次已提交。
- `executing`：批估算完成时刻已到，正在尝试执行。
- `completed`：正常完成。
- `expired`：窗口关闭前已过截止，或完成时刻晚于截止。
- `failed`：真实执行失败；`failure_count` 只在这里递增。

批次预计完成时刻为 `window_end_ms + ceil(queue_fixed / queue_scale)`。`expired` 不会增加失败计数；`REQUEST_EXPIRED` 与 `REQUEST_EXECUTION_FAILED` 是不同事件。`simulate_failures=N` 会产生 N 次失败事件和 N-1 次 `REQUEST_RETRY_EXECUTING`，每次失败都带前后计数。`status.requests[].lifecycle_events` 输出完整事件链与计数变化。

## 持久化与恢复

SQLite 使用 WAL、`synchronous=FULL`、`BEGIN IMMEDIATE`，并通过 `fcntl.flock` 对写操作加进程级排他文件锁。

一次窗口提交分三阶段：

1. **计划事务**：写窗口头、请求归属、批次、完整误差谱、敏感决策，并推进窗口水位。
2. **逐批提交事务**：批量把 `planned` 批次变为 `committed`，请求变为 `batched`，原子增加资源占用和 ledger 事件。
3. **窗口收尾事务**：处理超期请求，把窗口变为 `committed`，写事件。

进程可能在计划事务内、计划提交后、批次事务内、批次提交后或窗口提交后被强制终止。恢复时：

- `planned` 窗口直接重放提交；
- `applying` 窗口只提交仍为 `planned` 的批次；
- 已 `committed` 批次、已执行批次、已写误差谱均跳过；
- 资源只在批次 `planned -> committed` 成功时增加一次，只在 `executed 0 -> 1` 时释放一次；
- 目标时钟和在途标识 `schedule_target_ms` 在开始推进前持久化，`resume` 会完成该目标后清除。

测试用故障注入（只影响显式设置该环境变量的进程）：

```bash
BATCHKERNEL_FAULT=during_plan_build
BATCHKERNEL_FAULT=after_window_plan
BATCHKERNEL_FAULT=during_batch_commit
BATCHKERNEL_FAULT=after_batch_commit
BATCHKERNEL_FAULT=after_window_commit
BATCHKERNEL_FAULT_BATCH_COUNT=2   # 仅 after_batch_commit 使用
```

触发时进程以退出码 `91` 强制退出，下一次 `resume` 从 SQLite 恢复。

## 副本版本

- 每次内容变化必须使用更大的 `version`；完整快照写入 `replica_versions`，`replica_current` 单行指针原子切换。
- 内容完全一致：`no_op`，不推进版本。
- 旧副本 ID 全部保留且各自参数不变、只新增副本：`incremental_append`。
- 删除、修改算力/显存/窗口等参数：`full_replace`。
- 版本回退返回 `E_REPLICA_VERSION_ROLLBACK`；同版本不同内容返回 `E_REPLICA_VERSION_CONFLICT`。
- 每个窗口持久化 `replica_version`。版本切换时，新窗口从旧水位之后按新窗口宽度续接，避免跨版本窗口时间重叠。
- `status`/`verify` 在单个 SQLite 读事务内读取快照和窗口，不会观察到指针与参数混合状态。

## 命令行

`--state-dir` 可放在子命令之前或之后；两种写法等价。

| 子命令 | 作用 |
|---|---|
| `load` | 校验并加载模型、副本、请求、精度；`--reset` 重建状态；可只加载副本快照做版本更新 |
| `schedule --clock T` | 注入逻辑时钟并推进到 `T` |
| `resume` | 恢复未提交窗口并完成持久化的在途目标 |
| `status` | 稳定排序 JSON，输出水位、窗口归属、批次集合、跳过项、精度敏感标记和来源链 |
| `verify` | 只读核验窗口唯一归属、批次幂等、资源、版本、生命周期与误差谱一致性 |

常用错误码：

| 错误码 | 含义 |
|---|---|
| `E_BAD_MODEL_INPUT` / `E_BAD_REPLICA_INPUT` / `E_BAD_REQUEST_INPUT` / `E_BAD_PRECISION_INPUT` | 输入结构或字段非法 |
| `E_DUPLICATE_REPLICA` / `E_DUPLICATE_REQUEST` | ID 或请求序号重复 |
| `E_REPLICA_COMPUTE_INVALID` / `E_REPLICA_CAPACITY_INVALID` / `E_REPLICA_WINDOW_INVALID` | 副本数值非法 |
| `E_SEQUENCE_LIMIT_TOO_SMALL` | 序列长度上限小于某个请求 token 总和 |
| `E_REQUEST_CLOCK_INVALID` / `E_DEADLINE_BEFORE_ARRIVAL` | 逻辑时钟字段非法或截止早于到达 |
| `E_BAD_FIXED_SCALE` / `E_BAD_TOLERANCE` / `E_BAD_ERROR_BUDGET` | 精度配置非法 |
| `E_CLOCK_REGRESSION` | 逻辑时钟回退 |
| `E_REPLICA_VERSION_ROLLBACK` / `E_REPLICA_VERSION_CONFLICT` | 版本回退或同版本冲突 |
| `E_BATCH_UNSCHEDULABLE` | 单请求无法放入副本的算力/显存预算 |
| `E_STATE_CORRUPT` / `E_RECOVERY_STATE_CONFLICT` | 状态文件或恢复路径冲突 |

## 固定样本

样本包含两个副本（`r0` 窗口 100ms/算力 200，`r1` 窗口 150ms/算力 300）和 8 个请求：

- 请求 1/3/5 进入 `r1` 窗口 0，按优先级 `3,5,1` 组成一个恰好 300 算力的敏感批；
- 请求 2/4/6 进入 `r0` 窗口 0，批 0 为 `[4,2]`，请求 6 因 `compute` 被跳过并进入批 1；
- 请求 7 截止于 130ms，在 `r1` 窗口 0 关闭时变为 `expired`，失败计数保持 0；
- 请求 2 配置一次执行失败，最终为 `failed`，失败计数 1；
- 请求 8 在 `r0` 窗口 1 完成。

## 实测结果

运行环境：Python 3.10.12、SQLite 3.37.2、numpy 1.26.4。

自动化测试：

```text
python3 -m unittest discover -v
Ran 18 tests in 28.086s
OK
```

固定样本 `schedule --clock 1200`：

```json
{
  "action": "scheduled",
  "target_clock_ms": 1200,
  "windows_planned": 20,
  "batches_executed": 4
}
```

请求状态计数：`{"completed": 6, "expired": 1, "failed": 1}`。

精度敏感批：

```json
[
  {
    "replica_id": "r0",
    "window_index": 0,
    "batch_index": 0,
    "request_seqs": [4, 2],
    "batch_budget_check": {"metric": "compute", "absolute_gap": 0.0},
    "candidate_batch_alternative": [
      {"metric": "compute", "absolute_gap": 0.0},
      {"metric": "memory", "absolute_gap": 0.0},
      {"metric": "queue", "absolute_gap": 0.0}
    ]
  },
  {
    "replica_id": "r1",
    "window_index": 0,
    "batch_index": 0,
    "request_seqs": [3, 5, 1],
    "batch_budget_check": {"metric": "compute", "absolute_gap": 0.0}
  }
]
```

强制终止恢复（恢复后 `verify` 19/19 通过）：

```text
crash exit code: 91
resume recovered_windows: 1
status_identical_after_resume: true
baseline verify ok: true
recovered verify ok: true
```

5 个故障点均覆盖：`during_plan_build`、`after_window_plan`、`during_batch_commit`、`after_batch_commit`、`after_window_commit`。

并发一致性：4 个进程同时执行同一 `schedule --clock 1200`，全部返回成功；最终 `status` 与单进程基线完全一致，窗口/批次/请求唯一约束和资源核算通过 `verify`。

非法输入验证：重复副本、重复请求序号、截止早于到达、序列上限过小、定点标度为 0 均返回对应业务错误码；检查状态目录均未被创建。

## 设计边界

- 当前实现是单机本地内核，不启动推理服务网络端口。
- 副本归属策略是确定性的静态分发，不根据实时负载在副本间迁移已归属请求；双重预算由批组成阶段保证。
- 空窗口会持久化，以明确记录“窗口期满关闭”的水位，因此长时间大幅推进时钟会产生较多空窗口行。
- 所有状态都在 `--state-dir` 下；`load --reset` 会清空并重建该目录中的 SQLite 状态。
