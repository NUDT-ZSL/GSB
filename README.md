# 本地推理网关配额令牌账本

这是一个从零实现的确定性本地推理网关裁决组件。它解决的不是副本装箱，而是：

- 在**全局**与**每服务**双层令牌桶约束下，确定性选择已就绪副本；
- 用事件化账本裁决每次分发、确认、失败、超时与死信；
- 在 SQLite 单指针快照、文件锁和多进程并发下保持令牌不超卖、代数不重复、墓碑只写一次；
- 在进程被强制终止后，从最后一个完整提交的账本恢复，并区分已提交分发与未完成尝试。

所有业务时间都来自注入的 `clock_ms`；令牌补充、退避、抖动、超时与预算计算不读取系统墙钟。系统的墙钟只用于 SQLite 审计字段 `created_at`。

## 目录

```text
gateway/               实现包（CLI、校验、引擎、定点账本、SQLite、验证）
samples/               本地 JSON 样本与逻辑时钟步进表
tests/                 单元、崩溃恢复、多进程并发和 CLI 验证
scripts/validate.py    可重复验证脚本
docs/DESIGN.md         设计、事务协议、崩溃恢复与不变量说明
artifacts/             实际验证报告与样本 status
README.md
```

## 环境

当前容器实际环境：

```text
Python 3.10.12
NumPy 1.26.4
SQLite 3.37.2
```

实现只使用 Python 标准库（`sqlite3`、`fcntl`/`msvcrt`、`json`、`decimal`），因此可直接运行在 Python 3.11。NumPy 已按要求安装并确认版本，但账本不使用浮点数组，令牌与预算计算全部使用整数和 `Decimal` 精确运算。

Windows 下文件锁自动切换到 `msvcrt.locking`；Linux/POSIX 下使用 `fcntl.flock`。

## 快速开始

```bash
# 初始化账本
python -m gateway load \
  --db artifacts/sample.db \
  --services samples/services.json \
  --requests samples/requests.json \
  --config samples/config.json \
  --clock-steps samples/clock_steps.json

# 按逻辑时钟步进表执行分发与确认
python -m gateway dispatch --db artifacts/sample.db --plan samples/clock_steps.json

# 稳定排序 JSON 状态
python -m gateway status --db artifacts/sample.db --pretty

# 重放事件日志，逐版本校验持久化快照
python -m gateway verify --db artifacts/sample.db --pretty

# 再次执行同一计划：全部 round 幂等，账本版本不增长
python -m gateway dispatch --db artifacts/sample.db --plan samples/clock_steps.json
```

运行全部测试：

```bash
python -m unittest discover -s tests -v
```

运行样本验证并生成报告：

```bash
python scripts/validate.py
```

## 输入格式

服务清单：

```json
{
  "schema_version": 1,
  "services": [{
    "name": "alpha",
    "model_version": "alpha-1.0",
    "batch_window_ms": 2,
    "replicas": [
      {"replica_id": "alpha-a", "slots": 2, "compute_units": 2.0},
      {"replica_id": "alpha-b", "slots": 1, "compute_units": 1.0}
    ]
  }]
}
```

请求轨迹：

```json
{
  "schema_version": 1,
  "requests": [{
    "request_id": 1,
    "service": "alpha",
    "arrival_ms": 0,
    "input_tokens": 10,
    "output_tokens": 20,
    "priority": 4
  }]
}
```

网关配置：

```json
{
  "schema_version": 1,
  "config_version": 3,
  "global": {
    "capacity": 8,
    "refill_rate_per_second": 4,
    "max_inflight": 8
  },
  "service": {
    "capacity": 4,
    "refill_rate_per_second": 2,
    "max_inflight": 3
  },
  "service_overrides": {
    "beta": {
      "capacity": 2,
      "refill_rate_per_second": 1,
      "max_inflight": 2
    }
  },
  "round_budget_ms": 6,
  "dispatch_timeout_ms": 3,
  "backoff": {"base_ms": 2, "cap_ms": 8, "max_generation": 3},
  "fixed_point": {"scale": 1000, "tolerance_ms": 1},
  "scheduling": {"priority_min": 0, "priority_max": 5}
}
```

逻辑时钟步进表每个 step 是一轮原子事务：

```json
{
  "schema_version": 1,
  "steps": [{
    "round_id": "s3-confirm-and-dispatch",
    "clock_ms": 3,
    "budget_ms": 6,
    "outcomes": [
      {
        "request_id": 3,
        "generation": 1,
        "replica_id": "beta-a",
        "outcome": "success"
      }
    ],
    "abort_overdue": false
  }]
}
```

