# 本地顺序消息中间件

这是一个只依赖 Python 标准库和 SQLite 的本地顺序消息引擎，用于在固定样本上复现：

- 按顺序键稳定路由到分区；
- 分区日志原子追加与严格顺序消费；
- 事务消息“预备 -> 提交”两阶段可见性；
- 事务超时、回滚与不可见墓碑；
- 四阶段受控异常注入；
- 进程强制终止后的状态恢复；
- 多进程并发下的幂等生产、提交与确认；
- 双轮完全一致的可重复验证。

所有业务时间均来自样本或命令参数中的注入逻辑时钟。代码不调用 `time.time()`、`datetime.now()` 等系统墙钟接口。

## 环境

实际验证环境：

- Python `3.11.15`
- SQLite `3.53.1`
- pytest `9.2.0`

无需外部服务或容器。工作目录、SQLite WAL 和 `fcntl` 文件锁均使用本地文件。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
```

仓库自带 `smq` 包装器；如果 `python3.11` 不可用会回退到 `python3`：

```bash
./smq --help
./smq --db /tmp/smq/state.db load --sample samples/orders.json
```

## 固定样本

规范样本位于 `samples/orders.json`，包含两个主题：

- `orders`：3 个分区；
- `events`：2 个分区。

每个主题通过 `ordering_key_rules` 给出每分区规则。当前支持唯一的
`hash_modulo/fnv1a32` 规则：

```json
{
  "type": "hash_modulo",
  "algorithm": "fnv1a32",
  "modulus": 3,
  "partition": 0,
  "remainder": 0,
  "salt": "orders-v1"
}
```

校验要求同一主题的规则使用相同 `salt/modulus`，余数恰好覆盖全部分区。
因此每个非空顺序键只会匹配一个分区，同一顺序键不会跨分区漂移。

生产批次格式：

```json
{
  "batch_seq": 2,
  "topic": "orders",
  "ordering_key": "customer-bob",
  "transaction": {"declared": true, "id": "tx-2001"},
  "logical_ts_ms": 1100,
  "messages": [
    {
      "message_id": "m-0003",
      "payload": {
        "encoding": "utf8-json",
        "data": {"order_id": "B-2001", "amount": 250}
      }
    }
  ]
}
```

`payload.encoding` 支持 `utf8`、`utf8-json`、`base64`。`base64` 载荷必须能继续解码为 UTF-8。
批次可有 1 到 `max_batch_messages` 条消息，每条消息占用一个连续分区偏移。

`load` 会校验并摄入样本，但事务批次只进入预备态；调用方随后显式执行
`commit`、`rollback` 或通过超时恢复生成墓碑。

## CLI

所有成功和失败结果都是单行、排序键、无随机字段的 JSON。

```bash
# 校验、建库、幂等摄入固定样本
./smq --db /tmp/smq.db load --sample samples/orders.json

# 使用故障清单加载
./smq --db /tmp/smq.db load \
  --sample samples/orders.json \
  --injections samples/injections/full_chain.json

# 提交或回滚
./smq --db /tmp/smq.db commit --tx-id tx-2001 --logical-ms 1700
./smq --db /tmp/smq.db rollback --tx-id tx-2002 --logical-ms 1800

# 拉取分区消息
./smq --db /tmp/smq.db consume \
  --consumer-id analyst --topic orders --partition 2 \
  --max 10 --logical-ms 1800

# 确认已拉取偏移
./smq --db /tmp/smq.db ack \
  --consumer-id analyst --topic orders --partition 2 \
  --offset 0 --logical-ms 1900

# 恢复未完成步骤、清理孤立占位、执行超时扫描
./smq --db /tmp/smq.db resume --logical-ms 7000

# 稳定状态与摘要校验
./smq --db /tmp/smq.db status
./smq --db /tmp/smq.db verify

# 从样本中选择单批次重新生产（幂等）
./smq --db /tmp/smq.db produce \
  --sample samples/orders.json --batch-seq 2
