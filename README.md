# 本地弹性伸缩控制面

这是一个从零实现的确定性本地控制面，用于复现多副本模型服务的伸缩决策链路：

`负载观测 -> 目标副本换算 -> 资源准入与装箱 -> 并行拉起/排空 -> 状态机演进 -> SQLite 原子持久化 -> 中断恢复`

实现不访问真实模型、GPU 或线上资源。运行时只使用本地 JSON、SQLite、Python 标准库，以及回环地址上的本地测试 HTTP 服务。

## 环境与快速验证

当前工作区已经准备好：

- CPython `3.11.15`
- `.venv` 虚拟环境
- SQLite `3.53.1`
- NumPy `2.4.6`

重新创建环境：

```bash
uv python install 3.11
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
source .venv/bin/activate
python --version
python -c 'import sqlite3, numpy; print(sqlite3.sqlite_version, numpy.__version__)'
```

运行全部测试：

```bash
.venv/bin/python -m unittest discover -v
.venv/bin/ruff check elastic_control tests
```

验证样本：

```bash
.venv/bin/python -m elastic_control validate \
  --services samples/services.json \
  --nodes samples/nodes.json \
  --trace samples/trace.json \
  --faults samples/faults.json
```

启动本地测试服务：

```bash
.venv/bin/python -m elastic_control.test_server \
  --host 127.0.0.1 \
  --port 18080 \
  --faults samples/faults.json
```

另一个终端运行完整 8 个逻辑窗口：

```bash
.venv/bin/python -m elastic_control run \
  --services samples/services.json \
  --nodes samples/nodes.json \
  --trace samples/trace.json \
  --policy samples/policy.json \
  --db artifacts/demo-state.db \
  --runtime-dir artifacts/runtime \
  --launcher-url http://127.0.0.1:18080 \
  --run-id local-demo \
  --out-dir artifacts/demo-out
```

输出包括：

- `artifacts/demo-out/rounds/*.json`：逐轮决策、动作、装箱候选、预算和阶段摘要
- `artifacts/demo-out/snapshot.json`：当前完整快照
- `artifacts/demo-out/summary.json`：运行摘要
- `artifacts/runtime/locks/`：轮次/服务文件锁
- `artifacts/demo-state.db`：SQLite 规范化状态和快照历史

重复执行相同命令会按轮次 ID 幂等返回已提交结果，不会重复拉起副本。

单轮执行与恢复：

```bash
.venv/bin/python -m elastic_control run-round \
  --services samples/services.json \
  --nodes samples/nodes.json \
  --trace samples/trace.json \
  --policy samples/policy.json \
  --db artifacts/demo-state.db \
  --runtime-dir artifacts/runtime \
  --launcher-url http://127.0.0.1:18080 \
  --round-id manual-round-0001 \
  --tick 2 \
  --budget 8 \
  --out artifacts/manual-round.json
```

若进程在拉起或回收中途被强制终止，对同一轮次、同一样本摘要和同一策略再次执行 `run-round`（或直接重跑 `run`）即可恢复。持久化为 `reserved/in_flight` 的动作会使用原幂等键重放，已提交动作会跳过。

查看状态：

```bash
.venv/bin/python -m elastic_control snapshot \
  --db artifacts/demo-state.db \
  --runtime-dir artifacts/runtime

.venv/bin/python -m elastic_control state \
  --db artifacts/demo-state.db \
  --runtime-dir artifacts/runtime \
  --out artifacts/demo-out/state.json
```

## 输入格式

### 服务清单

```json
{
  "services": [
    {
      "name": "chat-alpha",
      "model_version": "alpha-1.2.0",
      "memory_mib_per_replica": 6000,
      "compute_units_per_replica": 100,
      "min_replicas": 1,
      "max_replicas": 6,
      "cold_start_ticks": 2,
      "batch_window_ms": 20,
      "tokens_per_tick_per_replica": 1000,
      "max_concurrent_launches": 2,
      "allowed_topology_domains": ["rack-a", "rack-b"]
    }
  ]
}
```

必填字段均做严格类型与范围校验。`tokens_per_tick_per_replica` 未提供时，以 `compute_units_per_replica` 作为单副本吞吐能力，并在决策中记录 `throughput_source`。

