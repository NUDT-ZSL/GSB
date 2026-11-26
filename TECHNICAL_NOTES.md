# 受控混沌预算与恢复一致性：技术说明

本交付是纯本地 Python 3.10+ 领域组件和命令行示例。运行时只使用标准库
（`sqlite3`、`fcntl`、`hashlib`、`argparse`），不访问网络、真实集群或外部服务；
`numpy` 仅由固定种子样本生成器使用。

## 快速运行

```bash
python -m pip install --user pytest numpy
mkdir -p sample_data
python generate_samples.py
python main.py status

# 完整可重复演示（等价于 tests 中的清单工作流）
bash scripts/demo.sh

# 不依赖已有数据库，也可直接运行内存级完整样本推演
python main.py verify --ephemeral

python -m pytest -q
```

默认状态库为 `runtime/chaos_state.sqlite3`，文件锁为
`runtime/chaos_state.sqlite3.lock`。所有 CLI 输出均为 JSON，并使用
`sort_keys=True`、实验 ID 升序和规范 JSON 哈希保证稳定。

## 子命令

- `load [--reset] [--clock MS]`：先完整校验全部输入，通过后才创建/写入 SQLite。
- `run --clock MS [--experiment ID] [--round-id R] [--include-recovered]`：按优先级、
  启动时钟和实验 ID 稳定排序并裁决准入。
- `observe [--events FILE | --event-json JSON] [--clock MS]`：校验、收件箱去重、
  驱动状态迁移并进行预算结算。
- 对生成样本直接运行无参数 `observe` 时，会幂等执行 `manifest.json.workflow`
  中的完整时钟编排；分步演示仍建议显式传入 `--clock`。
- `resume [--clock MS] [--experiment ID]`：重放 `received` 半处理收件箱，
  并可补做崩溃前缺失的准入。
- `status`：稳定排序 JSON，包含每个实验状态、剩余预算、退避时刻、代数和决策摘要。
- `verify [--ephemeral] [--strict]`：校验 SQLite 完整性、预算恒等式、决策哈希链、
  准入版本唯一、收件箱无积压和输入指纹。

## 输入与业务错误码

输入文件位于 `sample_data/`：

- `experiments.json`：实验计划。
- `budget_config.json`：每实验额度、每轮逻辑时间预算、并发上限、退避基数/上限。
- `observations.json`：卡点、完成、失败、恢复事件流。
- `policy_v1.json`：v1 单文件策略快照。
- `policy_v2_shard_00.json`、`policy_v2_shard_01.json`、
  `policy_v2_metadata.sqlite3`：v2 分片和 SQLite 元数据。
- `manifest.json`：固定种子、注入时钟和演示步骤。

常见错误码：

- `INPUT_NOT_FOUND` / `MALFORMED_JSON` / `SCHEMA_VIOLATION`
- `VALIDATION_FAILED`，其 `details.issues[]` 含字段路径和具体规则码
- `DUPLICATE_EXPERIMENT_ID`
- `INVALID_NON_NEGATIVE_INTEGER` / `INVALID_POSITIVE_INTEGER`
- `EVENT_SEQUENCE_NOT_STRICTLY_INCREASING`
- `SUMMARY_MISMATCH` / `SUMMARY_DIGEST_MISMATCH`
- `UNKNOWN_EXPERIMENT` / `OBSERVATION_ORDER_CONFLICT`
- `IDEMPOTENCY_CONFLICT`
- `CONCURRENCY_LIMIT_EXHAUSTED`
- `EXPERIMENT_BUDGET_INSUFFICIENT`
- `ROUND_BUDGET_INSUFFICIENT`
- `INVALID_TRANSITION` / `GENERATION_REQUIRED` / `GENERATION_MISMATCH`
- `IDENTITY_MISMATCH`
- `STATE_ALREADY_EXISTS` / `STATE_NOT_FOUND` / `VERIFICATION_FAILED`
- `LOCK_TIMEOUT`

`load` 在 `load_bundle()` 完成全部校验后才获取文件锁或连接目标状态库，因此畸形输入
不会创建业务 SQLite 状态。

观测摘要契约为确定性规范字符串：

```text
<experiment_id>#<sequence>:<normalized_type>@logical_ms=<clock_ms>:<canonical_detail_json>
```

也支持可选 `summary_sha256`，它是规范声明对象
`{experiment_id, sequence, observation_type, clock_ms, generation, target_service, detail}`
的 SHA-256。

## 策略合并和时间换算

每个策略键的候选版本按以下全序选择最大值：

