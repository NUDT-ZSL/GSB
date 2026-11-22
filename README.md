# 本地消息投递网关

这是一个只依赖 Python 标准库与 SQLite 的确定性投递追平组件。它解决的核心问题是：在全局令牌桶、每消费者令牌桶、每轮逻辑时间预算和每投递逻辑超时预算的共同约束下，对积压消息执行可重放、可恢复、多进程安全的投递裁决。

所有业务时间都来自显式注入的毫秒值 `--now-ms`，进程不读取系统墙钟。消息由主题、分区键和全局种子确定生成；令牌账本、退避、抖动、投递轨迹和死信墓碑都可逐位复现。

## 目录

- `src/delivery_gateway/`：配置校验、确定性生成器、SQLite 状态机、调度器与 CLI。
- `fixtures/`：主题分区、消费者租约、配额、逻辑时钟和受控异常注入样本。
- `tests/test_gateway.py`：20 项单元/并发/强杀恢复测试。
- `scripts/run_validation.py`：可重复验收脚本，生成 `artifacts/validation.json`。
- `scripts/setup_tools.sh`：无 root、无容器的本地工具准备脚本。

## 环境准备

本工作区已准备：

- Python `3.11.18`：`.venv/bin/python`
- SQLite CLI `3.37.2`：`.tools/bin/sqlite3`
- 可创建的本地 SQLite 文件、WAL 文件、`-shm` 文件与 `.lock` 文件

重新准备环境：

```bash
./scripts/setup_tools.sh
```

脚本使用 `uv` 安装 Python 3.11，使用 `apt-get download` + `dpkg-deb -x` 把 SQLite CLI 解到项目内，不需要 `sudo`、Docker 或其他容器。

也可以直接使用已安装的可执行入口：

```bash
.venv/bin/delivery-gateway --help
# 或
PYTHONPATH=src .venv/bin/python -m delivery_gateway.cli --help
```

## 快速运行

```bash
mkdir -p artifacts/demo

.venv/bin/delivery-gateway load \
  --db artifacts/demo/state.db \
  --topics fixtures/topics.json \
  --consumers fixtures/consumers.json \
  --quota fixtures/quota.json \
  --clock fixtures/clock.json \
  --faults fixtures/faults.json

.venv/bin/delivery-gateway deliver \
  --db artifacts/demo/state.db \
  --consumer consumer-b \
  --request-id demo-b-1 \
  --now-ms 1000

.venv/bin/delivery-gateway status --db artifacts/demo/state.db --pretty
.venv/bin/delivery-gateway verify --db artifacts/demo/state.db --pretty
```

重复执行完全相同的 `deliver` 请求会返回首次持久化的结果，不新增投递、令牌扣减或代数。

## 配置文件

### 主题与分区

```json
{
  "global_seed": 424242,
  "topics": [
    {
      "name": "orders",
      "partitions": 3,
      "partition_key_rule": "fnv1a32_mod",
      "backlog_limit": 4
    }
  ]
}
```

支持的规则：`fnv1a32_mod`、`sha256_mod`、`crc32_mod`。`backlog_limit` 是每个分区初始生成的消息上限，也是确定性生成器的填充目标。

### 消费者租约

```json
{
  "consumers": [
    {
      "consumer_id": "consumer-a",
      "topic": "orders",
      "request_size": 4,
      "rate_limit_per_sec": 2.0,
      "delivery_timeout_ms": 20
    }
  ]
}
```

消费者令牌桶补充速率取租约的 `rate_limit_per_sec`，桶容量取配额的 `per_consumer.capacity`。

### 配额

```json
{
  "config_version": 3,
  "global": {
    "capacity": 8,
    "refill_rate_per_sec": 4.0
  },
  "per_consumer": {
    "capacity": 4
  },
  "round_budget_ms": 100,
  "delivery_timeout_ms": 20,
  "max_delivery_generation": 3,
  "retry_backoff_base_ms": 10,
  "retry_backoff_max_ms": 70,
  "attempt_duration_ms": 5
}
```

`config_version` 只允许等于已存版本或升级；回退返回 `E_CONFIG_VERSION_ROLLBACK`。同版本不同内容返回 `E_CONFIG_VERSION_CONFLICT`。有投递历史后升级版本返回 `E_CONFIG_VERSION_FORWARD_UNSUPPORTED`，避免历史账本基线不可解释。

### 逻辑时钟与异常注入

