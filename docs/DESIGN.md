# 设计与一致性说明

## 1. 状态模型

账本状态是一个规范 JSON 对象，包含：

- `queue_version`：队列状态版本，每轮提交递增；
- `clock_watermark_ms`：逻辑时钟水位；
- `global_token`：全局令牌桶；
- `services[*].token`：每服务令牌桶；
- `services[*].replicas`：固定已就绪副本与槽位数；
- `requests[*]`：到达信息、优先级、代数、可分发时刻、确认标记、尝试链、死信；
- `tombstones`：按请求唯一的墓碑；
- `rounds`：round_id 到命令与结果的幂等记录；
- `counters`：分发、成功、失败、拒收、过期、放弃、死信计数。

状态不含任何系统墙钟派生业务字段。相同输入、相同命令序列得到相同规范 JSON。

## 2. 纯引擎不变量

`gateway.engine.Engine` 不打开文件或数据库。其关键不变量：

1. `request.generation` 只在新分发启动时增加，且增加后的值等于新 `attempt.generation`。
2. 一个请求最多有一个 `inflight` 尝试；确认、失败、拒收、过期、放弃都会使其离开 `inflight`。
3. 双桶扣费是“先检查、后同时扣减”：任一桶余额不足即不启动、不扣任何桶。
4. `0 <= balance_fixed <= capacity_fixed`。
5. `0 <= refill_remainder < 1000`，每次补充使用上次余量。
6. `next_eligible_ms` 在退避期间只由逻辑时钟和确定性哈希决定。
7. 墓碑按请求至多一个；重复失败不改变墓碑。
8. 确认键为 `(request_id, generation, replica_id)`；已终态尝试只返回幂等/迟到结果，不改变状态。
9. 超时路径必须满足 `clock_ms > deadline_ms` 且来自显式主动中止。
10. 所有排序都有稳定的最终决胜键（服务名、请求序号、副本 ID）。

`run_round` 在入口处保存深拷贝；任何异常都会原地恢复旧状态，保证纯引擎的单命令原子性。

## 3. 事务协议

写入路径：

```text
acquire <db>.lock
open SQLite connection
BEGIN IMMEDIATE
read pointer -> (version V, snapshot S)
state = decode(S)
result = Engine(state).run_round(command)
if state unchanged:
    ROLLBACK; return idempotent
insert snapshots(V+1, parent=V, hash(state), state)
UPDATE pointer SET version=V+1 ... WHERE id=1 AND version=V
if rowcount != 1: ROLLBACK; retry
insert events(V+1, command)
COMMIT
release lock
```

文件锁保证多进程写者串行；SQLite 的 `BEGIN IMMEDIATE`、WAL、`synchronous=FULL` 和版本 CAS 提供第二层保证。读路径只执行：

```sql
SELECT p.version, s.state_json, s.state_hash
FROM pointer p JOIN snapshots s ON s.version = p.version
WHERE p.id = 1;
```

因此读者不会看到“快照行已写、指针未换”的中间态。

## 4. 崩溃恢复

测试通过 `GW_FAILPOINT` 和 `os._exit(137)` 模拟强制终止：

| 故障点 | 持久化效果 | 恢复行为 |
|---|---|---|
| `after_mutation` | 事务未提交 | SQLite 回滚，重放原命令一次 |
| `after_snapshot_insert` | 新快照未提交、指针旧 | 回滚，旧指针完整 |
| `before_commit` | 事务未提交 | 回滚，旧指针完整 |
| `after_commit` | 新指针已提交 | 识别 `inflight` 尝试，不重复扣费/加代数 |

`after_commit` 的恢复不是“重放启动”，而是：

- `resume` 报告 `recovery.inflight_detected`；
- 已提交尝试仍占槽位，不会再被同代分发；
- 只有 `--abort-overdue` 且逻辑时钟超过 deadline 时才转为 `expired`；
- 过期后若未达最大代数，写入确定性退避；再后续轮次才可能启动下一代并再次扣令牌。

## 5. 并发与幂等

- 同一 `round_id` 相同命令：`Engine.run_round` 在读取状态后直接返回已存结果，状态不变，事务不提交。
- 同一 `round_id` 不同命令：返回 `E_IDEMPOTENCY_CONFLICT`，状态不变。
- 不同 `round_id` 同时竞争：文件锁串行化；后到者看到已 `inflight` 的请求会跳过。
- 全局/服务令牌、副本槽位都在同一串行事务内检查和扣减，因此不会并发超扣。
- 死信墓碑按请求唯一，重复失败结果只返回幂等。

## 6. 验证器

`gateway.verify`：

1. 读取并校验 `inputs` 中的 SHA-256；
2. 由规范化输入重建 genesis；
3. 校验 snapshot 版本连续、父指针正确、指针指向最新版本；
4. 校验事件版本序列为 genesis + 各提交版本；
5. 逐事件重放到 `Engine`，比较每个版本快照哈希；
6. 将重算最终状态与当前指针状态递归逐字段比较。

任一步失败返回 `E_VERIFY_MISMATCH` 或 `E_DB_CORRUPT`，不会静默接受。

## 7. 明确假设

- 副本被视为已就绪、固定槽位集合；不建模冷启动或节点装箱。
- 每次分发尝试在全局桶与服务桶各消耗 1 个并发分发令牌。
- 请求输入/输出 token 数记录为模型 token 预算，不决定并发令牌扣费。
- 每轮预算成本取 `ceil(batch_window_ms / compute_units)`，用于确定性逻辑时间裁决；实际推理耗时不在样本中，因此不读取墙钟。
- 退避与补充使用毫秒逻辑时钟，补充速率按每秒整令牌解释。
- `max_inflight` 可选择配置；未配置时默认等于对应令牌容量。