`round_id` 是命令幂等键。同一 `round_id` 重复提交相同命令返回原结果；相同 `round_id` 提交不同命令返回 `E_IDEMPOTENCY_CONFLICT`。

## 核心裁决语义

### 定点令牌与余量

- 1 个分发令牌价值 `scale` 个定点单位；一次分发在全局桶和所属服务桶各扣 `scale`。
- `refill_rate_per_second` 的单位是“整令牌/1000 逻辑毫秒”。
- 逻辑时钟推进 `dt` 毫秒时：

```text
numerator = rate * scale * dt + previous_remainder
added     = numerator // 1000
remainder = numerator % 1000
balance   = min(capacity * scale, balance + added)
```

- `remainder` 严格小于一个定点单位，逐层保留并在下次补充参与计算；到达容量上限的溢出单独记录为 `overflow_discarded_fixed`。
- status 中同时输出 `balance_fixed`、`balance`、`rounding_direction`、`refill_remainder`、`last_refill_fraction`。

### 分发轮次

- 请求在 `arrival_ms + 服务批处理窗口` 后进入可分发集合。
- 按服务分组，组内按 `(priority 降序, arrival_ms, request_id)` 排序；组间轮转。多进程同时写入时由文件锁和 SQLite 单写事务串行化，得到等价的确定性裁决。
- 每轮受以下约束：
  - 每轮逻辑时间预算；每次启动消耗 `ceil(batch_window_ms / replica.compute_units)` 毫秒；
  - 全局令牌余额、服务令牌余额；
  - 全局/服务 `max_inflight`；
  - 每副本 `slots`，只统计 `inflight` 尝试。
- 预算不足时立即停止本轮，未启动请求保留队列并标记 `budget_exhausted`。副本/服务/全局并发和令牌不足会逐请求或逐服务隔离为 `not_started`。
- 副本选择不是装箱优化：优先选择当前占用槽位最少者，再按算力降序、副本 ID 升序确定性决胜。

### 代数、确认与超时

- 每次成功启动分发，请求 `generation += 1`，记录 `attempt_id`、副本 ID、逻辑分发时刻、截止时刻、预算消耗和双层令牌扣减。
- 确认键是 `(request_id, generation, replica_id)`。相同结果重复确认幂等；迟到确认保留终态、不改代数；跨副本确认返回 `E_CONFIRM_REPLICA_MISMATCH` 且不改变账本。
- 已确认、已死信和仍处 `inflight` 的请求不会再次进入待分发队列。
- 超时必须同时满足“逻辑时钟超过 deadline”和“显式主动中止”（`abort_overdue` 或 `expired`/超期 `abort` 结果）。仅推进时钟不会把已启动分发事后标成超时；deadline 之前的 `expired` 结果返回 `E_TIMEOUT_NOT_DUE`。

### 失败、退避与死信

- `failure`、`rejected`、`expired` 会进入终态并释放副本槽位。
- 未达 `max_generation` 时：

```text
exponential = min(cap_ms, base_ms * 2^(generation-1))
jitter      = sha256(request_id:generation:replica_id:clock_ms) % base_ms
delay       = min(cap_ms, exponential + jitter)
next_eligible_ms = clock_ms + delay
```

- 退避期间只推进逻辑时间，不扣令牌。
- 达到代数上限后写入唯一墓碑，触发原因保留为 `failure`、`rejected`、`expired` 或显式 `abandoned`。

## 持久化与恢复

SQLite 中包含：

- `snapshots`：每个账本版本的完整状态 JSON、哈希和父版本；
- `pointer`：唯一当前版本指针；
- `events`：从 genesis 开始的可重放命令日志；
- `inputs`：初始化输入的规范化 JSON 与 SHA-256。

每轮在文件锁内执行 `BEGIN IMMEDIATE`，读取当前指针，应用命令，插入新快照，以版本 CAS 原子替换指针并追加事件，最后提交。读方每次只通过 `pointer JOIN snapshots` 读取一个完整已提交版本。

崩溃故障点：

- `after_mutation`：内存状态已改，尚未插入快照；
- `after_snapshot_insert`：快照行已插入，指针尚未替换；
- `before_commit`：事务尚未提交；
- `after_commit`：事务已提交，但进程在返回前被终止。

前三类由 SQLite 原子回滚；第四类会留下已提交的 `inflight` 尝试。`resume` 通过 `recovery.inflight_detected` 识别它，不会重复扣令牌或重复递增代数；只有显式 `--abort-overdue` 且逻辑时钟已越过 deadline 时才会将其转为过期并决定重试或死信。

