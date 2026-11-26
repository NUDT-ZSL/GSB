# 引擎连接顺序与代价模型诊断工作台

这是一个纯本地、无容器依赖的确定性验收工具。它把算子任务、平台统计和候选计划转换成可审计的子算子 DAG、双量纲代价、预算调度、期限仲裁、漂移诊断和 SQLite 执行账本。

## 快速开始

环境已在当前 WSL Ubuntu 工作区验证：

```text
Python 3.10.12
SQLite 3.37.2 (Python 标准库 sqlite3)
NumPy 2.0.0（本工具核心路径不依赖 NumPy）
```

```bash
python3 --version
python3 scripts/generate_samples.py --out samples

DB=var/demo.sqlite3
./engine-diag --db "$DB" load \
  --tasks samples/tasks_v1.jsonl \
  --stats samples/platform_stats.json \
  --plan samples/plans_v1.json

./engine-diag --db "$DB" build --plan-version p1
./engine-diag --db "$DB" inject --plan-version p1 \
  --seed 777 --drift-count 6 --anomaly-count 2

# 重新执行代价计算，把漂移真值固化到同一候选比较中。
./engine-diag --db "$DB" build --plan-version p1

./engine-diag --db "$DB" schedule --plan-version p1 \
  --budget 90000 --max-concurrency 3 --rounds 4 \
  --start-clock 0 --round-span-ms 100

./engine-diag --db "$DB" status --plan-version p1 > /tmp/status.json
./engine-diag --db "$DB" verify --plan-version p1
```

若需从空状态重跑，使用新的数据库路径即可；`samples/generate_samples.py` 只写入 `samples/`，不会访问网络。

完整自动化验收也可直接运行：

```bash
bash scripts/acceptance.sh var/acceptance.sqlite3
```

## 目录

```text
engine_diag/
  models.py       任务、平台统计、候选计划的严格解析与校验
  subtasks.py     固定种子、规范 JSON 哈希驱动的子算子生成
  cost.py         双量纲代价、漂移事件与可审计聚合顺序
  scheduler.py    预算、并发上限、期限和异常仲裁
  invalidation.py 修订、计划版本和缓存变化的增量闭包
  storage.py      SQLite 事务、文件锁、水位、恢复与迟到策略
  service.py      七个命令的业务编排
  cli.py          稳定 JSON 命令行
samples/          固定种子样本及清单
scripts/          样本生成器
tests/            unittest 验收测试
```

## 输入契约

### 算子任务 JSONL

```json
{
  "job_id": "job-alpha",
  "op_kind": "matmul",
  "input_shapes": [[64, 96], [96, 48]],
  "dtype": "float16",
  "layout": "ROW_MAJOR",
  "seed": 1001,
  "revision": 1,
  "logical_clock_ms": 100,
  "precision_sensitive": true
}
```

- `op_kind`: `matmul`、`conv2d`。
- `dtype`: `float16`、`float32`、`bfloat16`、`int8`。
- `layout`: matmul 使用 `ROW_MAJOR`/`COL_MAJOR`；conv2d 使用 `NCHW`/`NHWC`。
- matmul 形状为 `A[m,k]`、`B[k,n]`，且 `A.k == B.k`。
- conv2d 形状为 `input[N,C,H,W]`、`weight[O,C,KH,KW]`，且通道相等、卷积核不超过输入空间。
- 所有维度必须是正整数；`seed`、`logical_clock_ms` 非负；`revision >= 1`。
- 单个任务文件内 `job_id` 唯一，`logical_clock_ms` 非降。

### 平台统计

支持 JSONL、JSON 数组，或带 `max_threads` 外层对象的 JSON：

```json
{
  "max_threads": 16,
  "entries": [
    {
      "logical_clock_ms": 0,
      "cache_layer": "L1",
      "hit_rate": 0.92,
      "thread_occupancy": 8,
      "throughput_cost": 1.02
    }
  ]
}
```

