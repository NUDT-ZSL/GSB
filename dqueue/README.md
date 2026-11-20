# dqueue — 本地确定性延迟队列中间件

纯 Python 3.11 + SQLite + NumPy 实现的本地延迟队列。所有时间判定只使用可注入的
逻辑时钟毫秒值，**全代码不读取系统墙钟**（`rg "time\\.time|datetime\\.now"` 无命中），
不依赖任何外部服务或网络。

## 1. 目录结构

```
dqueue/
├── dqueue/                  # 库代码
│   ├── errors.py            # 业务错误码 + DQError（带诊断字段）
│   ├── clock.py             # 可注入逻辑时钟、回拨容差
│   ├── config.py            # 时间轮几何 / 自适应 / 租约配置
│   ├── manifest.py          # 任务清单与重试策略表解析校验、SHA-256 摘要
│   ├── backoff.py           # 指数退避 + NumPy 确定性抖动
│   ├── scheduler.py         # 精确最小堆 / 多级时间轮 + 开销估算与切换规则
│   ├── storage.py           # SQLite schema、WAL、文件锁、模式版本、操作日志
│   ├── queue.py             # 引擎：入队/扫描/poll/ack/resume/status/verify
│   └── cli.py               # enqueue/scan/poll/ack/resume/status/verify/mode
├── samples/                 # 固定确定性样本（任务流、策略表、时钟步进、配置）
├── tests/                   # 38 个 unittest 用例
├── demo.py                  # 端到端可重复演示（等价性/退避/死信/错误码）
├── demo_adaptive.py         # 大规模自适应切换演示
└── run/                     # 运行产物：SQLite、WAL、日志、报告（工作区内）
```