`verify` 从 genesis 输入重建状态，逐条重放事件日志，并逐版本比较快照哈希，最后按字段比较当前持久化状态，任何差异返回 `E_VERIFY_MISMATCH`。

## 错误码

| 错误码 | 场景 |
|---|---|
| `E_JSON_INVALID` / `E_JSON_DUPLICATE_KEY` | JSON 非法或对象键重复 |
| `E_INPUT_SCHEMA` / `E_INPUT_SCHEMA_VERSION` / `E_INPUT_IO` | 结构、schema_version 或文件读取错误 |
| `E_DUPLICATE_SERVICE` | 服务名重复 |
| `E_DUPLICATE_REQUEST` | 请求序号重复 |
| `E_DUPLICATE_REPLICA` | 服务内副本 ID 重复 |
| `E_DUPLICATE_ROUND_ID` | 步进表内 round_id 重复 |
| `E_INVALID_REQUEST_ID` | 请求序号非正整数 |
| `E_INVALID_REPLICAS` | 服务没有已就绪副本 |
| `E_INVALID_SLOTS` | 副本槽位数非正整数 |
| `E_INVALID_COMPUTE` | 副本算力非有限正数 |
| `E_INVALID_BATCH_WINDOW` | 批处理窗口非正整数 |
| `E_INVALID_ARRIVAL_CLOCK` | 到达逻辑时钟为负/非整数 |
| `E_INVALID_TOKEN_COUNT` | 输入或输出 token 非正整数 |
| `E_INVALID_TOKEN_CAPACITY` / `E_INVALID_TOKEN_RATE` | 双层令牌容量/补充速率非法 |
| `E_INVALID_BACKOFF` | 退避基数、上限或最大代数非法 |
| `E_INVALID_FIXED_POINT` | 定点标度或容差非法 |
| `E_REQUEST_PRIORITY` | 请求优先级非法 |
| `E_SERVICE_NOT_FOUND` | 请求或配置引用不存在服务 |
| `E_INVALID_CONFIG_VERSION` | 配置版本非正整数 |
| `E_INVALID_CONCURRENCY_LIMIT` | max_inflight 非正整数 |
| `E_CONFIG_VERSION_REGRESSION` | 令牌配置版本回退 |
| `E_CONFIG_VERSION_CONFLICT` | 同版本配置内容冲突 |
| `E_CONFIRM_NOT_FOUND` | 确认引用不存在的分发 |
| `E_CONFIRM_REPLICA_MISMATCH` | 跨副本确认 |
| `E_TIMEOUT_NOT_DUE` | 逻辑时钟尚未越过 deadline 却提交超时 |
| `E_REPLICA_NOT_FOUND` | 指定分发副本不存在（逐请求隔离） |
| `E_REPLICA_SLOTS_FULL` | 指定/候选副本槽位已满（逐请求隔离） |
| `E_TOKEN_EXHAUSTED` | 全局或服务令牌余额不足（逐请求/逐服务隔离） |
| `E_CLOCK_REGRESSION` | 逻辑时钟回拨超过容差 |
| `E_IDEMPOTENCY_CONFLICT` | 同 round_id 命令内容不同 |
| `E_INVALID_COMMAND` | CLI/步进命令字段非法 |
| `E_ALREADY_LOADED` | 已初始化账本尝试换用另一套输入 |
| `E_NOT_LOADED` / `E_DB_CORRUPT` | 未初始化或持久化损坏 |

逐请求或逐服务错误保留在 round 结果的 `not_started` 中，并带有 `request_id`、`service`、`replica_id`、`generation`、`error_code` 等诊断字段；其余请求仍可在同一事务中继续裁决。

## 已验证结果

`python -m unittest discover -s tests` 当前 **40 项测试全部通过**。覆盖：

- 输入唯一性、整数/有限正数、优先级、服务引用、配置版本与“非法输入不落盘”；
- 定点余量逐次进位、容量截断和溢出审计；
- 预算截停、优先级、副本槽位、强制副本错误隔离；
- 代数演进、重复/迟到/跨副本确认幂等；
- 确定性退避、失败/拒收/过期死信、墓碑唯一；
- 超时需时钟推进加主动中止；
- `after_mutation`、`after_snapshot_insert`、`before_commit` 回滚，`after_commit` 恢复；
- 多进程并发槽位/令牌不超卖、重复 round_id 只分发一次、并发读只看到完整快照；
- 样本计划在两次全新数据库中生成逐位相同的状态哈希。

实际生成的报告见 `artifacts/validation_report.json`，样本最终状态见 `artifacts/sample_status.json`。

更细的事务协议、不变量和边界假设见 [docs/DESIGN.md](docs/DESIGN.md)。
