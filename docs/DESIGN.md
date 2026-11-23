# 设计说明

## 1. 目标与约束

系统在单机内提供多租户资源账本、跨服务预留仲裁、借调义务、等待队列追平和声明版本管理。业务时间完全不取自系统墙钟：

- JSON 申请带有 `requested_at_ms` 与 `expires_at_ms`。
- CLI / API 操作使用 `--now-ms` / `now_ms` 注入当前逻辑时刻。
- `meta.logical_clock_ms` 只增不减，用作持久化时钟水位。
- 列表排序、到期判断与恢复重放都不使用当前日期。

## 2. 持久化状态

| 表 | 作用 |
| --- | --- |
| `resource_pools` | 池容量、池未归还借调总量 |
| `tenants` | 当前声明快照、基础已用、借调已用、精度标记、累计误差 |
| `tenant_versions` | 不可变声明版本快照与分类 |
| `services` | 资源池、单轮批次、并发上限 |
| `applications` | 申请原始需求、逻辑时间窗口、状态、准入版本 |
| `reservations` | 活动/已释放预留及基础/借调拆分 |
| `wait_queue` | 稳定排序等待队列与阻塞原因 |
| `borrow_obligations` | 每笔借调的来源池、池余量快照、时刻、期限、未归还量 |
| `ledger_entries` | 准入与释放的不可变审计分录及前后余额 |
| `operations` | PREPARED / DONE 两阶段操作日志 |
| `release_events` | 释放幂等键、触发来源、追平结果 |
| `wake_attempts` | 单次释放触发的每个候选裁决记录 |
| `conversion_steps` | 定点换算原始值、标度、舍入与误差上界 |
| `precision_events` | 精度敏感标记和完整来源链 |
| `snapshots` | 每次原子提交后的状态摘要 |

## 3. 状态机

### 申请

```text
STAGED
  ├─ 预算/批次限制 → ABANDONED
  ├─ 准入成功      → ADMITTED
  ├─ 配额/借调不足 → WAITING
  │                   ├─ 追平成功 → ADMITTED
  │                   ├─ 人工撤销 → CANCELLED
  │                   └─ 到期未准入 → ABANDONED
  └─ 终止

ADMITTED
  ├─ 到期/撤销 → RELEASED
  └─ 终止
```

### 操作

```text
PREPARED ──事务成功──> DONE
    │
    └─进程终止：事务回滚，PREPARED 保留；resume 幂等重放
```

### 预留

```text
ACTIVE ──释放事务──> RELEASED
```

释放事件为 `PREPARED -> COMPLETE`。`resume` 若看到 `PREPARED` 且预留仍为 `ACTIVE`，执行完整释放事务；若看到预留与事件均已提交，只把操作标记完成。

## 4. 关键事务

### 4.1 准入

同一 `BEGIN IMMEDIATE` 中：

1. 重读申请与租户当前版本。
2. 检查轮次预算、服务单轮批次、服务并发。
3. 计算基础份额与借调份额。
4. 写入 `conversion_steps` 和 `precision_events`。
5. 更新租户基础已用/借调已用。
6. 更新资源池未归还借调。
7. 写 `reservations`。
8. 需要时写 `borrow_obligations`。
9. 更新申请为 `ADMITTED`，删除任何等待行。
10. 写 `QUOTA_ADMIT` / `BORROW_ADMIT` 分录。
11. 标记操作为 `DONE`。
12. 写快照摘要。

任一步失败均回滚，不会出现“已扣账而无预留”或“有预留而无扣账”。

### 4.2 释放与追平

同一 `BEGIN IMMEDIATE` 中：

1. 验证借调义务、租户借调余额、资源池借调余量、基础已用。
2. 先写 `BORROW_RETURN`，更新租户与资源池借调。
3. 再写 `QUOTA_RELEASE`，恢复基础已用。
4. 将预留设为 `RELEASED`、申请设为 `RELEASED`。
5. 按稳定队列顺序尝试候选：
   - 成功：写完整准入分录，移出队列。
   - 阻塞：更新原因并停止。
   - 已过期：标记放弃并继续下一候选。
