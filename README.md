# 本地异构加速卡切分租约裁决器

本项目从零实现一个只依赖本地文件、SQLite 和 NumPy 的算力/显存双维切分租赁裁决组件。每张卡被拆成对齐的算力份额区间和显存份额区间；作业可在一张卡上获得一个切分租约，也可在允许跨卡时获得多个卡内切分租约。所有时间判断只使用注入的逻辑时钟。

## 快速开始

本机验证环境：

- Python `3.12.16`（项目声明要求 `>=3.11`）
- NumPy `1.26.4`
- SQLite `3.53.1`
- 未使用 Docker、网络服务或真实加速卡

```bash
uv venv .venv --python /home/zsl/.local/tools/python/bin/python3.12
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python scripts/generate_samples.py --out data
.venv/bin/python -m lease_arbiter load --force
.venv/bin/python -m lease_arbiter lease --round 1
.venv/bin/python -m lease_arbiter status
.venv/bin/python -m lease_arbiter verify
```

完整 10 轮验收（含续租、到期、抢占、碎片、四类异常）可直接运行：

```bash
.venv/bin/python scripts/run_demo.py
```

结果写入 `artifacts/demo_result.json`。默认运行态文件为 `var/ledger.db`、`var/ledger.pointer.json` 和 `reports/commit-*.json`。

## 目录

```text
lease_arbiter/
  validation.py   输入解析与独立错误码
  engine.py       双维授予、续租、回收、抢占、异常注入
  fragments.py    NumPy 槽位掩码、连续区间与碎片裁决
  ledger.py       SQLite 事务账本、文件锁、原子指针、恢复
  verify.py       区间不重叠/份额守恒/退款账目校验
  cli.py          load/lease/renew/preempt/resume/status/verify/recombine
  samplegen.py    确定性 JSON 样本生成器
scripts/
  generate_samples.py
  run_demo.py
tests/
  test_validation.py
  test_engine.py
  test_recovery.py
data/             四份本地确定性样本 + manifest
artifacts/        实际验收摘要
```

## 输入契约

四份 JSON 不依赖外部系统，均由 `lease_arbiter.samplegen` 确定性生成。

### 加速卡清单

```json
{
  "cards": [{
    "card_id": "card-a",
    "total_compute_shares": 64,
    "total_memory_shares": 48,
    "granularity": 4,
    "health": "healthy",
    "topology_domain": "node-a"
  }]
}
```

`health` 闭合为 `healthy/degraded/unhealthy`。`unhealthy` 不参与授予；`degraded` 在健康卡之后按卡 ID 确定性排序。`topology_domain` 被持久化并进入确定性排序依据，但本任务没有把它定义为硬亲和约束。

### 作业清单

```json
{
  "jobs": [{
    "job_id": "job-100",
    "compute_shares": 16,
    "memory_shares": 8,
    "lease_duration_ms": 12000,
    "priority": "normal",
    "quota_group": "research",
    "preemptible": true,
    "allow_cross_card": false,
    "arrival_round": 1
  }]
}
```

`priority` 闭合为 `low/normal/high`；`quota_group` 必须出现在配置的配额组中。`arrival_round` 是本地样本扩展，表示从哪个逻辑轮开始参与裁决，默认 1。

### 租赁配置

```json
{
  "quota_group_compute_limits": {"research": 100},
  "alignment_granularity": 4,
  "renewal_grace_ms": 3000,
  "fragmentation_threshold": 0.25,
  "clock_steps_ms": [1000, 5000, 10000],
  "clock_rollback_tolerance_ms": 0
}
```

轮次从 1 开始，第 `r` 轮使用 `clock_steps_ms[r-1]`。每张卡的生效对齐粒度是：

```text
lcm(card.granularity, config.alignment_granularity)
```

因此卡总量必须同时能被卡粒度和生效对齐粒度整除。作业需求不是对齐倍数时会向上取整到对齐位置，配额和卡占用按实际授予长度计算；这种情况在报告中保留原始需求与实际授予长度。

### 异常注入清单

注入项包含 `anomaly_id/stage/type/round/card_id/job_id/enabled/payload`。允许组合：

| stage | type | 错误码 |
|---|---|---|
| `grant` | `interval_overlap` | `ARB-ANOM-001` |
| `grant` / `reclaim` | `share_nonconservation` | `ARB-ANOM-002` |
| `renew` | `lease_generation_regression` | `ARB-ANOM-003` |
| `recombine` | `fragment_digest_mismatch` | `ARB-ANOM-004` |

注入是阶段内受控失败，不会写坏账本，也不会修改四份源 JSON。某个卡/作业的异常不会阻止其他卡继续授予、回收或重组；异常报告始终携带轮次和卡 ID。

## 裁决语义

### 授予

每份租约同时拥有：

- `compute_start/compute_length`
- `memory_start/memory_length`
- `alignment`

两个维度分别维护占用区间，同一卡同一维度不得重叠。授予依次检查：

1. 配额组剩余算力份额；
2. 目标卡剩余算力份额；
3. 目标卡剩余显存份额。