```json
{
  "initial_now_ms": 1000,
  "regression_tolerance_ms": 100
}
```

注入清单支持 `success`、`fail`、`reject`、`expire`、`slow`、`crash_after_claim`。条目可精确指定 `message_id`，也可用 `topic + partition + seq` 选择消息，并可限定 `generation`。

## CLI

```text
load     校验全部输入并原子装载配置、消息和注入项
deliver  执行一轮受配额约束的追平投递
resume   重放未完成投递并继续未完成请求
confirm  幂等确认成功投递
status   以稳定排序 JSON 输出积压、桶水位、预算消耗和死信统计
verify   重放令牌事件、代数、退避与墓碑并校验持久化状态
```

`status` 默认输出单行排序 JSON；`--pretty` 只改变缩进，不改变字段或列表顺序。

## 账本与调度语义

### 双层令牌桶

令牌使用整数“微令牌”记账，`1 token = 1_000_000`。容量、余额、补充量和扣减量都不经过浮点数。

每次投递尝试在同一个 `BEGIN IMMEDIATE` 事务中：

1. 根据 `now - last_refill_ms` 与补充速率计算全局限速桶和消费者桶的新增令牌。
2. 分别扣减 1 个全局令牌和 1 个消费者令牌。
3. 写入令牌事件的前值、补充量、后值以及两层 `last_refill_ms`。
4. 递增消息投递代数，写入 `deliveries` 的 `started` 行。
5. 更新请求游标、队列版本和逻辑时钟水位。

任一步骤失败或被强杀，整个事务回滚，不会出现“扣了令牌但没有投递行”或“加了代数但没扣令牌”的已提交中间状态。

### 等待而非拒绝

令牌不足时不丢消息也不死信：

- 若补充到 1 个令牌所需的等待仍在本轮逻辑预算和单次投递超时内，调度器前进逻辑游标并继续。
- 否则写入 `token_waits`，记录两层桶水位、`last_refill_ms`、所需等待毫秒数和 `not_started` 结局，保留消息。

等待事件不扣令牌；退避事件也不扣令牌。二者与配额账本分开记录。

### 一轮调度

消息按 `(partition, seq_in_partition)` 稳定排序。每个请求最多处理租约 `request_size` 条消息，且 `--limit` 不得超过它。

结局分类：

- `delivered`：已启动并成功投递。
- `failed_in_flight`：已启动后由注入项判定失败、拒收或过期。
- `abandoned`：启动前发现计划持续时间超过单次逻辑超时，主动放弃本轮启动。
- `not_started`：轮预算、令牌等待或消息当前不可用导致本轮未启动，消息保留。

只有 `cursor + duration <= deadline` 才会启动。已启动投递不会再被事后改标为超时。

### 投递代数

代数表示“已裁决的投递尝试序号”。一次开始投递即为第 N 代，成功、失败、拒收和过期都不会在确认阶段再次改代数：

- `generation` 从 0 开始，每次启动尝试原子加 1。
- 确认只写确认标记，不递增代数。
- 重复确认、跨消费者确认、迟到确认都是幂等的，不会回退或重复递增。
- 已确认消息状态为 `confirmed`，不会再次进入 `ready` 队列。

### 退避与死信

失败重试延迟：

```text
capped_base = min(retry_backoff_max_ms, base * 2^(generation-1))
jitter = SHA-256(message_id, consumer_id, generation) 的确定性模值
delay = min(retry_backoff_max_ms, capped_base + jitter)
next_at = completion_ms + delay
```

实际实现中抖动模空间为 `base_ms + 1`。失败达到代数上限、消费者拒收、消息过期分别迁移死信，原因写入 `failed`、`rejected`、`expired`。死信墓碑由消息 ID 唯一约束保证只写一次。

## 中断恢复

每个投递请求有持久化进度：`running/completed`、逻辑游标、截止时刻、已处理数量和逐条结果。`resume`：

1. 找到所有 `deliveries.status='started'` 的未完成投递。
2. 使用启动事务中已持久化的计划结果完成它们，不再扣令牌或递增代数。
3. 继续 `running` 请求的剩余步骤。
4. 已提交步骤直接从请求日志恢复，不重新执行。

验收使用的强杀点：

```text
claim_after_bucket_update
claim_after_generation_increment
claim_before_commit
claim_after_commit
complete_after_tombstone
complete_before_commit
complete_after_commit
```

例如：