```text
(declared_logical_clock_ms, generation, source_format_priority)
```

来源格式优先级为：

- `v1_json = 10`
- `v2_shard_sqlite = 20`

因此：

- 时钟不同：高声明逻辑时钟胜出。
- 时钟相同但代数不同：更高策略代数胜出。
- 时钟和代数都相同：v2 分片/SQLite 来源优先级胜出。

每个键的 `selection_reason`、候选数量、来源文件和仲裁哈希都写入 `policy_view` 表和
`status.policy_view`。

v1 的 `effective_relative_seconds` 使用 `Decimal` 换算，避免浮点歧义。样本值
`0.2509` 秒在注入时钟 `1000ms` 下采用向上取整：

```text
scaled = 0.2509 * 1000 = 250.9ms
rounded = ceil(250.9) = 251ms
effective_absolute_clock_ms = 1000 + 251 = 1251ms
rounding_direction = ceil_to_positive_infinity
scale = 1000
```

## 状态机

持久化状态为：

- `pending_admission` / 待准入
- `running` / 执行中
- `blocked_waiting` / 卡点等待
- `completed` / 已完成
- `failed` / 已失败
- `recovered` / 已恢复
- `abandoned` / 主动放弃（调度结局扩展状态）

合法迁移：

```text
pending_admission -> running
running           -> blocked_waiting | completed | failed
blocked_waiting   -> running | failed
failed            -> recovered
recovered         -> running（resume/readmit，generation + 1）
pending/recovered -> abandoned（调度约束触发）
```

所有迁移只使用命令传入的注入逻辑时钟、事件时钟和 SQLite 持久化状态。锁等待可使用
`time.monotonic()` 实现超时，但它不参与任何状态、预算、退避或证据哈希。

## 预算和调度规则

预算单位为整数 points；逻辑时间账户单位为 `logical_ms`。

准入时：

1. 检查活动实验数是否小于全局并发上限。
2. 检查实验 points 账户是否足够支付
   `expected_duration_ms * cost_per_ms + admission_charge_points`。
3. 检查轮次逻辑时间账户是否足够预扣 `expected_duration_ms`。
4. 在同一个 `BEGIN IMMEDIATE` 事务内写决策序号、预算分录、准入版本和实验状态。

终态事件按实际经过逻辑时间结算：

```text
elapsed_ms = max(0, terminal_clock_ms - admitted_clock_ms)
billable_ms = min(reserved_round_ms, elapsed_ms)
refund_points = reserved_points - billable_ms * cost_per_ms
round_refund_ms = reserved_round_ms - billable_ms
```

每次状态推进还会扣除统一策略中的 `phase_charge_points`（样本中 v2 仲裁为 3）。
恢复事件按 `recovery_restore_bps` 返还最近一次失败计费的一定比例，并按
`recovery_fee_points` 扣除恢复费用；所有返还/扣费都在恢复状态和决策的同一事务内。

账户恒等式由 `verify` 全量重算：

```text
balance = initial_balance + SUM(ledger.delta_amount)
total_balance = total_initial - total_debited + total_credited
balance >= 0
```

调度排序键为：

```text
(priority ASC, start_logical_clock_ms ASC, experiment_id ASC)
```

调度器记录每个候选的三项约束检查和第一个失败约束。遇到第一个不可准入候选时，该候选及
本轮剩余到期候选被标记为 `voluntarily_abandoned`，并共享同一触发约束证据。未到期或
仍在退避窗口的候选保持 `within_budget_not_started`。

样本最终覆盖：

- `within_budget_not_started`: `EXP-011`
- `failed_after_start`: `EXP-006`
- `voluntarily_abandoned`: `EXP-003`, `EXP-004`, `EXP-008`
- 另外保留 completed、blocked、recovered、running 状态用于展示完整状态机。

## 退避和确定性抖动

失败时连续失败次数加一，退避计算为：

```text
exponential = min(cap_ms, base_ms * 2 ** (consecutive_failures - 1))
jitter_ms = sha256_int(canonical_material) mod base_ms
backoff_ms = min(cap_ms, exponential + jitter_ms)
next_start_clock_ms = failure_clock_ms + backoff_ms
```

哈希材料包含实验 ID、连续失败次数、失败时钟和历史代数，因此无随机全局状态，跨进程和
重复运行结果一致。

## 幂等、证据链和崩溃恢复

