# schedkernel

本地、确定性、可恢复的动态批处理与调度内核。实现使用 Python 3.12（要求
3.11+）、标准库 `sqlite3` 与 `numpy`，不需要 GPU、网络或外部服务。

本次实际验证环境：Python `3.12.13`、numpy `2.5.5`、SQLite `3.53.1`。

核心目标不是追求在线吞吐，而是把以下决策做成可审计、可重放、逐位可复现：

- 逻辑时钟驱动的副本批处理窗口；
- 算力/显存/批大小/序列长度约束下的批组成；
- 优先级插队及被跳过请求、首个约束的记录；
- 浮点估算到定点量的换算与误差上界传播；
- 精度敏感标记及误差来源链；
- SQLite 两阶段窗口提交和强制终止后的断点续算；
- 副本规格版本整体替换、并发幂等与状态账本校验。

## 快速开始

```bash
uv venv --python python3.12 .venv
uv pip install --python .venv/bin/python numpy

.venv/bin/python -m schedkernel load
.venv/bin/python -m schedkernel dispatch
.venv/bin/python -m schedkernel status --details --out artifacts/status.json
.venv/bin/python -m schedkernel verify --out artifacts/verify.json
```

默认路径：

- SQLite：`state/scheduler.db`
- 文件锁：`state/scheduler.lock`
- 临时目录：`.tmp/`
- 样本：`samples/model.json`、`samples/replicas.json`、
  `samples/requests.json`、`samples/precision.json`

所有命令都支持 `--db`、`--lock`、`--tmp`。`status` 使用
`json.dumps(sort_keys=True)` 输出稳定排序 JSON。输出文件通过同目录临时文件、
`fsync` 和 `os.replace` 原子替换。

## 目录

```text
schedkernel/          核心包与 CLI
samples/              固定本地 JSON 样本
scripts/run_demo.py   完整 load/dispatch/status/verify 演示
scripts/crash_demo.py 可重复强制终止点演示
tests/                标准库 unittest 测试
state/                默认 SQLite 与锁文件目录
.tmp/                 临时目录
artifacts/demo/       实际运行输出
```

## 输入模型

### 模型

`model.json` 包含每层参数量、每 token 算力参数和显存参数。总参数量按层顺序
求和。单请求/单批估算公式如下：

```text
param_cost(req) =
    total_params * layer_param_units_per_token * (input_tokens + max_output_tokens)

batch_compute =
    batch_base_units
    + sum_in_aggregation_order(
        param_cost(req)
        + input_tokens * input_token_units
        + max_output_tokens * output_token_units
      )

weights_mib = total_params * bytes_per_param / 1048576

batch_memory_mib =
    weights_mib + activation_base_mib
    + sum_in_aggregation_order(
        request_base_mib
        + input_tokens * kv_input_mib_per_token
        + max_output_tokens * kv_output_mib_per_token
      )

window_remaining_ms = max(logical_clock_step_ms, window_end_ms - decision_clock_ms)
execution_ms =
    batch_compute / replica.available_compute_units * replica.batch_window_ms
    + fixed_queue_latency_ms
estimated_queue_ms = window_remaining_ms + execution_ms
```

输出 token 使用请求声明的最大输出量，因此显存和算力估算都是保守上界。

### 副本与请求

- 副本版本为正整数；`replica_id` 唯一；算力为有限正数；显存上限、批窗口、
  最大批大小、最大序列长度为正整数。
- 请求 `request_seq` 与 `request_id` 分别唯一；到达/截止为非负整数；
  `priority` 为 `0..255`，数值越大优先级越高；截止不早于到达。
- 每个请求先按输入 token + 最大输出 token 过滤序列长度、单请求算力、
  单请求显存，得到候选副本。无候选返回 `E_REQ_NO_REPLICA`。
- 所有校验在任何持久化写入之前完成。校验失败不会插入请求、窗口、批次或
  副本状态。

## 确定性调度

### 窗口

请求按 `(arrival_ms, request_seq)` 纳入副本窗口。窗口起点：

```text
max(
  floor(arrival_ms / batch_window_ms) * batch_window_ms,
  persisted_window_watermark_ms
)
```

