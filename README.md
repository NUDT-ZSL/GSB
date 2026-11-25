# 本地 GPU 调度与配额引擎

一个不依赖 Docker、可在 WSL Ubuntu 本地复现的异构 GPU 切分、配额、租约、碎片重组、抢占推演和崩溃恢复引擎。核心算法为纯 Python，持久化使用标准库 `sqlite3`，固定样本生成使用 `numpy` 的固定种子随机数。

## 快速开始

要求 Python 3.10+。

```bash
python3 -m pip install -r requirements.txt
python3 generator.py
python3 scripts/run_validation.py
```

一键脚本会：

- 重新生成 `data/samples/cards.json`、`jobs.json`、`config.json`、`inject.json`；
- 重建 `results/scheduler.db`；
- 跑通授予、幂等重放、续租代数、到期回收、碎片重组、抢占执行、配额收缩和四类异常注入；
- 用真实子进程验证 9 个 SIGKILL 崩溃点；
- 用多进程验证同卡竞争和重复请求；
- 写出 `results/status.json`、`results/verify.json`、`results/recovery-evidence.json`、`results/concurrency-evidence.json` 和 `results/validation-summary.json`。

也可以直接运行测试：

```bash
python3 -m unittest tests.test_scheduler -v
```

## 常用 CLI

全局参数 `--db` 和 `--samples` 需放在子命令前：

```bash
python3 -m gpu_scheduler.cli --db results/scheduler.db load

python3 -m gpu_scheduler.cli --db results/scheduler.db \
  grant --round grant-frag-01 --now 100 \
  --job job-frag-01 --card gpu-frag

python3 -m gpu_scheduler.cli --db results/scheduler.db \
  renew --round renew-ok --now 150 \
  --job job-frag-01 --generation 1

python3 -m gpu_scheduler.cli --db results/scheduler.db \
  recover --round recover-expired --now 300

python3 -m gpu_scheduler.cli --db results/scheduler.db \
  reorganize --round reorg-ok --now 300 \
  --job job-frag-target --card gpu-frag

python3 -m gpu_scheduler.cli --db results/scheduler.db \
  preempt --job job-high-priority --now 300

python3 -m gpu_scheduler.cli --db results/scheduler.db \
  preempt --job job-high-priority --now 300 \
  --execute --round preempt-exec --plan-digest <PLAN_DIGEST>

GPU_SCHEDULER_CRASH_AT=grant:after_intent \
  python3 -m gpu_scheduler.cli --db results/scheduler.db \
  grant --round crash --now 100 --job job-frag-01

python3 -m gpu_scheduler.cli --db results/scheduler.db resume
python3 -m gpu_scheduler.cli --db results/scheduler.db status --output results/status.json
python3 -m gpu_scheduler.cli --db results/scheduler.db verify --output results/verify.json
python3 -m gpu_scheduler.cli --db results/scheduler.db verify --self-test
```

支持的子命令为 `load`、`grant`、`renew`、`recover`、`preempt`、`resume`、`status`、`verify`，并额外暴露 `reorganize`。

## 确定性样本设计

固定种子为 `20261126`。关键样本不是随机边界，而是显式构造的可诊断场景：

- `gpu-frag`：100 算力 / 100 显存 / 粒度 10。`job-frag-01..05` 各占 20；到期回收 `02`、`04` 后留下两个长度 20 的算力和显存碎片，`job-frag-target` 需 30/30，结论为“碎片导致不可直接满足，1 次迁移后可满足”。
- `gpu-preempt`：`job-prey-a/b/c` 占满卡；高优先级 `job-high-priority` 需 60/70。按优先级和剩余时长先选 `job-prey-b`，再选 `job-prey-a`，回收后得到 `[0,60]` 算力区间和 `[0,70]` 显存区间。
- `gpu-small`：60 算力 / 60 显存，由 `job-small-fill` 占满，避免高优先级作业绕过推演直接落到其他卡。
- `gpu-mp`：40/40 的并发竞争卡，多个 20/20 作业中只能有两个活动授予。
- `gpu-locked`：`splittable=false`，任何指定该卡的授予均得到 `card_unsplittable`。
- `gpu-dead`：`health=unhealthy`，不参与调度。
- `job-quota-blocked`、`job-compute-too-big`、`job-memory-too-big` 分别覆盖配额算力、卡算力和卡显存约束。

## 裁决与状态模型

- 配额：`config.json` 中的 `quota_groups.<group>.compute_limit` 是第一重约束；账本同时记录算力和显存已消耗份额。
- 切分：每张卡分别在线性算力空间和显存空间中按 `granularity` 对齐查找最低可用区间；授予结果包含 `compute_interval` 和 `memory_interval`。
- 租约：首次授予 `generation=1`；续租校验 `job_id + lease_id + generation`，成功后代数加一。续租宽限由 `renew_grace_ms` 控制。
- 回收原因：`normal_expiry`、`quota_shrink`、`preempted`。每条记录包含回收逻辑时刻、释放区间、份额数、配额组和影响范围。
- 重组：输出算力/显存空闲碎片、最大碎片、总空闲量、是否真无容量、是否仅碎片不足，以及最少迁移子集和迁移目标区间。
- 抢占：先纯函数推演，不写账本；计划包含完整受害者链、每一步授予裁决、最终区间、计划摘要和未结退还记录。`--execute --plan-digest` 会重新推演并强制执行结果与摘要一致。