- `cache_layer`: `L1`、`L2`、`L3`、`DRAM`。
- `hit_rate` 位于 `[0,1]`；`thread_occupancy` 位于 `[0,max_threads]`；`throughput_cost` 非负有限。
- `(logical_clock_ms, cache_layer)` 唯一。
- 新统计的逻辑时钟不得早于已持久化水位。

### 候选计划

```json
{
  "plan_version": "p1",
  "global_seed": 20261127,
  "base_logical_clock_ms": 0,
  "logical_clock_step_ms": 10,
  "max_threads": 16,
  "cost_model": {
    "instruction_weight": 1.0,
    "memory_weight": 0.75,
    "cache_cpu_penalty": 0.35,
    "cache_memory_penalty": 2.0,
    "occupancy_penalty": 0.15,
    "logical_ms_per_cost_unit": 0.001
  },
  "jobs": {
    "job-alpha": {
      "priority": 1,
      "deadline_clock_ms": 160,
      "cost_estimates": {
        "weight_sum": {"cpu_instructions": 24000, "memory_bytes": 16000},
        "activation": {"cpu_instructions": 6000, "memory_bytes": 4500}
      },
      "dependencies": [],
      "critical_path_id": "path-alpha-beta",
      "threads_required": 4
    }
  },
  "candidates": [
    {
      "candidate_id": "alpha-first",
      "job_order": ["job-alpha"],
      "interleave_jobs": false
    }
  ],
  "critical_paths": {
    "path-alpha-beta": ["job-alpha"]
  }
}
```

`priority` 越小优先级越高。每个候选的 `job_order` 必须恰好覆盖计划中的全部任务。候选可覆盖 `priority`、`deadline_clock_ms`、`cost_estimates`、`critical_path_id`、`threads_required`，但不能覆盖任务语义字段 `dependencies` 和 `precision_sensitive`。

## 确定性子算子

每个任务被拆为：

1. 有序 `weight_sum` 分片链；分片轴是 matmul 的 `k` 或 conv2d 的通道维 `C`。
2. 依赖所有分片的 `activation` 融合节点。
3. 计划级跨任务依赖追加到后继任务第一个分片，并通过哈希向后递归传播。

分片数、分片边界、私有种子、依赖和 ID 都来自规范 JSON 的 SHA-256：

```text
subtask_id = "st_" + sha256(canonical_json(subtask_payload))
summary    = sha256(canonical_json(all_ordered_subtasks))
```

不使用 Python `hash()`、时间、PID、目录遍历或随机模块，因此不同进程及不同 `PYTHONHASHSEED` 下逐项一致。子算子摘要就是漂移观测、账本和真值比对的唯一语义来源。

## 代价模型

候选形态顺序由 `job_order` 与可选任务间交错确定。每个聚合步骤固定在 `base_logical_clock_ms + index * logical_clock_step_ms` 观测平台统计，并按索引选择 `L1/L2/L3/DRAM`。因子为：

```text
miss             = 1 - hit_rate
occupancy_factor = 1 + (thread_occupancy / max_threads) * occupancy_penalty
cpu_factor       = throughput_cost * occupancy_factor
                   * (1 + miss * cache_cpu_penalty) * drift_cpu
memory_factor    = throughput_cost * occupancy_factor
                   * (1 + miss * cache_memory_penalty) * drift_memory
step_cost        = cpu_instructions * instruction_weight
                   + memory_bytes * memory_weight
```

每一步都持久化 `aggregation_index`、子算子 ID/哈希、逻辑时钟、缓存层、完整平台观测、命中事件、漂移事件、干净代价、漂移后代价和持续时间。总代价在固定顺序上用 `Decimal` 累加后六位半入舍入。

漂移倍数在相同逻辑时钟采样；候选比较同时给出 `clean_total_cost`、`observed_total_cost` 与 `drift_delta`，即离线比较的漂移真值。

## 调度与仲裁