窗口不读取系统墙钟。窗口键为
`<replica_id>:v<version>:w<12位窗口起点>`。窗口关闭时，即使没有达到最大
批大小也提交。`windows`、`window_plan_requests`、`batch_requests` 的唯一
约束保证一个请求只能被一个窗口/批次占用。

### 副本选择

对每个到达请求，在候选副本上分别估算单请求探针批次的
`projected_queue_ms = 已规划队列估算 + 探针 estimated_queue_ms`。选择最小项，
差值小于 `float_tolerance` 时记录 `replica_choice` 精度敏感标记，并以
`replica_id` 稳定决胜。

### 批组成与优先级

同一窗口内请求按
`priority DESC, arrival_ms ASC, request_seq ASC` 排序。依次构造可行前缀，
约束检查顺序固定：

```text
MAX_BATCH_SIZE
SEQUENCE_LENGTH
COMPUTE_BUDGET
MEMORY_BUDGET
```

候选前缀效用为 `priority_sum / estimated_queue_ms`。选取效用最高前缀；若
前两名差值小于容差，记录 `batch_composition` 精度敏感标记，并使用定点
效用、批大小、请求序号签名稳定决胜。决策 JSON 持久化以下观测：

- 每请求 token、优先级、到达、截止；
- 副本算力/显存预算及批估算后的剩余量；
- 窗口起止、决策逻辑时钟和剩余逻辑时长；
- 固定聚合顺序与约束检查顺序；
- 被推迟请求及首个触发约束；
- 因高优先级后到而排到低优先级请求之前的 `priority_insertions`。

## 定点换算与误差谱

每个批估算最终分别换算为：

```json
{
  "fixed_scales": {
    "compute": 1000,
    "memory": 1000,
    "queue_ms": 1000
  }
}
```

每个量记录：

- 换算前 float64 原始值、标度、定点整数；
- 舍入模式（样本为 `half_even`）与实际舍入方向
  （`up`/`down`/`none`）；
- 本次换算误差和累计绝对误差上界；
- 每次 `add`、`subtract`、`multiply`、`divide`、`fixed_convert` 的
  误差输入/输出、半步 ULP 舍入上界、请求序号与参数标签。

候选批的 `priority_sum / queue_ms` 内部比较量也以 `queue_ms` 标度换算并
纳入误差谱。

运算顺序固定为请求聚合顺序内的从左到右 float64 算术，不使用并行归约。
误差采用一阶绝对上界传播；乘法包含两个操作数误差项及交叉项。定点换算使用
`decimal.Decimal.from_float(raw)` 对原始二进制浮点值精确量化。

当两个候选批或两个副本的估算差小于 `float_tolerance` 时，持久化
`precision_flags`，包括差值、容差、选定项、两个候选估算和完整误差来源链。
`status --details` 与 `artifacts/demo/live/status.json` 可查看。

## 持久化与恢复

SQLite 表包括：

```text
meta, model_snapshot, precision_snapshot
replica_versions, replica_current, replica_specs, replica_state
requests, request_candidates, request_events
windows, window_plan_requests
batches, batch_requests
precision_flags
```

窗口采用两阶段提交：

1. `prepare_window` 在单个事务中写入窗口计划、计划请求预留和精度标记，
   状态为 `prepared`；不扣减额度。
2. `commit_window` 在单个事务中写批次、请求归属、状态事件、窗口水位、
   算力/显存占用、误差上界和逻辑时钟，状态改为 `committed`。

因此进程在计划持久化后退出时，`resume` 只提交已经持久化的计划，不重新
裁决；在提交事务中退出时，SQLite 回滚整个事务，窗口仍为 `prepared`，可安全
重放。`batch_requests.request_seq`、`window_plan_requests.request_seq` 和
批次/窗口主键提供数据库级幂等。

额度释放在请求终态后执行，并从仍活跃批次集合重新计算，而不是仅做浮点减法，
避免恢复路径留下尾差。

## 请求生命周期

```text
pending -> batched -> executing -> completed
                                  -> expired
```

- 窗口提交：`pending -> batched`，记录窗口、批次、副本、版本。
- 批次到期结算：`batched -> executing -> ...`，每个迁移写入
  `request_events`。
- 正常完成：`outcome=SUCCESS`，失败计数清零。
- 截止错过：`outcome=DEADLINE_MISSED`，失败计数不增加。
- 执行失败：`outcome=EXECUTION_FAILED`，`failure_count` 增加，错误文本持久化。