- `observation_inbox` 以 `(experiment_id, sequence)` 为主键，并保存规范请求哈希。
- 相同键、相同内容重放时返回原决策序号和原证据哈希，不产生第二次预算分录。
- 相同键、不同内容返回 `IDEMPOTENCY_CONFLICT`。
- `admissions` 以 `(experiment_id, generation)` 为主键，保证同一实验同一版本只准入一次。
- `decisions.idempotency_key` 和 `budget_ledger.idempotency_key` 提供额外唯一约束。
- 每个决策取得单调连续序号，并保存 `prev_hash` 与
  `evidence_hash = SHA256(canonical_json(evidence_without_hash))`。
- `verify` 重算全部哈希、前向链接和预算分录引用。

SQLite 使用 `journal_mode=WAL`、`synchronous=FULL`、`foreign_keys=ON` 和
`busy_timeout=30000`。状态、余额、决策序号、退避截止和时钟水位在同一
`BEGIN IMMEDIATE` 事务内提交；事务中途被 `SIGKILL` 时 SQLite 回滚，不会留下半条预算或
决策记录。

为了显式识别“已收件但未处理”的半推进记录，观测提交分为：

1. 事务 A：校验实验和幂等键，写入 `observation_inbox.status='received'` 并提交。
2. 事务 B：完成状态、预算、决策、水位和 `status='processed'`。

若进程在 A 后、B 提交前被杀死，数据库保留 `received`；`resume` 按
`(experiment_id, sequence)` 顺序只重放这些记录。若 B 已提交，重放直接返回原结果，
不改变既有决策哈希链。

`chaos_budget.crash.maybe_kill()` 支持测试钩子：

- `CHAOS_CRASH_STAGE=after_inbox_commit`
- `CHAOS_CRASH_STAGE=before_admission_commit`
- `CHAOS_CRASH_STAGE=after_admission_commit`
- `CHAOS_CRASH_STAGE=after_process_commit`

钩子发送真实 `SIGKILL`，同时验证操作系统会释放 `fcntl` 锁。

## 多进程并发

所有写 CLI 命令先获取 `runtime/chaos_state.sqlite3.lock` 上的排他 `flock`，再进入
SQLite `BEGIN IMMEDIATE`。进程被杀死时文件锁由内核自动释放。准入事务内部还会重新执行
约束裁决并由 `balance >= 0` 检查约束兜底，因此即使绕过 CLI 文件锁直接使用 Engine，
也不会预算超扣。

测试中启动多个真实 Python 子进程：

- 4 个进程分别准入不同实验：恰好 4 条准入。
- 8 个进程并发准入同一实验同一版本：恰好 1 条准入，其余幂等返回。
- 4 个进程竞争仅够 1 个实验的轮次预算：1 个成功、3 个记录
  `ROUND_BUDGET_INSUFFICIENT`，余额为 0 且恒等式通过。

## 实际验证结果

验证环境：

- Python 3.10.12
- pytest 8.2.0
- numpy 1.26.4
- Linux/WSL Ubuntu

当前验证用托管 Python 禁用了 user-site，因此 `python -m pip install --user ...`
会提示 “User site-packages are not visible in this virtualenv”；该环境已经预装所需
依赖。普通 WSL Ubuntu 系统 Python 可按题目原文使用 `--user`，托管/虚拟环境中可改用
`python -m pip install pytest numpy`。

已执行：

```text
python -m py_compile main.py generate_samples.py chaos_budget/*.py
bash scripts/demo.sh
python -m pytest -q
```

结果：

```text
DEMO_RC=0
19 passed
```

持久化样本最终状态：

```json
{
  "clock_watermark_ms": 1800,
  "decision_count": 25,
  "account_count": 12,
  "states": {
    "abandoned": 3,
    "blocked_waiting": 1,
    "completed": 3,
    "failed": 1,
    "pending_admission": 1,
    "recovered": 1,
    "running": 1
  },
  "total_initial_balance": 11700,
  "total_debited": 1660,
  "total_credited": 302,
  "total_balance": 10342
}
```

`verify` 的 9 项检查全部通过：

```text
sqlite_integrity
foreign_key_integrity
no_negative_balance
budget_ledger_conservation
decision_hash_chain
ledger_decision_foreign_key
admission_version_uniqueness
no_unresolved_inbox
input_fingerprint
```

测试文件：

- `tests/test_contracts_policy.py`：校验、错误码、无持久化写入、策略仲裁、稳定状态。
- `tests/test_workflow_idempotency.py`：完整工作流、确定性、幂等、代数冲突、CLI、
  裸 `observe` 自动编排。
- `tests/test_multiprocess_and_crash.py`：真实子进程并发、预算竞争、SIGKILL 恢复。