### 节点资源池

```json
{
  "compute_units_capacity": 1000,
  "max_replicas_per_domain_per_service": 4,
  "nodes": [
    {
      "name": "gpu-a0",
      "gpu_count": 2,
      "memory_mib_per_gpu": 12000,
      "max_instances_per_node": 3,
      "topology_domain": "rack-a"
    }
  ]
}
```

`compute_units_capacity` 是全局算力预算；显式配置时，副本准入必须同时满足全局算力、单卡显存、节点并发实例和拓扑域约束。

### 负载轨迹

```json
{
  "trace_id": "deterministic-local-8-window",
  "windows": [
    {"tick": 2, "arrival_rate": 5.0, "avg_sequence_length": 1000.0}
  ]
}
```

窗口 `tick` 必须唯一且严格递增。到达率和平均序列长度必须是非负有限数。

### 策略与异常注入

`samples/policy.json` 控制退避、轮次逻辑时间预算和单动作 HTTP 超时。故障文件支持：

- `delay_ms`：响应延迟
- `mode: http_500` / `http_504`
- `mode: version_mismatch`
- `mode: digest_mismatch`
- `mode: remote_error`
- `mode: bad_json`
- `mode: connection_drop`
- `times`：规则生效次数
- `match`：按 `action`、`service_name`、`replica_id`、`tick` 等稳定字段匹配

测试服务仅允许绑定 `127.0.0.1`、`localhost` 或 `::1`。

## 决策语义

### 目标副本数

```text
required_tokens_per_tick = arrival_rate * avg_sequence_length
estimated = ceil(required_tokens_per_tick / tokens_per_tick_per_replica)
target = clamp(estimated, min_replicas, max_replicas)
```

计算使用 Python `Decimal` 和固定 50 位精度，避免二进制浮点差异。输出明确记录：

- 观测项：到达率、平均序列长度、单副本吞吐、批处理窗口
- 聚合顺序
- 唯一的向上取整位置
- 取整结果与 min/max 截断原因

### 装箱

副本只允许整块放入一张 GPU，绝不跨卡拆分。候选过滤顺序为：

1. 服务允许的拓扑域
2. 每服务每拓扑域副本上限
3. 节点并发实例上限
4. 单卡剩余显存

通过后按“域内副本数、节点名、GPU 序号”的稳定顺序选择落点。每轮输出每张卡的剩余 MiB、被淘汰节点及其 `first_constraint`。

### 拉起调度和预算

- 同一轮次内的拉起动作并发执行。
- 每服务最多拉起 `max_concurrent_launches` 个副本。
- 每个动作按冷启动逻辑耗时（或策略覆盖值）从轮次总预算中预留并只扣减一次。
- 预算一旦不足以覆盖当前优先级候选，后续候选统一截断，不允许低优先级候选插队。
- 已启动动作不会因轮次预算或总调度器超时被截断；单动作只受独立 HTTP 超时约束。

结局分类：

- `started`：动作已启动，随后成功或正在冷启动
- `launch_failed`：动作已启动但远程失败
- `budget_not_started`：按稳定优先级在预算处截断，未启动
- `abandoned`：因服务并发上限、显存、节点并发、算力或拓扑约束主动放弃

### 生命周期

```text
pending -> cold_starting -> ready -> draining -> reclaimed
```

- `cold_starting` 在 `ready_tick <= 当前逻辑时钟` 时转为 `ready`
- `draining` 持有显存和节点并发预算，但不计入可服务容量
- 排空失败保持 `draining`，后续轮次继续排空
- 拉起成功重置连续失败数；拉起失败递增失败数并计算下一次可重试时钟

退避公式：

```text
delay = min(cap,
            base * 2^(failure_count - 1)
            + floor(stable_fraction * (jitter_ticks + 1)))
```

`stable_fraction` 来自 `SHA-256(replica_id | failure_count | tick | policy_digest)`，因此跨进程和重复运行一致。

## 持久化与恢复

SQLite 使用 WAL、`BEGIN IMMEDIATE` 和 15 秒 busy timeout。主要表：