截止超期和执行失败使用不同 `outcome`，并都会释放批次额度。可先停在
“批次已提交、尚未执行结算”的终止点，再用 `fail` 演示执行失败：

```bash
python -m schedkernel dispatch --crash-at after_commit_before_settle
python -m schedkernel fail --request-id req-001 --attempts 3 \
  --error SIMULATED_EXECUTOR_CRASH
python -m schedkernel resume
```

同一请求序号在同一副本版本中不会二次入批；`failure_count` 记录本次终止前
观察到的连续失败尝试数，正常完成路径会清零。

## 副本版本

`replica_current` 是单行快照指针；所有旧版本参数仍保存在
`replica_versions/replica_specs`。加载新清单时：

- 内容完全相同：`no_op`；
- 旧副本参数均未变且只追加副本：`incremental_append`；
- 任一已有参数变化或删除：`full_replace`；
- 请求版本小于当前版本：`E_VERSION_ROLLBACK`；
- 版本号相同但内容变化：`E_VERSION_CONFLICT`。

窗口计划与批次行都保存 `replica_version`，计划中还固化 `replica_spec`。
中途终止窗口继续使用其开始时版本，不会被新快照混合估算。

## 并发

- 跨平台文件锁：POSIX `fcntl.flock`，Windows `msvcrt.locking`。
- SQLite `BEGIN IMMEDIATE`、WAL、`synchronous=FULL`、30 秒 busy timeout。
- `status`/`verify` 使用只读事务快照，避免并发换版时跨查询观察到混合参数。
- 唯一约束保证同一窗口/版本和请求序号只入批一次。
- 重复提交同一窗口计划是幂等的；不同计划哈希返回 `E_WINDOW_CONFLICT`。

## 终止点演示

`scripts/crash_demo.py` 会在 `artifacts/demo` 下创建独立运行目录，强制退出码
固定为 `137`，再执行 `resume` 和 `verify`：

```bash
.venv/bin/python scripts/crash_demo.py --point after_prepare_windows
.venv/bin/python scripts/crash_demo.py --point inside_window_commit
.venv/bin/python scripts/crash_demo.py --point after_window_commit
.venv/bin/python scripts/crash_demo.py --point after_commit_before_settle
```

## 实际验证

测试命令：

```bash
.venv/bin/python -m unittest discover -v
```

结果：18 项测试全部通过，包括重复运行逐位一致、双预算约束、并发四进程、
提交事务中断回滚、提交后额度恢复、旧版本窗口绑定、版本回退拒绝、超期与执行
失败区分、失败计数演进、重复窗口提交幂等、非十进制定点标度换算及原子输出/
文件锁。

完整演示的确定性结果：

```text
18 requests -> 10 windows -> 14 batches
17 completed, 1 expired
6 precision-sensitive flags
verify ok, 18 unique request assignments
logical clock watermark 42.68275952 ms
```

代表批次 `r0:v1:w000000000000#b0001`：

```text
requests: req-018, req-017, req-015
first constraint: MAX_BATCH_SIZE
deferred: req-016 (MAX_BATCH_SIZE)
priority insertion: req-018 (arrival 6) ahead of req-017/req-015
compute: raw=115.270848 fixed=115271 scale=1000 direction=up
verify: ok
```

强制终止于 `after_window_commit` 时：

```text
crash exit code: 137
before resume: 1 committed window, 2 batches, 9 prepared windows,
               compute_in_use = [154.094464, 0, 0], verify ok
after resume:  10 committed windows, 14 batches, 18 assignments,
               compute_in_use = [0, 0, 0], verify ok
```

强制终止于 `inside_window_commit` 时，提交事务整体回滚：

```text
before resume: 10 prepared windows, 0 batches, 18 pending, verify ok
after resume:  10 committed windows, 14 batches, 17 completed + 1 expired, verify ok
```

详细输出位于：

- `artifacts/test-output.txt`
- `artifacts/demo/run-output.txt`
- `artifacts/demo/live/status.json`
- `artifacts/demo/live/verify.json`
- `artifacts/demo/crash-after-window-commit.json`
- `artifacts/demo/crash-inside-window-commit.json`
