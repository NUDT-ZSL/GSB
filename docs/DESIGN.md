# 技术设计

## 1. 模块结构

| 模块 | 职责 |
|---|---|
| `validation.py` | 配额、限流、事件的解析与业务校验 |
| `models.py` | 不可变配置、事件、可变账本状态模型 |
| `engine.py` | 纯函数式裁决引擎，在线写入与离线重放共用 |
| `schema.py` | SQLite 表、约束与索引 |
| `locking.py` | POSIX `flock` / Windows `msvcrt.locking` 文件锁 |
| `ledger.py` | 事务持久化、状态读取、恢复与校验 |
| `cli.py` | 六个必需子命令 |

核心原则：**裁决逻辑不依赖内存中的上一轮状态**。每次写入都从 SQLite 已提交账目装载状态，交给纯裁决引擎，再在同一事务写回。

## 2. 数据模型

SQLite 使用 WAL、`synchronous=FULL`、外键与 30 秒 busy timeout。

主要表：

- `quota_layers`：三层声明、软/硬字节与对象阈值
- `usage`：每层已用字节、对象数、软限状态与进入/退出快照
- `objects`：对象键、层级归属、大小、创建/更新事件序号
- `tokens`：租户微令牌余额、补充水位、最后逻辑时钟、余数
- `events`：事件水位、规范化载荷、裁决结果、指纹、元数据
- `idempotency`：幂等键、载荷指纹、对应事件序号、首次结果
- `soft_events`：软限 `ENTER`/`EXIT` 历史
- `pending_operations`：仅供崩溃恢复测试的孤儿意图探针
- `meta`：规范化配置、配置哈希、最后已裁决序号、模式版本

数据库约束主动阻止负用量、负令牌、令牌超过容量、软百分比高于硬百分比等非法持久状态。

## 3. 写入事务

一次写入的完整流程：

1. 在数据库外完成事件字段和层级校验。
2. 获取 `ledger.db.lock` 排他 OS 文件锁。
3. 打开 SQLite，执行 `BEGIN IMMEDIATE`。
4. 读取配置、三层用量、对象、令牌、幂等记录与水位。
5. 幂等命中：返回持久化的首次结果，不修改任何账目。
6. 校验事件序号与逻辑时钟回拨。
7. 计算对象差值、三层投影用量、硬/软检查、令牌补充与消耗。
8. 若有拒绝：写入裁决事件与幂等结果，水位推进，但配额和令牌扣减不生效。
9. 若允许：同时更新三层用量、对象、令牌、软状态、软告警、事件、幂等结果和水位。
10. `COMMIT`；任何异常执行 `ROLLBACK`。

读取方不获取写锁，而使用 WAL 的只读事务快照；一次 `status` 或 `verify` 的全部查询来自同一已提交快照。

## 4. 分层配额守恒

对每个事件，三层使用同一个 `(bytes_delta, objects_delta)`：

- 新建：`+size`、`+1`
- 覆盖：`new_size - old_size`、`0`
- 删除：`-persisted_size`、`-1`

因此：

```text
tenant.used >= sum(bucket.used)
bucket.used >= sum(prefix.used)
```

样本系统中没有父层直接对象，所以实际为严格相等。删除使用持久化对象大小，而不是删除事件中可能为 0 的字节字段。

## 5. 软硬阈值

声明百分比必须是 `0..100` 整数，且 `soft_pct <= hard_pct`。

```text
soft_limit = floor(capacity * soft_pct / 100)
hard_limit = floor(capacity * hard_pct / 100)
```

判定：

- `projected > hard_limit`：硬拒绝。
- `projected > soft_limit`：软超限，允许写入。
- 软状态从 `false -> true`：写入一条 `ENTER`，记录首次触发序号、当前用量和阈值。
- 软状态从 `true -> false`：写入一条 `EXIT`。
- 软状态保持 `true` 时重复超限：不新增告警。

检查顺序固定且写入返回的 `decision_order` 字段中明确给出。限流是准入闸门，因此优先于三层的字节/对象检查。

## 6. 确定性令牌桶

限流参数：

```text
capacity_tokens = bucket_capacity + burst_depth
effective_rate = min(refill_rate_per_second, requests_per_second)
```

令牌以微令牌保存。补充速率转换为最简有理数 `n/d` 后，经过 `elapsed_ms` 的增量：

```text
numerator = remainder + elapsed_ms * n * 1000
earned    = floor(numerator / d)
remainder = numerator mod d
```

补充后余额截断到 `capacity_tokens * 1_000_000`。未通过硬配额时只展示“若放行会得到多少令牌”，但不持久化扣减或补充；下一次事件会从旧补充水位重新计算，因此路径无关。

拒绝等待：

```text
deficit = 1_000_000 - available_micro
wait_ms = ceil(deficit * d / (n * 1000))
retry_after_ms = max(wait_ms, backoff_window_ms)
```

时钟回拨超过容差时在修改任何状态前抛错，因此余额、账目、水位和幂等表均保持不变。

## 7. 幂等与事件序号

载荷指纹为事件规范化 JSON 的 SHA-256。规范化字段包括：

```text
seq, tenant, bucket, prefix, object_key, bytes,
deleted, clock_ms, idempotency_key
```

- 幂等键存在且指纹相同：返回首次结果原文。
- 幂等键存在但指纹不同：`E_IDEMPOTENCY_CONFLICT`。
- 幂等键不存在且 `seq <= last_decided_seq`：`E_EVENT_SEQ_CONFLICT`。
- 拒绝裁决也占用事件序号和幂等键，防止重试时得到不同结果。

`write_batch` 会在第一次数据库写入前解析并校验全部事件，避免“批次后部非法但前部已写入”。

## 8. 崩溃一致性

生产路径不把配额或令牌作为独立可恢复的“预占”记录提交。所有账目更新、对象更新、令牌扣减、事件与幂等结果位于同一个 SQLite 事务：

- 在 `COMMIT` 前被强制终止：SQLite WAL 中只有未提交帧，恢复时丢弃。
- 在 `COMMIT` 后被终止：完整事务已落盘，重试由幂等表返回首次结果。
- 不存在“令牌已扣但空间未占”或反向的持久中间态。

`resume` 还处理显式 `after_intent` 崩溃探针产生的孤儿 `pending_operations` 记录。该探针只写入意图、不预占账目，所以清理时释放量为 0；实际配额预占从未提交。

## 9. 并发

- 所有写操作先获取 OS 文件锁，避免同机进程在 SQLite 锁之上产生不同重试顺序。
- SQLite `BEGIN IMMEDIATE` 保证同一时刻只有一个写事务。
- WAL 允许读操作在写事务进行时继续读取最后完整提交快照。
- 并发同幂等键由唯一约束与串行裁决保证只消费一次。
- 并发同序号不同幂等键只有一个能进入裁决，其余得到 `E_EVENT_SEQ_CONFLICT`。

## 10. 验证器

`verify` 不信任 `usage`、`objects`、`tokens` 等结果表，而是：

1. 从 `meta` 读取规范化配置。
2. 从 `events` 读取已裁决事件与结果。
3. 使用与在线写入完全相同的 `engine.adjudicate`，从初始状态逐事件重放。
4. 比较三层用量与软状态、对象表、令牌表、软告警历史、幂等记录、事件元数据和水位。
5. 检查父层用量不小于子层总和、令牌非负等守恒约束。
6. 任何差异进入稳定排序的 `mismatches`，不做自动修复。