- `rounds`：轮次请求摘要、状态、预算、版本观察值和最终结果
- `replicas`：副本阶段、连续失败数、下次重试时钟
- `occupancies`：卡级显存、节点实例和全局算力占用
- `actions`：带唯一幂等键的拉起/排空动作
- `round_decisions`：包括未启动和主动放弃在内的全部决策
- `service_rounds`：每轮每服务的需求与规划上下文
- `transitions`：生命周期迁移审计
- `snapshots` + `state_meta`：不可变整轮快照与当前快照指针

规划事务同时写入副本、占用、动作、决策和预算预留。外部 HTTP 调用在事务之外执行，结果再以独立事务提交。

崩溃恢复规则：

- 规划事务未提交：没有任何残留，重新规划
- 动作已预留但未执行：按原幂等键执行
- 调用成功但结果未提交：按原幂等键重放，测试服务返回缓存结果
- 动作结果已提交：不重复执行
- 预算在预留阶段已记账：恢复不会重复扣减

每轮最终提交在一个事务内插入完整新快照并替换 `state_meta.current_snapshot_version`。若提交时发现版本已被其他控制面实例推进，结果会标记 `rebased=true`，但仍在同一事务中整体替换指针。

## 错误码

输入错误不会初始化 SQLite、租约目录或输出文件。

| 错误码 | 含义 |
| --- | --- |
| `E_INPUT_JSON` | JSON 无法解析或包含非有限数 |
| `E_INPUT_SCHEMA` | 顶层结构或字段类型错误 |
| `E_DUPLICATE_SERVICE` | 服务名重复 |
| `E_DUPLICATE_NODE` | 节点名重复 |
| `E_INVALID_MEMORY` | 每副本显存不是正整数 |
| `E_INVALID_COMPUTE` | 每副本算力不是正整数 |
| `E_INVALID_REPLICA_BOUNDS` | 最小/最大副本数非法 |
| `E_INVALID_COLD_START` | 冷启动耗时为负或非整数 |
| `E_INVALID_ARRIVAL_RATE` | 到达率非法 |
| `E_INVALID_SEQUENCE_LENGTH` | 平均序列长度非法 |
| `E_INVALID_TOPOLOGY` | 拓扑域或域上限非法 |
| `E_TRACE_WINDOW_MISSING` | 当前逻辑时钟没有观测窗口 |
| `E_CLOCK_REGRESSION` | 注入逻辑时钟回退 |
| `E_ROUND_CONFLICT` | 相同轮次 ID 使用了不同输入/策略 |
| `E_LAUNCH_CONNECTION_REFUSED` | 本地拉起目标拒绝连接 |
| `E_LAUNCH_TIMEOUT` | 单动作响应超出预算 |
| `E_REPLICA_VERSION_MISMATCH` | 回显模型版本不符 |
| `E_SAMPLE_DIGEST_MISMATCH` | 回显样本摘要不符 |
| `E_LAUNCH_HTTP_ERROR` | 远端返回 HTTP 错误 |
| `E_LAUNCH_REMOTE_ERROR` | 远端明确拒绝动作 |
| `E_LAUNCH_BAD_RESPONSE` | 响应 JSON 或回显字段非法 |
| `E_STATE_CORRUPT` | 持久化状态不满足约束 |

## 实际验证记录

在当前工作区执行：

```text
Python 3.11.15
SQLite 3.53.1
NumPy 2.4.6
ruff: All checks passed
unittest: Ran 30 tests ... OK
```

已实际覆盖：

- 确定性目标副本换算重复 100 次逐字段一致
- 完整 8 窗口样本运行，快照版本从 1 连续推进到 8
- 两类独立 CLI 运行生成 10 个 JSON 输出文件，逐字节一致
- 模拟 HTTP 500、版本不符、摘要不符、超时、拒绝连接的独立错误码
- 单服务失败不影响同轮其他服务提交
- 拉起成功落盘前强制 `os._exit(77)`，恢复后远端请求数仍为 1，幂等命中 1，预算只扣一次
- 排空中途强制退出，恢复后占用只释放一次
- 两个进程并行处理不同服务，节点卡级显存与节点并发均未超卖，快照版本连续推进
- 两个进程同时提交相同轮次，仅产生 1 个动作和 1 个快照，远端只拉起一次

更细的崩溃矩阵和并发不变量见 `docs/DESIGN.md`。