- 就绪队列稳定排序键：`priority`、`deadline_clock_ms`、`job_id`、子算子序号、子算子 ID。
- 每轮预算独立重置；每轮最多启动 `max_concurrency` 个子任务。
- 预算按 `step_cost` 扣除，已覆盖该步骤的 CPU 指令与访存字节。
- 节点状态显式区分：`committed`、`completed_deadline_miss`、`failed_after_start`、`blocked_anomaly`、`blocked_dependency`、`abandoned_budget`。
- 预算不足而从未启动的节点只记录 `abandoned_budget/round_budget_exhausted`，不会改写为超时。
- 终止期限按严格小于处理：`end_clock_ms < deadline_clock_ms`。
- 任务发生期限溢出时输出从首个问题节点开始的 `overflow_chain`、关键路径和受影响任务集合。
- 注入的 `deadlock`、`resource_overrun` 以及超过 `max_threads` 的节点均保留异常记录，不静默淘汰。
- 仲裁签名是 `sha256(plan_version, job_id, deadline_clock_ms)`；SQLite 唯一约束保证同一直截止期限只认可第一次决策，后续差异保留在 `arbitration_conflicts`。

## 增量失效

- 任务修订：比较检查点修订与当前修订生成的有序子算子，哈希差异向后递归传播到依赖节点。
- 计划版本切换：按 `(job_id, phase, shard_index)` 逐项比较子算子哈希；相同项直接复用账本，仅异项触发闭包。
- 缓存层变化：生成“代价失效、账本不变”的闭包，并把相关变更标记为已消费。
- 已提交的旧修订或已移除子算子只进入 `protected_committed`，账本不更新、不删除。
- 闭包以完整差异集合的指纹去重，重复构建不会重复追加。

## SQLite 与恢复

数据库启用 WAL、`synchronous=FULL`、`busy_timeout`，写操作额外使用 `fcntl.flock` 文件锁。主要保证：

- `ledger.subtask_id` 主键使同一子算子只提交一次；重复相同结果幂等。
- 执行前先提交 `ledger_attempts(pending)`，执行后在一个事务中写账本并标记提交。
- 强制中断后 `resume` 找到孤立 pending 记录，先标记 `orphaned`，再复用/补齐账本。
- 任务水位在临时表中整体暂存，分配新 `watermark_version` 后只切换 `meta.current_watermark_version` 指针。
- 迟到结果写入 `late_arrivals`，采纳策略固定为 `reject_late_and_keep_accepted`，已认可摘要不变。
- 调度运行、逐节点状态、仲裁决策、失效闭包、漂移事件和迟到记录均可追溯。

## CLI

全局参数：`--db PATH`，默认 `var/engine_diag.sqlite3`。

```text
load      --tasks FILE [--stats FILE] [--plan FILE] [--max-threads N]
build     [--plan-version V] [--crash-after N]
schedule  --budget N [--max-concurrency N] [--rounds N]
          [--start-clock MS] [--round-span-ms MS] [--candidate-id ID]
inject    [--seed N] [--drift-count N] [--anomaly-count N]
          [--duration-steps N] [--clear]
          [--late-subtask-id ID --late-digest HEX]
resume    [--plan-version V]
status    [--plan-version V]
verify    [--plan-version V]
```

所有命令输出稳定排序 JSON。业务异常统一为：

```json
{
  "ok": false,
  "error": {
    "code": "INVALID_DTYPE",
    "message": "dtype is not in the declared set",
    "details": {}
  }
}
```

主要错误码包括 `INVALID_OP_KIND`、`INVALID_DTYPE`、`INVALID_LAYOUT`、`INVALID_SHAPE`、`SHAPE_OP_MISMATCH`、`CLOCK_REGRESSION`、`DUPLICATE_JOB_ID`、`PLAN_VERSION_CONFLICT`、`INVALID_COST`、`INVALID_DEADLINE`、`INVALID_THREAD_OCCUPANCY`、`PLAN_CYCLE`、`UNKNOWN_JOB`、`NO_PLATFORM_STAT`、`LATE_RESULT` 和 `STATE_CONFLICT`。

