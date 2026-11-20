# 本地模型推理网关：请求路由与预算裁决引擎技术说明

## 总览

引擎在纯本地、确定性样本上复现：推理请求随到达规模变化在多个后端副本
之间路由，在延迟预算与并发预算约束下做算法取舍，并保证强制终止后的恢
复一致性。所有时间语义都来自**注入的逻辑时钟**（轨迹中的 `start_ms`/
`window_ms`），引擎与测试均不读取系统墙钟。

## 模块结构

| 模块 | 职责 |
| --- | --- |
| `gateway/loader.py` | 三份 JSON 样本解析与校验，非法输入抛 `GatewayError(E_xxx)`，校验先于任何持久化状态创建 |
| `gateway/clock.py` | 可注入逻辑时钟，回拨超阈值抛 `E_CLOCK_ROLLBACK` |
| `gateway/router.py` | 按规模的路由模式裁决（逐请求 vs 窗口批处理），输出决定链 |
| `gateway/placement.py` | 显存 + 并发实例 + 拓扑域约束下的副本落点（全有或全无） |
| `gateway/scheduler.py` | 轮内并行分发：轮次时间预算、窗口 token 预算、重试退避、三类非成功结局 |
| `gateway/upstream.py` | 确定性模拟上游 + 本地回环 HTTP 客户端，统一错误码映射 |
| `gateway/upstream_server.py` | 本地回环 HTTP 测试服务（127.0.0.1，无睡眠无随机） |
| `gateway/store.py` | SQLite 持久化：单事务轮次提交、快照指针、恢复清理、幂等插入 |
| `gateway/engine.py` | 编排：窗口推进、裁决、落点、分发、提交；崩溃钩子用于恢复测试 |
| `gateway/evaluate.py` | 多到达率采样点下两种模式的可比较指标与选择依据 |
| `gateway/cli.py` | `validate` / `run` / `evaluate` / `serve` 子命令 |

## 关键算法

### 1. 负载等级与路由模式裁决（需求 2）

对每个（服务， 窗口）：

```
request_count = floor(arrival_rate * window_ms / 1000)
tokens_per_request = int(avg_seq_len) + service.output_tokens
demand_tokens = request_count * tokens_per_request
load_ratio = demand_tokens / token_budget
load_ratio <  switch_load_threshold -> per_request（逐请求直接路由）
load_ratio >= switch_load_threshold -> window_batch（按 token 预算切批）
```

每次裁决输出决定链条目：实际模式、触发条件（`trigger`）、被否决模式
及原因（`rejected`）。全部算术是轨迹的纯函数，任意次数运行决定链逐位
一致（`tests/test_routing.py::test_decision_chain_bit_identical_across_runs`）。

### 2. 副本落点（需求 3）

- 副本数：`clamp(ceil(demand / (compute_units * window_ms)), min, max)`。
- 落点：节点按名称、卡按索引确定性首次适配；依次检查拓扑域
  （`E_PLACEMENT_DOMAIN`）、节点实例上限（`E_PLACEMENT_CONCURRENCY`）、
  卡剩余显存（`E_PLACEMENT_VRAM`）。
- **全有或全无**：任一副本落不下即回滚该服务本轮全部占用，不产生部分
  放置；落点失败的服务其请求记为 `not_started`。
- 每轮记录每卡剩余显存、被淘汰候选与首个触发约束；路由目标只取自本轮
  已提交落点；每请求 token 成本在窗口 token 预算内扣减。

### 3. 轮内并行分发与三类结局（需求 4）

同一轮次内按服务分组分发，受每轮总逻辑时间预算
（`round_time_budget_ms`）与每服务并发上限
（`per_service_concurrency_cap`，且不超过副本数）约束：

- `not_started`：轮次时间预算或落点失败导致请求从未启动；
- `failed_after_start`：请求已启动、上游随后失败（映射 `E_UPSTREAM_*`），
  已启动请求跑完所有重试，绝不被截断为超时；
- `abandoned`：窗口 token 预算耗尽，网关主动放弃剩余请求。

### 4. 批处理窗口与排队演进（需求 5）

批边界：`batch_id = (arrival_ms - window.start_ms) // batch_window_ms`，
批在 `(batch_id+1)*batch_window_ms` 关闭并分发；重试退避
`retry_backoff_base_ms * 2^(attempt-1)`；排队时长 = 分发时刻 - 到达时刻。
以上只依赖注入逻辑时钟。请求阶段在
`pending → queued → (batching) → dispatched / failed` 间迁移，每条的
`stage_path` 持久化在轮次快照中，重启后状态只来自 SQLite，不依赖内存残留。

### 5. 事务原子提交与恢复（需求 6）