```

`status` 的分区项包含：

- `offset_watermark`：下一偏移；
- `visible_messages`：当前对消费者可见的消息数；
- `tombstones`：超时、回滚或无引用清理产生的墓碑数；
- `prepared_messages`：预备但不可见的消息数；
- `pending_transactions`：该分区涉及的未完成事务数。

`verify` 重新计算分区追加摘要链、分区状态摘要、批次摘要、事务摘要、
载荷摘要及可见性约束。任何不一致返回 `ok=false`，进程退出码为 1。

## 持久化与事务可见性

SQLite 使用 `journal_mode=WAL`、`synchronous=FULL` 和显式
`BEGIN IMMEDIATE` 事务。跨多个 SQLite 步骤的命令同时持有
`<db>.lock` 上的 `flock` 排他锁。

主要状态：

- `batches`：批次意图和追加/预备/提交/墓碑状态；
- `partition_log`：不可变追加字段、偏移、载荷摘要、逻辑时间戳和可见性；
- `partition_state`：分区下一偏移、追加摘要链和状态摘要；
- `transactions`：预备时间、截止时间、持久化事务状态和事务摘要；
- `consumer_offsets`：每个消费者/主题/分区已确认到的下一偏移；
- `deliveries`：确定性投递 ID 与 pending/acked 确认状态；
- `operations`：崩溃前已持久化的提交或确认意图；
- `injections`：故障清单及已触发计数；
- `clock_state`：单调逻辑时钟水位。

事务消息追加时，`partition_log.visibility='prepared'`。只有事务状态从持久化的
`prepared` 原子迁移到 `committed` 时，同一 SQLite 事务才会把所有对应记录改成
`visible`。消费者查询不会读取 `prepared` 记录。

事务超时使用：

```text
deadline_ms = prepared_at_ms + transaction_timeout_ms
timeout      = commit_clock_ms >= deadline_ms
```

超时或显式回滚会把预备记录改为 `tombstone`。墓碑是已终结状态，不阻塞后续
可见消息；真正的 `prepared` 记录会阻塞该分区后续消息投递。

## 分区消费与确认

消费从消费者已确认偏移开始，按 `offset` 严格扫描：

1. 遇到 `prepared` 立即停止，不跳过未提交消息；
2. 遇到 `tombstone` 可安全越过；
3. 只投递 `visible` 消息；
4. 相同输入的投递 ID 由消费者、主题、分区、偏移和载荷摘要确定性计算；
5. 未确认投递在后续 `consume` 中按相同顺序重放。

确认规则：

- 必须存在该消费者、主题、分区的 pending 投递；
- `offset == committed_offset` 时原子推进到 `offset + 1`；
- 中间只允许越过终结墓碑，遇到可见未确认或预备消息会拒绝；
- 重复确认和迟到确认返回幂等成功，不倒退偏移；
- 同一消费者/主题/分区的偏移只会单调前进。

## 故障注入

注入清单结构见 `samples/injections/full_chain.json`。四个阶段分别映射固定错误码：

| 阶段 | 动作 | 错误码 |
|---|---|---|
| `append` | `partition_log_full` | `E_APPEND_LOG_FULL` |
| `prepare` | `transaction_conflict` | `E_PREPARE_TX_CONFLICT` |
| `commit` | `digest_mismatch` | `E_COMMIT_DIGEST_MISMATCH` |
| `ack` | `offset_regression` | `E_ACK_OFFSET_REGRESSION` |

可用匹配字段包括 `topic`、`batch_seq`、`tx_id`、`partition`、`consumer_id`、`offset`。
`occurrence` 控制第几次匹配触发。错误结果包含 `injection_id`、阶段和业务诊断字段。

`action: crash` 可用于四个阶段：

- `timing: before`：先持久化触发计数和未完成意图，再调用 `os._exit(137)`；
- `timing: after`：先完成当前原子步骤，再持久化触发计数并退出。

强制终止可能没有 stdout 或退出报告。测试只依赖 SQLite 和后续 `resume`。

自带清单：

- `none.json`：无故障；
- `full_chain.json`：四种受控业务异常；
- `crash_append.json`：追加步骤后终止；
- `crash_prepare.json`：预备步骤后终止；
- `crash_commit.json`：提交状态变更前终止；
- `crash_ack.json`：确认状态变更前终止。

注入只读写 SQLite 中的触发状态，不修改样本 JSON。

## 恢复

`resume` 按稳定排序执行：

1. 完成 `batches.status` 为 `pending_append` 或 `appended` 的追加/预备步骤；
2. 使用持久化的请求逻辑时钟完成 pending 的 `commit` 操作；
3. 完成 pending 的 `ack` 操作；
4. 把无事务引用的 `prepared` 占位改为墓碑；
5. 在最终逻辑时钟水位下扫描并终止超时事务；
6. 汇报仍为 pending 的投递，等待消费者重新拉取或确认。

已提交或已确认步骤不会被重复执行。相同命令和相同逻辑时钟重复调用
`resume` 是幂等的。

## 并发

- `flock` 将跨多步操作串行化；
- SQLite `BEGIN IMMEDIATE` 和唯一约束保护批次、事务、投递及分区偏移；
- 同一批次重复生产只保留一份日志；
- 同一事务重复提交只发生一次状态迁移；
- 同一消费者偏移重复确认只推进一次；
- 消费者只读取同一 SQLite 快照中已提交且可见的记录。

运行时新批次要求其 `batch_seq` 大于该主题已有的最大批次号；样本加载还要求
整个样本的批次序号全局严格递增。

## 确定性验证

```bash
.venv/bin/python -m pytest -q
python3.11 scripts/verify_e2e.py
python3.11 scripts/verify_e2e.py --show-transcript
```

`verify_e2e.py` 在两个独立临时目录中分别执行同一组 36 条命令，覆盖：

- 两批注入失败后的继续加载；
- 提交摘要异常与重试；
- 确认回退异常与重试；
- 消费、确认、恢复和 `verify`；
- 追加后、预备后、提交前、确认前四个强制终止点。

然后逐字段比较两轮 transcript，并校验样本文件运行前后 SHA-256 未变化。

## 实际验证结果

2026-11-20 在 Python 3.11.15 / SQLite 3.53.1 / pytest 9.2.0 下执行：

```text
21 passed
```

双轮端到端验证结果：

```json
{"command_count_per_run":36,"deterministic":true,"runs":2,"samples_unchanged":true,"transcript_sha256":"7488e30b955295bc625eacf59caf34611d0ead33ff01a06715439a301ea2154f"}
```

## 代码布局

- `sequential_mq/model.py`：纯输入校验、路由规则、载荷归一化和注入清单解析；
- `sequential_mq/storage.py`：SQLite schema、事务上下文、文件锁和摘要链；
- `sequential_mq/engine.py`：生产、两阶段事务、消费确认、注入和恢复状态机；
- `sequential_mq/cli.py`：稳定 JSON CLI；
- `samples/`：固定样本和故障清单；
- `tests/`：校验、确定性、事务、注入、崩溃恢复和并发测试；
- `scripts/verify_e2e.py`：双轮完整链路复现工具。