## 验收场景

### 计划版本切换

```bash
DB=var/invalidation.sqlite3
./engine-diag --db "$DB" load \
  --tasks samples/tasks_v1.jsonl \
  --stats samples/platform_stats.json \
  --plan samples/plans_v1.json
./engine-diag --db "$DB" build --plan-version p1

./engine-diag --db "$DB" load --plan samples/plans_v2.json
./engine-diag --db "$DB" build --plan-version p2
./engine-diag --db "$DB" build --plan-version p2
```

`plans_v2.json` 删除 `job-delta`，保留其余任务和全局种子。首次切换只产生一个 `plan_version_switch` 闭包，已提交的 5 个 `job-delta` 子算子进入保护集合；新增计划中的 12 个未变子算子全部复用账本。重复构建时闭包 `inserted=false`，不再重算。

### 任务修订

```bash
./engine-diag --db "$DB" load --tasks samples/tasks_revision.jsonl
./engine-diag --db "$DB" build --plan-version p2
```

`job-gamma` 从 revision 1 变为 2，闭包只包含新修订链及其依赖后代；旧修订的已提交账本不被覆盖，未受影响任务继续复用。

### 崩溃恢复

```bash
./engine-diag --db var/crash.sqlite3 load \
  --tasks samples/tasks_v1.jsonl \
  --stats samples/platform_stats.json \
  --plan samples/plans_v1.json

./engine-diag --db var/crash.sqlite3 build --plan-version p1 --crash-after 2
# 进程以 75 退出，数据库保留两个已提交项、一个 pending attempt、无水位。

./engine-diag --db var/crash.sqlite3 resume --plan-version p1
./engine-diag --db var/crash.sqlite3 verify --plan-version p1
```

恢复后为 17 个唯一账本项，`pending=0`、重复账本数为 0。

### 并发构建

```bash
for i in 1 2 3 4; do
  ./engine-diag --db "$DB" build --plan-version p1 &
done
wait
./engine-diag --db "$DB" verify --plan-version p1
```

四个进程并发时每项只提交一次，重复进程得到 `reused`，SQLite 完整性检查为 `ok`。

## 实际验证结果

在 2026-11-27 的当前 WSL Ubuntu 环境执行：

```text
python3 -m unittest discover -s tests -v
Ran 24 tests in 5.485s
OK
```

固定种子 `20261127` 下：

```text
子算子总数                         17
job-alpha / beta / gamma / delta   4 / 5 / 3 / 5
子算子摘要                         68997517a5c34e6c139e96ae4c683fef9ff1aa49c32201be9306ffc2a782c37e
代价评估摘要                       834607c3074052b000c9e2095200e972d173504812b147340816ac67dc8970bd
胜出连接顺序                       alpha-beta-gamma-delta
```

无漂移候选总代价：

```text
alpha-beta-gamma-delta   273179.041182
interleaved-deadline     273727.781356
delta-gamma-beta-alpha   274666.437990
```

注入种子 `777`、6 个漂移点、2 个异常后的总代价：

```text
alpha-beta-gamma-delta   316474.129527   drift_delta=43295.088345
interleaved-deadline     316735.535281   drift_delta=43007.753925
delta-gamma-beta-alpha   318040.488372   drift_delta=43374.050382
```

`verify` 在该状态下通过 16 项检查，错误和警告均为 0。连续两次 `status` 字节级一致，摘要为 `8fe11aabd5075b0f8127ee94ff8f950a7e7b93b79cd9afc342dd071a8adf559e`。强制中断恢复后账本 17 项、重复 0；四进程并发构建后账本 17 项、重复 0、pending 0。

## 边界

- 子算子输出摘要是确定性的模拟摘要，用于验证调度与账本协议，不连接真实计算引擎。
- 样本数值是可复现的验收载荷，不代表某硬件的真实性能。
- 工具不访问网络、不启动容器，也不会清理用户指定数据库之外的路径。

