# 本地列式聚合与滑动窗口引擎

这是一个从零实现的本地确定性执行引擎，用于复现列式批次在“并行分片调度 + 每任务内存预算”约束下的完整链路：输入校验、确定性切分、NumPy 向量化执行、分组聚合、跨分片滑动窗口、定点/浮点双通路误差诊断、SQLite 事务持久化、强制终止恢复、并发幂等、自适应分片粒度，以及 `run` / `resume` / `status` / `verify` 命令行。

## 快速开始

当前工作区已创建带 pip 的 Python 3.12.16 虚拟环境 `.venv`，并通过 `python -m pip` 安装 NumPy 2.5.5。重新搭建：

```bash
./scripts/bootstrap.sh
source .venv/bin/activate
python tools/generate_samples.py
python -m pytest -q
```

执行完整本地实验：

```bash
python -m fieldengine run --experiment samples/basic/experiment.json --db state/basic.sqlite3
python -m fieldengine status --db state/basic.sqlite3
python -m fieldengine verify --experiment samples/basic/experiment.json --db state/basic.sqlite3
```

一次命令复现全部验收场景并写 `artifacts/acceptance-report.json`：

```bash
python tools/run_acceptance.py
```

若系统提供 `make`，也可使用 `make test`、`make acceptance`、`make demo`。

## 目录

```text
fieldengine/            引擎实现
  model.py              输入解析与业务校验
  exact.py              精确二进制浮点累加器
  compute.py            过滤、分组、定点换算、窗口映射、误差诊断
  planning.py           确定性分片、预算估算、自适应模式、窗口计划
  execution.py          分片执行、状态合并、单流真值、结果比较
  store.py              SQLite 状态、文件锁、租约、事务提交
  scheduler.py          并行调度、注入动作、恢复驱动
  engine.py             run/resume/status/verify 门面
  cli.py                可重复命令入口
samples/                本地确定性 JSON 样本
tools/generate_samples.py
tools/run_acceptance.py
tests/                  24 项自动化测试
artifacts/acceptance-report.json
```

## 样本格式

`experiment.json` 只引用本地文件，路径相对该文件：

```json
{
  "version": 1,
  "batch_files": ["batch_0000.json", "batch_0001.json"],
  "query_file": "query.json",
  "config_file": "config.json",
  "injection_file": "injections.json"
}
```

批次文件：

```json
{
  "batch_index": 0,
  "row_count": 2,
  "columns": {
    "region_id": {"type": "int", "values": [0, 1]},
    "amount": {"type": "float", "values": [1.5, 2.5]},
    "active": {"type": "bool", "values": [true, true]}
  },
  "content_digest": {
    "algorithm": "sha256",
    "value": "批次规范化 JSON 的 SHA-256"
  }
}
```

内容摘要按 `batch_index`、`row_count`、按列名排序的 `type/values` 规范化 JSON 计算；摘要只覆盖列内容，不依赖文件空白或键顺序。JSON 对象重复键会被拒绝，浮点列允许解析器接受的 `NaN`、`Infinity`、`-Infinity`。

查询文件支持：

- `group_keys`：分组列，可为空表示全局单组。
- `aggregates`：`count`、`sum`、`mean`、`var`，名称唯一。
- `filters`：全部 AND；支持 `eq/ne/lt/le/gt/ge/in/is_nan/is_finite`。
- `window`：`length`、`step`、可选 `partition_by` 和窗口聚合；只输出完整窗口。
- 方差为总体方差（`ddof=0`）；`count` 统计过滤后行数并包含 NaN/无穷。

执行配置：

```json
{
  "parallel_limit": 3,
  "per_task_memory_mib": 16,
  "total_memory_budget_mib": 48,
  "fixed_scale": 4,
  "tolerance": 1e-6,
  "logical_clock_step": 3,
  "shards_per_batch": 3,
  "adaptive": {
    "row_threshold": 1000,
    "group_cardinality_threshold": 100,
    "coarse_divisor": 2
  }
}
```

`fixed_scale` 合法范围为 `0..18`；`total_memory_budget_mib` 可省略，默认等于 `parallel_limit * per_task_memory_mib`。窗口要求 `length > 0`、`0 < step <= length`。

注入清单：

```json
{
  "version": 1,
  "injections": [
    {
      "id": "kill-after-two",
      "action": "kill",
      "trigger": {"after_committed_shards": 2},
      "once": true
    },
    {
      "id": "fail-one",
      "action": "fail",
      "trigger": {"shard_id": "b000000-s0000"},
      "error_code": "E_TEST_FAILURE",
      "once": true
    }
  ]
}
```

触发器只能含一个选择器：`shard_id`、`shard_ordinal` 或 `after_committed_shards`。`after_committed_shards` 必须一次性触发且只能用于 `kill/delay`。`kill` 使用真实 `SIGKILL`，不是线程超时或伪状态。

## 业务错误码

所有输入错误在创建 SQLite 前返回 JSON：

```json
{"ok":false,"error":{"code":"E_CONFIG_CLOCK","message":"...","details":{}}}
```