```bash
GATEWAY_CRASH_POINT=claim_before_commit \
.venv/bin/delivery-gateway deliver --db state.db --consumer consumer-b \
  --request-id crash-1 --now-ms 1000 --limit 1

.venv/bin/delivery-gateway resume --db state.db --now-ms 1000
.venv/bin/delivery-gateway verify --db state.db
```

## 多进程一致性

写入命令对整个操作持有 `<db>.lock` 的 `flock` 排他锁，并在 SQLite 内继续使用 `BEGIN IMMEDIATE` 事务、WAL 和 `busy_timeout`。因此：

- 同一 `(message_id, generation)` 最多一行投递记录。
- 两个进程不能同时从同一桶扣到负余额。
- 死信墓碑唯一。
- 相同请求 ID 并发提交时，后到进程读取已提交结果，不重复执行。
- `status`/`verify` 在 SQLite 读事务快照中读取，不会混用不同提交点的队列版本和桶水位。

## 业务错误码

主要错误码：

| 场景 | 错误码 |
|---|---|
| 配置 JSON/文件非法 | `E_CONFIG_FILE`、`E_CONFIG_JSON`、`E_CONFIG_ROOT` |
| 主题重复/名非法 | `E_TOPIC_DUPLICATE`、`E_TOPIC_NAME_INVALID` |
| 分区数/规则/积压上限非法 | `E_TOPIC_PARTITIONS_INVALID`、`E_TOPIC_PARTITION_RULE_INVALID`、`E_TOPIC_BACKLOG_LIMIT_INVALID` |
| 消费者重复/字段非法 | `E_CONSUMER_DUPLICATE`、`E_CONSUMER_ID_INVALID`、`E_CONSUMER_RATE_INVALID`、`E_CONSUMER_TIMEOUT_INVALID` |
| 主题不存在 | `E_TOPIC_NOT_FOUND` |
| 消费者租约缺失 | `E_CONSUMER_NOT_FOUND` |
| 桶容量/补充速率非法 | `E_GLOBAL_CAPACITY_INVALID`、`E_GLOBAL_REFILL_INVALID`、`E_CONSUMER_CAPACITY_INVALID` |
| 预算/代数/退避非法 | `E_ROUND_BUDGET_INVALID`、`E_MAX_GENERATION_INVALID`、`E_BACKOFF_BASE_INVALID`、`E_BACKOFF_MAX_INVALID` |
| 配置版本回退 | `E_CONFIG_VERSION_ROLLBACK` |
| 同版本不同内容 | `E_CONFIG_VERSION_CONFLICT` |
| 投递消息不存在 | `E_MESSAGE_NOT_FOUND` |
| 确认投递不存在 | `E_DELIVERY_NOT_FOUND` |
| 逻辑时钟回拨超阈值 | `E_CLOCK_REGRESSION` |
| 请求 ID 载荷冲突 | `E_REQUEST_CONFLICT` |
| 注入选择器非法/重复 | `E_FAULT_TARGET_INVALID`、`E_FAULT_TARGET_NOT_FOUND`、`E_FAULT_DUPLICATE` |

错误 JSON 包含 `code`、`message` 和可诊断 `details`。配置校验在打开写连接前完成；非法配置不会创建数据库或写入状态。

## 测试与验收

运行全部测试：

```bash
.venv/bin/python -m unittest discover -v tests
```

运行可重复验收并写入实际结果：

```bash
.venv/bin/python scripts/run_validation.py --output artifacts/validation.json
```

当前实际结果见 `artifacts/validation.json`，摘要：

- Python `3.11.18`，SQLite CLI `3.37.2`
- 20 项测试全部通过
- 两次相同逻辑序列的投递输出、状态 JSON 与账本 SHA-256 完全一致
- 重复确认与跨消费者确认幂等，代数保持 1
- `fault_crash_after_claim` 和 `claim_before_commit` 强杀后恢复，账本无重复扣减
- 两个 `deliver` 进程加一个 `resume` 进程并发，无重复 `(message_id, generation)`、无重复墓碑、全局桶不为负、`verify` 通过

## 设计边界

- 消息生成与投递结果是本地确定性模拟，不访问网络目标。
- 有投递历史后不支持配额配置升级，以避免改变历史账本基线。
- 成功投递后等待显式 `confirm`；未确认的成功消息不会自动重投。
- 逻辑时间预算、超时预算和退避都以毫秒整数表示。

