# 技术设计

## 1. 分层

```text
fixture.py      解析、规范化、完整校验（任何错误发生在持久化之前）
models.py      不可变领域对象
fixed.py        Decimal 定点换算与加法/乘法误差上界
planner.py      纯确定性路由、窗口、批组成和误差谱
db.py           SQLite schema、文件锁、事务提交、幂等约束
engine.py       推进逻辑时钟、注入点、执行状态机、status/verify
cli.py          六个验收子命令
```

规划器不访问 SQLite、系统时间或环境状态。它只依赖规范化夹具、副本状态
`available_ms` 和已路由请求，因此相同输入总产生相同计划及 `plan_hash`。

## 2. 请求路由与窗口切分

先按 `(arrival_ms, priority, request_id)` 处理请求。对每个支持序列长度且能容纳
单请求算力/显存的副本，计算：

```text
window = floor(arrival_ms / replica.window_ms)
start  = window * replica.window_ms
end    = start + replica.window_ms
score  = end + overhead + (existing_window_compute + request_compute) / capacity * 1000
```

选择最小 `(score, replica_id)`。选择后把请求算力加入该副本窗口负载。
前两名候选的原始分数差小于 `float_tolerance` 时，产生 `replica_choice` 精度事件，
事件含候选定点值、舍入方向、上界和来源链。

副本窗口是固定逻辑区间。请求只属于一个窗口；数据库层再用请求主键约束归属。

## 3. 批组成

计算基线：

```text
request_compute = (input + max_output) * total_params * flops_per_parameter_per_token / 1e9
request_memory  = (input + max_output) * kv_mib_per_token + input * activation_mib_per_token
batch_memory    = memory_base_mib + sum(request_memory)
duration_ms     = batch_overhead_ms + batch_compute / compute_capacity * 1000
```

副本序列长度约束校验 `input + max_output`。

窗口在 `end_ms <= injected_clock` 时关闭。批起点为：

```text
cursor = max(window.end_ms, replica_state.available_ms)
```

候选按 `(priority, arrival_ms, request_id)` 升序排列，优先级数字小者优先。
每轮先处理无法在 `cursor` 满足截止时间的请求，置为 `EXPIRED`。随后尝试将请求
加入当前批，按固定顺序检查：

1. `max_batch_size`
2. `compute_budget`
3. `memory_budget`
4. `deadline`

若加入造成约束失败，该请求写入 `skipped`，包含：

- `first_constraint`
- 违反项观测值与限额
- 试算完成时间
- 请求 token、算力/显存原始与定点观测
- 靠近边界时的精度事件

低优先级请求仍可进入当前批，形成显式“插队但不静默”的裁决证据。

## 4. 定点与误差账本

每批包含以下换算：

- 每请求算力：`compute_scale`
- 每请求 KV/激活显存：`memory_scale`
- 批次基础显存
- 批总算力加法
- 批总显存加法
- 算力到排队时长乘法
- 批 overhead 换算与加法
- 预计排队时长

`half_up` 使用 `Decimal` 四舍五入；`floor`/`ceil` 可选。每条记录包含原始浮点、
可逆文本、标度、定点整数、实际舍入方向、量化误差、误差上界与递归来源链。

误差传播：

```text
add: error = sum(input_error)
mul: error = |factor| * input_error + quantization_error
```

批总误差上界为算力、显存、时长、排队时长四类最终量上界之和，并与
`accuracy.error_budget` 比较。超预算产生 `error_budget_exceeded` 精度事件。

## 5. 事务边界

加载成功后原子替换完整初始化库。运行期事务：

1. `window_open`：`PLANNED -> OPEN`
2. `commit_expirations`：超期状态、生命周期、窗口决策
3. `commit_batch`：批、账本条目、请求归属、`PENDING -> BATCHED`、窗口决策
4. `commit_window`：窗口结论/哈希/误差谱、`replica_state`
5. `start_batch_execution`：批 `PLANNED -> RUNNING`，请求 `BATCHED -> RUNNING`
6. `finish_batch_execution`：批 `RUNNING -> FINISHED`，请求 `RUNNING -> COMPLETED`

每个批次是独立原子单元。SIGKILL 可能留下：

- `OPEN` 窗口、0 个或多个已提交批次：半提交窗口。
- `RUNNING` 批：执行中断，恢复时只补完成事务。
- `PLANNED` 批：提交后、执行前中断。

恢复时从 `replica_state` 得到该窗口之前已提交的可用时刻，重新生成完整窗口计划；
已存在批次必须与重放 `plan_hash` 一致，只提交缺口。

## 6. 并发与幂等

同一数据库的调度使用 `DB.lock` 排他 `flock`，SQLite 内部仍使用
`BEGIN IMMEDIATE`、唯一主键与预算复核。约束组合：

- `assignments.request_id PRIMARY KEY`：请求不能出现在两个批。
- `assignments(replica_id, window_index, version, batch_index, request_id)`：
  窗口内归属唯一。
- `windows`/`batches` 复合主键：窗口/批次版本唯一。
- `batches.plan_hash`：重复提交必须内容一致。
- 提交批前重新比较固定与原始算力/显存限额。

因此并发进程等价于按锁顺序串行执行，最终语义与单进程一致。

## 7. verify

`verify` 要求当前库 `complete=true`。它：

1. 从状态库读取规范化夹具快照。
2. 清除注入并在临时目录创建全新基线库。
3. 一次性执行基线。
4. 对窗口、批、裁决、误差谱、精度事件、请求、归属、窗口决策、生命周期逐字段比较。
5. 校验请求唯一归属、已超期不归属、预算、来源链无重复、批/窗口 `plan_hash`。

任何字段差异以 JSONPath 风格路径输出，退出码 `4`。

## 8. 设计取舍

- 仅标准库，固定版本 SQLite，不引入 ORM 或 Web 服务。
- 规划在内存中完整重算，但所有状态迁移以 SQLite 已提交事实为准；重算结果必须
  与已持久化 `plan_hash` 对齐，否则拒绝续算。
- 粗粒度进程锁换取清楚、可证明的一致性；SQLite 唯一约束仍提供第二道防线。
- `FAILED` 为独立状态，但本验收场景不主动制造执行失败；`SIGKILL` 不是请求失败。