6. 写释放事件、候选记录、最终队列快照。
7. 标记操作 `DONE`，写账本快照。

违反任一借调/基础账本检查时返回 `RELEASE_BORROW_MISMATCH` 或 `RELEASE_QUOTA_MISMATCH`，整个事务回滚。

## 5. 不变量

```text
tenant.base_used_mc  = sum(active reservations.base_mc for tenant)
tenant.base_used_mib = sum(active reservations.base_mib for tenant)
tenant.borrowed_mc   = sum(active reservations.borrow_mc for tenant)
tenant.borrowed_mib  = sum(active reservations.borrow_mib for tenant)

0 <= tenant.base_used_mc  <= tenant.quota_mc
0 <= tenant.base_used_mib <= tenant.quota_mib
0 <= tenant.borrowed_mc   <= tenant.borrow_limit_mc
0 <= tenant.borrowed_mib  <= tenant.borrow_limit_mib

pool.borrowed = sum(active reservation.borrow in pool)
sum(tenant.quota) + pool.borrowed <= pool.capacity

reservation.total = reservation.base + reservation.borrow
open obligation.outstanding = reservation.borrow

ledger QUOTA_ADMIT - QUOTA_RELEASE = tenant.base_used
ledger BORROW_ADMIT - BORROW_RETURN = tenant.borrowed
```

所有不变量均由 `verify` 重新计算并核对，而不是只信任缓存字段。

## 6. 并发协议

进程级锁路径默认为：

```text
<db-path>.lock
```

锁顺序：

```text
OS file lock -> in-process RLock -> SQLite BEGIN IMMEDIATE
```

`BEGIN IMMEDIATE` 使写事务在获得 SQLite 写锁时立即失败/等待，从而避免读后写竞争。唯一约束和确定性操作 ID / 释放 ID 处理重复请求。

SQLite 参数：

```text
journal_mode=WAL
synchronous=FULL
foreign_keys=ON
busy_timeout=30000
```

## 7. 版本比较

版本摘要不包含版本号本身，只包含：

```text
resource_pool
quota_mc, quota_mib
allow_overdraft
borrow_limit_mc, borrow_limit_mib
```

因此：

- 同版本、同摘要：`NO_OP`。
- 同版本、不同摘要：`VERSION_CONFLICT`。
- 更高版本、同字段：保留新版本指针，分类 `NO_OP`。
- 更高版本、有字段变化：`FULL_REPLACE`。
- 更低版本：`VERSION_ROLLBACK`。

额外安全条件：

- 新基础配额不得低于当前基础已用。
- 新借调上限不得低于当前未归还借调。
- 更新后池内基础配额加未归还借调不得超过池容量。

## 8. 精度模型

整数账本不参与浮点计算。定点视图使用：

```text
converted = round(raw_numerator * fixed_scale / raw_denominator)
```

每次转换记录 `rounding` 与 `error_bound_micro`。敏感判断比较：

```text
abs(effective_available - request) * 1_000_000 < tolerance * 1_000_000
```

当判断涉及毫核或 MiB 边界时，写入精度事件并把租户/申请标记为敏感。事件链包含具体换算步骤 ID，可直接追溯到原始分子和分母。

## 9. 恢复边界

SQLite 事务本身保证：

- 提交即完整可见。
- 中途终止即完整回滚。

系统在此基础上增加 PREPARED 意图日志，使“操作意图已持久化、执行事务尚未提交”这一跨事务窗口也可识别。恢复只处理 PREPARED，不重放 DONE 操作，因此不会重复扣账或归还。

快照摘要不参与业务裁决，只用于验证当前状态确实来自最近一次原子提交，并可用于问题定位。