主要错误码：

- 输入：`E_FILE_NOT_FOUND`、`E_INVALID_JSON`、`E_DUPLICATE_JSON_KEY`、`E_BATCH_SEQUENCE`、`E_BATCH_SCHEMA`、`E_COLUMN_TYPE`、`E_COLUMN_LENGTH`、`E_ROW_COUNT`、`E_BATCH_DIGEST`。
- 查询：`E_QUERY_REFERENCE`、`E_QUERY_TYPE`、`E_QUERY_AGGREGATE`、`E_QUERY_FILTER`、`E_WINDOW_ARGUMENT`。
- 配置：`E_CONFIG_PARALLELISM`、`E_CONFIG_MEMORY`、`E_CONFIG_FIXED_SCALE`、`E_CONFIG_TOLERANCE`、`E_CONFIG_CLOCK`、`E_CONFIG_SHARDS`、`E_CONFIG_ADAPTIVE`。
- 状态/运行：`E_STATE_NOT_FOUND`、`E_STATE_MISMATCH`、`E_RUN_EXISTS_INCOMPLETE`、`E_RUN_INCOMPLETE`、`E_RUN_ABANDONED`、`E_SHARD_FAILED`、`E_IDEMPOTENCY_CONFLICT`、`E_LEASE_CONFLICT`、`E_BUDGET_REJECTED`、`E_VERIFY_FAILED`。
- 诊断：`E_FIXED_POINT_OVERFLOW` 在结果中以诊断字段出现，不与 NaN/无穷异常混同。

## 确定性与精确聚合

浮点聚合没有使用完成顺序相关的朴素累加。每个有限 IEEE-754 double 被拆为：

```text
sign * mantissa * 2^exponent
```

分片内用 NumPy 的 `frexp/ldexp`、整块 limb 分解和 `reduceat` 形成按“分组 + 二进制指数”的精确整数桶；分片间只合并整数桶。最终对精确有理数做一次正确舍入转回 `float64`。因此：

- 分片完成顺序不影响结果；
- 细粒度与合并粒度分片得到逐位一致结果；
- 与单流全量重算的 `value_bits` 逐位一致；
- 平方和同样用 limb 卷积精确累计，方差不会因分片合并产生漂移。

`compute_truth()` 在单流全量数据上重新执行同一确定性规约，`verify` 对比真实值位模式、计数、异常谱、定点值位模式和精度敏感证据。差异输出首个分组或窗口、指标及来源分片。

过滤器、投影、分组 ID、窗口行对映射和异常统计均使用 NumPy 整块数组操作。Python 循环只用于唯一分组/窗口或二进制桶的合并，不逐行执行算子。

### NaN / 无穷 / 定点溢出

- `count` 包含 NaN、正负无穷。
- `sum/mean` 按 IEEE 规则传播：任意 NaN 或同时出现正负无穷时结果为 NaN；单侧无穷传播为对应无穷。
- `var` 遇到任何非有限输入为 NaN。
- 上述输入计入 `error_spectrum.nan/posinf/neginf`。
- 定点通路把有限值乘 `10^scale` 后按 `nearest_ties_to_even` 转为 int64 单位；越界值计入 `fixed_overflow`，定点结果标记不可用并给出 `E_FIXED_POINT_OVERFLOW`，浮点通路仍正常完成。
- 每个结果记录 `scale`、`rounding_direction`、转换步骤、定点分子/分母和保守误差上界。

### 精度敏感

对同名浮点指标，若两个分组或两个窗口的有限结果满足：

```text
0 < |a - b| < tolerance
```

则“两者可区分”这一结论依赖小于容差的差值，双方被标记 `precision_sensitive=true`，并写入确定排序的证据链：参与分片、批次、浮点精确通路、定点标度/舍入步骤、双方值位模式。证据不依赖分片完成顺序。

## 分片、窗口与自适应

- 每个批次按行切分；边界为整数均分，摘要由批次序号、行数、分片索引/数量和配置决定，不含数据内容。
- `shard_digest` 是描述符摘要；`content_digest` 另外绑定批次内容摘要、行区间和过滤后行数。
- 同一输入重复规划得到逐位一致的分片清单（测试覆盖）。
- 窗口仅在其全部分区行覆盖完成后产出；部分状态持久化在 `window_buffer`，每项记录窗口、分片、覆盖行数、部分摘要和 blob。
- 自适应规则：任一批次 `row_count > row_threshold` 或分组基数 `> group_cardinality_threshold` 时选 `coarse`，否则 `fine`。`coarse` 使用 `ceil(shards_per_batch/coarse_divisor)`。输出记录实际模式、触发批次、阈值、被否决模式及原因。精确累加保证粒度切换不改变结果。

分片预算估算写入 `shard.estimate_json` 并在 `status` 中返回：

```text
base(1 MiB)
+ referenced column bytes (按列类型和行数)
+ filter mask
+ projection
+ group state (分片分组基数)
+ exact accumulator workspace (行数 × 指标数)
+ fixed-point workspace
+ window buffer (过滤行数 × ceil(length/step) × 窗口指标数)
+ window accumulator workspace
+ partial serialization
```