## SQLite 与崩溃恢复

数据库主要表：

- `ledger_versions`：每个已提交轮次的完整快照、快照摘要和哈希链；
- `meta.ledger_version`：当前账本指针，与新版本在同一事务中更新；
- `operations`：幂等键、请求哈希、`planned/applied/completed/failed` 阶段和结果；
- `idempotent_results`：相同 `round + action + job/批次` 的稳定结果；
- `occupancy_intervals`：当前版本的关系型区间占用映射；
- `refunds`：抢占产生的未结退还记录；
- `injection_runs`：异常注入消费标记和实际错误码；
- `audit_events`：持久化审计日志。

写入管道采用三阶段：

1. 独立事务写入 `operations(status='planned', stage='intent')`；
2. `BEGIN IMMEDIATE` 后基于当前快照执行纯规划、不变量校验、插入新版本/指针/占用/幂等结果，并把操作推进到 `applied/stage='ledger_committed'`，随后原子提交；
3. 提交后把操作收尾为 `completed/stage='done'`。

`GPU_SCHEDULER_CRASH_AT` 可在 `grant:*`、`renew:*`、`recover:*`、`reorganize:*` 的 `after_intent`、`before_commit`、`after_commit` 点发送真实 `SIGKILL`。恢复时：

- `planned`：账本事务未提交，按持久化请求确定性重放；
- `applied`：账本版本和幂等结果已提交，只补做完成标记，不重复应用业务效果。

多进程使用 `results/scheduler.db.lock` 上的 `fcntl` 排他文件锁，并叠加 SQLite `BEGIN IMMEDIATE`、WAL、`busy_timeout` 和幂等主键。相同请求键只有一个进程执行状态推进，其他进程读取同一结果；不同作业被串行化裁决，因此不会超卖或重复授予同一活动作业。

## 异常与业务错误码

| 场景 | 稳定错误码 | 退出码 |
| --- | --- | ---: |
| 输入校验失败 | `E_VALIDATION_FAILED` 及 `E_DUPLICATE_CARD_ID` 等细分码 | 20 |
| 租约不存在 | `E_ACTIVE_LEASE_NOT_FOUND` | 21 |
| 代数不匹配 | `E_GENERATION_MISMATCH` | 49 |
| 逻辑时钟回退 | `E_LOGICAL_CLOCK_BACKWARD` | 42 |
| 超过续租宽限 | `E_LEASE_EXPIRED_BEYOND_GRACE` | 48 |
| 抢占链不可行 | `E_PREEMPTION_NOT_FEASIBLE` | 47 |
| 推演/执行不一致 | `E_PREEMPT_EXECUTION_MISMATCH` | 50 |
| 注入区间重叠 | `E_INJECT_INTERVAL_OVERLAP` | 58 |
| 注入账本失衡 | `E_INJECT_LEDGER_IMBALANCE` | 58 |
| 注入续租代数回退 | `E_INJECT_GENERATION_ROLLBACK` | 58 |
| 注入重组摘要不匹配 | `E_INJECT_REORG_DIGEST_MISMATCH` | 58 |

配额或资源不足不是命令失败，而是已提交/可重放的授予裁决：JSON 中 `granted=false`，并带 `constraint=quota_compute|card_compute|card_memory|...`、已用/上限/请求值和逐卡尝试信息。

## `verify` 校验内容

`python3 -m gpu_scheduler.cli ... verify` 会：

- 从 `ledger_versions` 重算每个快照摘要和父子哈希链；
- 核对 `meta` 指针等于最新版本；
- 检查区间边界、真实重叠、占用份额合计、配额已用份额和活动作业映射；
- 核对关系型 `occupancy_intervals` 与快照一致；
- 重放所有 `idempotent_results` 对应请求，确认结果摘要一致且版本号不变；
- 检查四类注入均有运行记录、错误码正确且失败注入没有产生账本版本；
- `--self-test` 另建两个临时库执行同一确定性核心链，比较最终状态摘要。

## 最近一次本地验证

以下产物由 `python3 scripts/run_validation.py` 实际生成：

- `results/verify.json`：5 项当前账本检查全部 `ok=true`；
- `results/recovery-evidence.json`：9 个强制终止/恢复用例全部通过；
- `results/concurrency-evidence.json`：6 个重复提交只产生 1 个租约，竞争卡上恰好 2 个活动租约，算力和显存均为 40/40；
- `results/validation-summary.json`：端到端总结果 `ok=true`；
- `python3 -m unittest tests.test_scheduler`：14 个测试全部通过。

## 作为算法库集成

```python
from pathlib import Path
from gpu_scheduler import SchedulerEngine, SchedulerStore
from gpu_scheduler.models import load_bundle

bundle = load_bundle(Path("data/samples"))
engine = SchedulerEngine(SchedulerStore("results/scheduler.db"), bundle)
engine.initialize_bundle()
decision = engine.submit({
    "action": "grant",
    "stage": "grant",
    "round_id": "integrate-1",
    "now_ms": 100,
    "job_id": "job-frag-01",
})
```

纯规划函数位于 `gpu_scheduler/planner.py`，可脱离 SQLite 直接用于搜索、重组和抢占算法集成。