失败报告给出 `first_constraint`、`required`、`available`、`dimension`、`quota_group` 和 `card_id`。若总量足够但缺少完整对齐连续区间，标记为 `fragmentation`；若总量本身就小于需求，标记为 `no_capacity`。

单卡作业先尝试单卡；允许跨卡的作业在单卡失败后按卡 ID/健康状态确定性生成多个卡内租约。每个跨卡租约仍是“算力 + 显存”成对份额，不会把不同卡的地址空间拼接成同一连续区间。

### 续租与到期

续租必须同时匹配 `lease_id`、`job_id`、`generation`。判断条件仅为：

```text
clock_ms <= expires_at_ms + renewal_grace_ms
```

成功后续租代数加 1，新到期时间为 `clock_ms + lease_duration_ms`。只有严格满足：

```text
clock_ms > expires_at_ms + renewal_grace_ms
```

的租约才进入 `expired`，释放双维区间并回退配额计数。逻辑时钟回拨超过 `clock_rollback_tolerance_ms` 返回 `ARB-RT-001`；容差内回拨会被钳制到水位，不产生时间倒退。

### 抢占与退款

`preempt` 只考虑优先级严格低于目标作业、且 `preemptible=true` 的活动租约所属作业。候选按：

```text
优先级升序 -> 最早剩余时长升序 -> job_id
```

推演会逐步回收并尝试重新授予，然后裁掉非必要回收项，输出最小必要 `selection_chain`、全部 `evaluated` 判定、最终约束和是否可授予。只有 `--apply` 且推演成功时才提交回收与授予。

被抢占租约生成唯一 `pending` 退款记录。整数公式为：

```text
elapsed = max(0, min(clock_ms, expires_at_ms) - granted_clock_ms)
consumed = ceil(allocated_shares * elapsed / lease_duration_ms)
refund = allocated_shares - consumed
```

账本覆盖 `refund_id` 唯一约束和 `lease_id` 唯一约束，因此重复提交不会重复登记退款。

### 碎片重组

`fragments.py` 使用 NumPy 布尔槽位掩码标记卡内占用，再用边界差分找出空闲连续段。相邻对齐段可合并；跨 gap、跨卡不重排、不借用地址。报告包含：

- 每张卡的算力/显存碎片分布；
- 最长连续长度与总空闲量；
- `fragmentation_score`、配置阈值和 `threshold_exceeded`；
- 关键失败维度与建议的碎片拼接顺序；
- 单卡/跨卡对齐假设。

`fragmentation_threshold` 用于标记碎片严重程度，不改变“当前是否可授予”的事实判定。

## 持久化、指针与恢复

SQLite 表覆盖：`cards`、`jobs`、`quota_groups`、`leases`、`occupancy_ranges`、`occupancy_slots`、`refunds`、`fragments`、`rounds`、`events`、`injections`、`anomaly_fires`、`recovery_log`、`meta`。

关键机制：

- 文件锁：`var/ledger.db.lock` 使用 `fcntl.flock` 跨进程串行化。
- SQLite：`BEGIN IMMEDIATE`、WAL、`synchronous=FULL`、业务约束和唯一槽位。
- 每卡每维度的 `occupancy_slots(card_id, dimension, slot_index)` 主键从数据库层阻止同一对齐槽被授予两次。
- 配额更新是带 `consumed_compute + ? <= limit_compute` 条件的单条 `UPDATE`。
- 每轮生成独立报告摘要；`meta.ledger_version`、所有业务表、轮次记录和逻辑时钟水位在同一事务提交。
- 提交后原子写入 `reports/commit-*.json`，再通过临时文件 + `fsync` + `os.replace` 整体替换账本指针。
- `resume` 先按数据库轮次修复缺失报告和过期指针；只对未出现在 `rounds` 中的轮次执行裁决。已提交轮次直接返回原报告，不重复扣减或退款。

相同的提交键 `lease:{round}`、`preempt:{round}:{job}` 是幂等键。重复提交返回数据库中的原始报告和摘要。

## CLI

全局参数需放在子命令前：

```bash
python -m lease_arbiter --db var/ledger.db \
  --pointer var/ledger.pointer.json --reports reports <command>
```

| 子命令 | 作用 |
|---|---|
| `load` | 全量校验后装载四份 JSON；非法输入不创建数据库/指针 |
| `lease --round N` | 执行一轮授予、自动到期回收；可附加 `--renew LEASE:JOB:GEN` |
| `renew` | 在已提交的逻辑轮上单独续租；未提交轮次应使用 `lease --renew` |
| `preempt` | 默认提交最小回收链并授予；加 `--dry-run` 只推演 |
| `resume` | 修复指针/报告并重放未提交轮次 |
| `status` | 查看版本、活动租约、等待作业、配额、退款与异常 |
| `verify` | 校验重叠、对齐、份额守恒、退款账目、报告摘要与指针 |
| `recombine` | 对指定等待作业执行碎片重组分析 |

常用完整流程：