## 2. 快速开始

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e .
.venv/bin/python samples/make_samples.py     # 重新生成固定样本（可选）
.venv/bin/python -m unittest discover -s tests
.venv/bin/python demo.py
.venv/bin/python demo_adaptive.py
```

CLI 示例：

```bash
D="--db run/demo.db --config samples/queue_config.json"
dqueue $D enqueue --manifest samples/manifest.json --policies samples/policies.json --now 0
dqueue $D scan  --now 1500
dqueue $D poll  --topic orders.created --consumer c1 --now 1500
dqueue $D ack   --pull-id <pull_id> --consumer c1 --now 1600 --failure --error-code E_DOWNSTREAM
dqueue $D resume --now 9000
dqueue $D status | jq .
dqueue $D verify
dqueue $D mode --set wheel --now 500
```

## 3. 核心设计

### 3.1 解析校验（业务要求 1）

- 任务 ID 正则 `[A-Za-z0-9_-]{1,64}` 且清单内唯一；主题 `[A-Za-z][A-Za-z0-9_.-]{0,63}`。
- `fire_ms`、`max_retries` 必须是非负整数（拒绝 bool/float/负数），策略 `base_ms`/
  `cap_ms` 必须是正整数。
- 载荷摘要 = 规范化 JSON（`sort_keys`、紧凑分隔符）的 SHA-256；摘要与内容不一致返回
  `E_PAYLOAD_DIGEST_MISMATCH`。
- 策略标签缺失返回 `E_POLICY_NOT_FOUND`。
- **校验在数据库文件创建之前完成**，非法输入不产生任何持久化状态（有测试
  `test_invalid_manifest_creates_no_database_file` 佐证）。

### 3.2 两种调度算法与逐位一致（业务要求 2）

- `HeapScheduler`：`heapq` 最小堆，严格按 `(fire_ms, task_id)` 排序。
- `WheelScheduler`：多级时间轮，层级宽度
  `level_width(L) = tick_ms * ticks_per_wheel ** L`；按触发时间落入最低可容纳层级的槽位，
  推进逻辑时钟时排空 0 级到期槽并在层级边界逐级级联（cascade），超出视界的任务由
  overflow 刷新兜底。
- 两核心输入相同条目与相同时钟步进时，输出序列按 `(fire_ms, task_id)` 稳定排序，
  **逐位一致**。测试覆盖：样本流、乱序/非对齐插入、跨越视界（51200ms）、500 条
  固定种子随机流。

### 3.3 自适应切换（业务要求 3）

每次扫描先观测待调度任务数 `n`、触发跨度 `span_ms`，再按确定性模型估算开销：

```
heap_cost  = n * log2(n + 1)                 # O(n log n)，弹/压堆驱动
wheel_cost = max(1, span/tick_ms) + n        # 扫描槽位 + 每任务一次分桶
```

切换规则（`scheduler.choose_mode`）：

1. `n < switch_threshold` 一律保持/回到精确堆；
2. 堆模式下仅当 `wheel_cost < heap_cost * hysteresis_ratio` 才切到轮；
3. 轮模式下规模跌破阈值才回切（迟滞，防止抖动）。

每次切换在 `mode_switch_log` 落一条记录：版本号、前后模式、逻辑时刻、切换前后 `n`、
跨度、两项开销估算、触发原因。任务状态只存 SQLite，切换不搬运任务，因此在途到期任务
不丢失、不重复（触发由条件更新 `stage=SCHEDULED -> RUNNING` 的 rowcount 保证）。

### 3.4 退避与死信（业务要求 4）

```
raw   = min(cap_ms, base_ms * multiplier ** (attempt-1))
delay = clamp(round(raw * (1 + (2*noise-1) * jitter_pct/100)), 1, cap_ms)
```

`noise` 来自以 `(policy, task_id, attempt)` 字符串为固定种子的 NumPy `PCG64` 生成器，
与 Python 哈希种子无关，跨进程/跨机器可复现；同一任务同一重试次数重复计算结果相同。
重试次数超过 `max_retries` 时任务进入 `DEAD`，记录 `dead_reason=RETRY_EXHAUSTED` 与
最后错误码，正常扫描只选 `SCHEDULED` 任务，死信不会再被命中。

### 3.5 poll/ack 幂等与租约重投（业务要求 5）

- `scan` 为每次触发创建 `pulls` 记录并把任务置 `RUNNING`；`poll` 以条件更新领取租约。
- `ack` 以 `pull_id` 幂等：已 `COMMITTED` 直接返回 `idempotent=true`；租约超时后的迟到
  确认返回 `ignored_late=true` 且不改变状态；跨消费者确认返回
  `E_FOREIGN_CONSUMER`；引用不存在的拉取返回 `E_PULL_NOT_FOUND`。
- 租约到期（`lease_deadline_ms <= now`）由持久化状态判定重投回 `SCHEDULED`，
  原 pull 标记 `TIMEOUT`，重试计数不增加——不依赖任何内存残留。

### 3.6 持久化与崩溃恢复（业务要求 6）

SQLite 开启 WAL + `synchronous=FULL` + `busy_timeout`。任务阶段、重试次数、下次触发
时钟、拉取/确认水位、模式版本、操作日志（`op_journal`）均在 `BEGIN IMMEDIATE` 事务内
原子提交。`resume` 重放 `PENDING` 日志：已落库的触发/确认只做核验，回滚掉的半事务
按持久化状态重新开放，保证不重复触发、不重复计重试。测试用子进程 `os._exit(99)`
模拟 SIGKILL，覆盖入队中途、触发提交前后、确认提交中途三个崩溃点。

### 3.7 多进程并发与模式版本整体替换（业务要求 7）

- 调度/确认临界区取 `fcntl.flock` 排他文件锁，配合 SQLite 事务与条件更新：
  同一任务同一版本只触发一次、同一确认只提交一次、重试次数不会并发超增
  （`tests/test_concurrency.py` 用 6 个 spawn 进程并发扫描、5 个进程并发确认验证）。
- 每次模式变更生成递增 `version` 并在同一事务内整体替换 `current_mode_version`
  指针；请求旧版本号返回 `E_MODE_VERSION_ROLLBACK`。一次扫描在开始时读取版本并把
  `mode_version` 写入每条 pull，全程绑定该版本，读取方不会观察到新旧模式混合。

### 3.8 错误码

| 错误码 | 触发场景 | 进程退出码 |
| --- | --- | --- |
| `E_PARSE` | JSON/字段结构非法 | 20 |
| `E_TASK_DUPLICATE` | 任务 ID 重复 | 21 |
| `E_TASK_NOT_FOUND` | 任务不存在 | 22 |
| `E_TOPIC_INVALID` | 主题名非法 | 23 |
| `E_CLOCK_INVALID` | 时钟非非负整数 | 24 |
| `E_RETRIES_INVALID` | 最大重试次数非法 | 25 |
| `E_POLICY_NOT_FOUND` | 策略标签缺失 | 26 |
| `E_POLICY_INVALID` | 策略基数/上限非法 | 27 |
| `E_PAYLOAD_DIGEST_MISMATCH` | 摘要与载荷不一致 | 28 |
| `E_CLOCK_ROLLBACK` | 时钟回拨超过容差 | 29 |
| `E_MODE_VERSION_ROLLBACK` | 模式版本回退 | 30 |
| `E_PULL_NOT_FOUND` | 确认引用不存在的拉取 | 31 |
| `E_FOREIGN_CONSUMER` | 跨消费者确认 | 32 |
| `E_TASK_NOT_RUNNING` / `E_DEAD_LETTER` / `E_STATE_CORRUPT` / `E_VERIFY` | 状态类异常 | 33–36 |

单次异常只影响当前任务/命令，扫描循环内其他任务继续执行。

## 4. 验证结果

见 `VALIDATION.md`（含实际运行输出摘录）。