估算明确依赖列类型、行数、分组基数和窗口缓冲深度。

## 调度、预算与结局

调度在 SQLite 中写租约，外层使用 `flock` 文件锁，关键操作为 `BEGIN IMMEDIATE` 事务：

- 全局活跃租约数不超过 `parallel_limit`。
- 每个分片估算必须不超过 `per_task_memory_mib`。
- 所有活跃租约估算之和不超过 `total_memory_budget_mib`。
- 入队顺序按估算内存降序、分片 ID 升序，结果仍由精确状态合并保证顺序无关。
- 启动前超预算：不创建租约，分片置 `ABANDONED`，写 `ABANDON` 事件（0 字节占用）和原因，不写部分结果。
- 已启动任务：线程池不使用超时，必须完成或显式失败；失败置 `FAILED` 并释放租约。
- 尚未调度但预算内：保持 `PENDING`。
- 有失败/放弃且无待处理分片时，运行置 `INCOMPLETE`，不生成最终结果。

`status` 使用只读 SQLite 连接，输出稳定排序 JSON，含分片水位、尝试/中断次数、租约存活、预算事件、算子状态、窗口缓冲和结果摘要。

## 持久化与恢复

SQLite 表保存：

- `run`：状态、内容/查询/配置/计划摘要、逻辑时钟、模式决策。
- `shard`：边界、描述符摘要、内容摘要、预算估算、部分结果、提交摘要。
- `lease`：分片、owner（主机/pid/进程启动 ticks/uuid）、内存预留。
- `operator` / `watermark`：半提交算子状态与批次水位。
- `window_buffer`：跨分片窗口部分状态。
- `result`：最终分组/窗口结果、摘要、来源。
- `injection_applied`、`budget_event`：注入与预算审计。

分片提交在同一事务内写结果、内容摘要、水位、逻辑时钟、算子计数并释放租约。重复提交相同摘要直接幂等返回；不同摘要返回 `E_IDEMPOTENCY_CONFLICT`。最终化在同一事务写全部结果并置算子/运行 `COMMITTED/COMPLETE`。

`resume` 会检查 owner 进程是否仍存活；死进程留下的 `RUNNING` 分片回到 `PENDING` 并增加 `interrupted_attempts`，只重放未提交分片。已提交分片结果不重新累加。注入已应用标记与提交同事务，恢复不会再次触发同一 kill。

## 命令

```bash
# 新运行；非法输入不会创建 DB
python -m fieldengine run --experiment samples/basic/experiment.json --db state/basic.sqlite3

# 强制终止后恢复
python -m fieldengine resume --experiment samples/crash_running/experiment.json --db state/crash.sqlite3

# 只读稳定 JSON 状态
python -m fieldengine status --db state/crash.sqlite3

# 重算全部提交分片与单流真值并比较
python -m fieldengine verify --experiment samples/crash_running/experiment.json --db state/crash.sqlite3
```

退出码：`0` 成功；`2` 输入/业务错误；`4` 运行不完整；`5` verify 失败；被 `SIGKILL` 的进程由操作系统返回 `137`（Python 子进程视角为 `-9`）。

## 已执行验证

工作区实际执行结果见 `artifacts/acceptance-report.json`。摘要：

- Python 3.12.16、NumPy 2.5.5、SQLite 3.53.1。
- 自动化测试：`24 passed`。
- `samples/basic`：12 个分片全部提交，4 个分组 + 8 个完整窗口，verify 真值逐位一致。
- `samples/anomalies`：NaN/正无穷/负无穷与定点溢出均可区分，`fixed_overflow_observations > 0`，verify 通过。
- `samples/precision`：2 个分组被标记精度敏感，证据含分片、批次和换算步骤，正反分片合并顺序结果一致。
- `samples/budget`：12 个分片在获取租约前全部 `ABANDONED`，活动预算 0、结果表空，运行 `INCOMPLETE`。
- `samples/crash_running`：首进程真实 `SIGKILL`，留下已提交分片与一个死租约 `RUNNING`；`resume` 回收后全部提交并 verify 通过，`interrupted_attempts=1`。
- `crash` 样本覆盖“提交后 kill”并验证重复 `resume/run` 不推进逻辑时钟、不重复累加。
- 双进程同时 `resume` 同一状态库通过测试：租约/文件锁分摊分片，最终仅一份 `COMPLETE` 结果且 verify 通过。

## 设计边界

- 仅处理本地 JSON 和标准库 SQLite，不访问网络、不使用容器。
- 列类型限定为 int64、float64、bool；分组键可为这些类型。
- 定点值域按 int64 单位检查，溢出作为可解释诊断而非进程异常。
- 滑动窗口按过滤后、分区内的行序列计算；只输出完整窗口。
- 浮点精确累加针对 IEEE-754 float64；整数列进入浮点通路时先按 float64 表示，定点通路保留整数换算的精确语义。