```bash
python -m lease_arbiter load --force
python -m lease_arbiter lease --round 1
python -m lease_arbiter lease --round 2
python -m lease_arbiter lease --round 3
python -m lease_arbiter preempt --round 3 --job-id job-900
python -m lease_arbiter status
python -m lease_arbiter verify
```

## 错误码

### 输入校验

| 错误码 | 含义 |
|---|---|
| `ARB-VAL-001` | JSON/顶层结构非法 |
| `ARB-VAL-002` | 卡 ID 重复 |
| `ARB-VAL-003` | 总算力份额非法 |
| `ARB-VAL-004` | 总显存份额非法 |
| `ARB-VAL-005` | 卡切分粒度非法 |
| `ARB-VAL-006` | 卡总量不能被切分粒度整除 |
| `ARB-VAL-007` | 健康状态非法 |
| `ARB-VAL-008` | 作业 ID 重复 |
| `ARB-VAL-009` | 作业需求份额非法 |
| `ARB-VAL-010` | 租约时长非法 |
| `ARB-VAL-011` | 优先级非法 |
| `ARB-VAL-012` | 配额组非法 |
| `ARB-VAL-013` | 可抢占/跨卡/到达轮次字段非法 |
| `ARB-VAL-014` | 不可跨卡作业超过所有单卡对齐容量 |
| `ARB-VAL-015` | 配置字段非法 |
| `ARB-VAL-016` | 逻辑时钟步进非法 |
| `ARB-VAL-017` | 异常注入条目非法 |
| `ARB-VAL-018` | 异常引用不存在 |
| `ARB-VAL-019` | 卡总量不能被生效对齐粒度整除 |
| `ARB-VAL-020` | 续租宽限不小于租约时长 |

### 运行时与异常

| 错误码 | 含义 |
|---|---|
| `ARB-RT-001` | 逻辑时钟回拨超容差 |
| `ARB-RT-002` | 轮次越界 |
| `ARB-RT-003` | 幂等键冲突 |
| `ARB-RT-004` | 租约不存在 |
| `ARB-RT-005` | 续租作业身份不匹配 |
| `ARB-RT-006` | 续租代数不匹配 |
| `ARB-RT-007` | 租约已过期，不能续租 |
| `ARB-RT-008` | 作业状态不允许操作 |
| `ARB-RT-009` | 抢占不被允许 |
| `ARB-RT-010` | 账本尚未装载 |
| `ARB-RT-011` | 文件锁等待超时 |
| `ARB-RT-012` | 账本一致性失败 |
| `ARB-RT-013` | 协议参数非法 |
| `ARB-ANOM-001` | 注入切分区间重叠 |
| `ARB-ANOM-002` | 注入账本份额不守恒 |
| `ARB-ANOM-003` | 注入续租代数回退 |
| `ARB-ANOM-004` | 注入碎片重组摘要不匹配 |

## 实际验证结果

执行：

```bash
.venv/bin/python -m unittest discover -v
.venv/bin/python scripts/run_demo.py
```

本次结果：

- 自动化测试 20 项全部通过，覆盖独立校验错误码、非法 load 零写入、双维授予、单卡/跨卡、配额/显存首约束、续租、独立续租、到期、抢占最小链、退款幂等、碎片分类、四类异常隔离、逻辑时钟回拨、崩溃前/后恢复、5 进程同轮并发。
- 样本演示完成 11 次提交（10 个逻辑轮 + 1 个抢占提交），最终 `verify` 的 59 项检查全部通过，`failure_count=0`。
- 抢占链仅包含 `job-700`；其退款记录为 `pending`，目标 `job-900` 成功授予。
- 第 8 轮 `job-1100` 被判定为 `fragmentation`；卡 B/C 的总算力空闲量足够，但最长连续区间均不足 48 份。
- 第 10 轮同时命中 `ARB-ANOM-001/002/003/004`；`job-1200` 被卡 A 注入异常阻断，但 `job-1201` 在卡 B 正常授予。
- 样本源文件哈希在运行前后完全一致；`artifacts/demo_result.json` 保存逐轮摘要。

验证辅助环境（仅测试用）：

```text
LEASE_ARBITER_TEST_CRASH_AFTER_COMMIT=1   提交后、发布报告/指针前强制退出
LEASE_ARBITER_TEST_CRASH_BEFORE_COMMIT=1  事务提交前强制退出
```

## 假设与边界

- 本组件面向本地单机 SQLite 文件和多进程仲裁，不提供网络服务或分布式共识。
- `topology_domain` 当前只作为元数据和确定性排序信息，不作为网络拓扑硬约束。
- 双维区间按各自维度检查不重叠；算力区间和显存区间可以起始位置不同。
- 需求向上对齐可能产生少量超额占用，报告保留原始需求与授予长度。
- 碎片候选只从权威活动占用映射重建；`fragments` 表保留历史回收来源和摘要，不替代占用表。
- 不修改样本源文件；所有运行态数据均写入显式指定的 SQLite、指针和报告路径。