每轮在**单个 SQLite 事务**（`BEGIN IMMEDIATE`）内写入：轮次水位
（`rounds`）、请求阶段（`requests`）、落点（`placements`）、窗口 token
消耗（`budgets`）、逻辑时钟水位（`clock`），并同事务整体替换
`meta.current_version` 快照指针。读取方只能看到 `committed = 1` 的完整
轮次。进程在轮次中途被强制终止（`--crash-after N` 以 `os._exit(3)` 模拟）
时事务整体回滚；恢复路径先清除超出已提交指针的半提交残留，再只重放未
提交轮次，已消耗预算不会被重复扣减。强制终止不承诺生成退出报告。

### 6. 多进程并发与幂等（需求 7）

多个网关实例可并行处理同一批请求：文件锁（`gateway/flock.py`，POSIX
`fcntl` / Windows `msvcrt`）保护 schema 初始化与提交临界区，SQLite
`BEGIN IMMEDIATE` 串行化写事务；`(version, request_id)`、
`(version, service, replica)`、`(version, service)` 主键保证同一请求同一
版本只路由一次、同一节点显存/并发不被并发超卖、同一窗口 token 预算不被
超扣；重复提交同一轮次幂等跳过（后到的实例直接复用已提交快照，输出逐位
一致）。

### 7. 算法取舍评估（需求 8）

`evaluate` 在到达率采样点（0.25/0.5/1/2/4 倍）上分别强制两种模式运行，
输出可比较指标：路由完成数与最后完成逻辑毫秒、副本利用率、排队时长
p50/p90/p99（numpy 分位数）、失败数；并给出模式选择依据
（`selected_mode` + `rationale`）。估计来源在报告 `sources` 中说明：
到达率/序列长度/并发/预算为轨迹注入的**真值**，完成数/排队/利用率/失败
为对模拟上游合约的**观测**。

## 业务错误码

| 代码 | 含义 |
| --- | --- |
| `E_SCHEMA_SERVICE` / `E_SCHEMA_NODE` / `E_SCHEMA_TRACE` | 样本字段非法 |
| `E_DUPLICATE_SERVICE` / `E_DUPLICATE_NODE` | 名称重复 |
| `E_UNKNOWN_SERVICE` | 轨迹引用未知服务 |
| `E_PLACEMENT_VRAM` / `E_PLACEMENT_CONCURRENCY` / `E_PLACEMENT_DOMAIN` | 落点约束（显存/实例上限/拓扑域） |
| `E_TOKEN_BUDGET` | 窗口 token 预算耗尽（主动放弃） |
| `E_ROUND_TIME_BUDGET` | 轮次总逻辑时间预算耗尽（未启动） |
| `E_UPSTREAM_CONN_REFUSED` | 上游副本连接拒绝 |
| `E_UPSTREAM_BUDGET` | 上游响应超出单请求预算 |
| `E_UPSTREAM_DIGEST` | 上游响应摘要不匹配 |
| `E_CLOCK_ROLLBACK` | 逻辑时钟回拨超阈值 |

所有错误以 JSON（含可诊断字段）输出到 stderr 或运行报告，进程退出码为
整数：0 成功，2 业务/校验错误，3 模拟强制终止，1 未预期错误。

## 使用

```bash
pip install numpy pytest
python -m gateway.cli validate --samples samples
python -m gateway.cli run --samples samples --db artifacts/gateway.db --out artifacts/runs/run.json
python -m gateway.cli evaluate --samples samples --workdir artifacts/eval --out artifacts/runs/eval.json
python -m gateway.cli serve --port 8377   # 本地回环上游（测试用）
python -m pytest tests/ -q
```

## 实际验证结果（本工作区）

- `python -m pytest tests/ -q`：**43 passed**。
- 模式切换：w0/w1 逐请求，w2/w5 窗口批处理，w3 chat 因 token 预算放弃
  14 请求（`E_TOKEN_BUDGET`），w4 持续连接拒绝产生 4 个
  `failed_after_start`（`E_UPSTREAM_CONN_REFUSED`）且同轮 chat/embed 全部
  成功，w5 轮次时间预算耗尽产生 10 个 `not_started`
  （`E_ROUND_TIME_BUDGET`）。
- 确定性：两次独立运行 `run1.json` 与 `run2.json` 逐位一致（`cmp` 通过）；
  同一数据库重复运行输出不变（幂等）。
- 中断恢复：`--crash-after 5` 以退出码 3 强制终止后重跑，恢复输出与干净
  运行逐位一致，预算未重复扣减。
- 多进程：两个进程并发写同一 SQLite，输出与单进程逐位一致，无显存/并发
  超卖，每轮仅提交一次。
- 评估：`eval.json` 在 0.25 倍采样点选择 `per_request`、0.5 倍及以上选择
  `window_batch`，两次评估输出逐位一致。
