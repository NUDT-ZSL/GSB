# 本地弹性容器实例冷启动配额账本

这是一个不依赖 Docker、真实集群或网络服务的确定性冷启动裁决引擎。它把实例准入、镜像层读取预计耗时、临时存储写入额度、启动阶段、抢占回收、单轮调度和恢复水位统一写入 SQLite 版本账本。所有业务时间均来自调用方注入的逻辑时钟。

## 快速开始

要求 Python 3.11+。工作区根目录已有四份样本：`instances.json`、`layers.json`、`nodes.json`、`config.json`。

Linux/macOS：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
.venv/bin/python scripts/run_acceptance.py
```

Windows PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\run_acceptance.py
```

运行样本轮次：

```bash
.venv/bin/python -m coldstart_ledger --db coldstart.db run-round --round-id r1 --now-ms 6
.venv/bin/python -m coldstart_ledger --db coldstart.db audit
.venv/bin/python -m coldstart_ledger --db coldstart.db plan-preemption --instance-id api-batch --now-ms 7
.venv/bin/python -m coldstart_ledger --db coldstart.db recover
```

样本在 `now_ms=6` 时准入 `api-fast`、`worker-late`、`edge-one`；`api-batch`、`worker-low`、`edge-two` 因节点并发/容量未启动。所有节点余额通过守恒审计。

## Python API

```python
from coldstart_ledger import ColdStartEngine, load_inputs

bundle = load_inputs(".")
engine = ColdStartEngine("ledger.db", bundle)

round_result = engine.run_round("r1", now_ms=6)
engine.complete_reading(
    "api-fast", "r1",
    ["sha256:layer-api", "sha256:layer-base"],
    now_ms=10,
)
engine.complete_writing("api-fast", "r1", now_ms=12)

plan = engine.plan_preemption("api-batch", now_ms=13)
assert engine.audit()["ok"]
snapshot = engine.recover()
```

主要入口：

- `run_round(round_id, now_ms)`：按节点并发推进一轮，返回准入、未启动、失败、放弃分类及调度时隙。
- `admit`、`complete_reading`、`complete_writing`：显式推进状态机。
- `fail_instance`、`abandon_instance`、`withdraw_instance`：启动后失败、主动放弃和撤回。
- `plan_preemption`、`settle_refund`：只读推演回收链及结算退还未结记录。
- `apply_layer_change`：层字节变更并撤回所有引用该层的在途实例。
- `audit`、`snapshot`、`recover`：守恒审计、全量快照和强制终止后恢复。

## 数据格式

配置至少包含：

```json
{
  "temp_storage_quota_limit_bytes": 20000,
  "layer_read_bandwidth_bytes_per_second": 1000000,
  "round_logical_time_budget_ms": 40,
  "fixed_scale": 1000,
  "error_upper_bound_budget": 8,
  "logical_clock_step_ms": 5,
  "comparison_tolerance_ticks": 2,
  "clock_rollback_threshold_ms": 0
}
```

实例的层集合必须全部托管在同一个节点；节点由层托管清单推导。实例 ID、层摘要、节点名唯一。实例/层/节点字段也接受常见别名（如 `startup_deadline_ms`、`layer_bytes`、`available_temp_storage_bytes`）。

## 核心语义

### 状态机

阶段仅使用：

- `PENDING_APPROVAL`
- `READING_LAYERS`
- `WRITING_EPHEMERAL`
- `READY`
- `WITHDRAWN`

截止判定只比较注入的 `now_ms` 与 `deadline_ms`。`now_ms > deadline_ms` 的待准入实例直接撤回，不进入读层。到期撤回、读层失败、启动后失败、主动放弃、抢占分别使用不同结局码，例如 `DEADLINE_MISSED`、`READ_LAYER_FAILED`、`FAILED_AFTER_START`、`ABANDONED`、`PREEMPTED`。

### 定点换算

读层换算的分母是带宽：

```text
ticks = floor((layer_bytes * 1000 * fixed_scale + carry) / bandwidth)
carry_out = (layer_bytes * 1000 * fixed_scale + carry) % bandwidth
ticks = ticks / fixed_scale   # 逻辑毫秒
```

写入额度为 `requested_ephemeral_bytes * fixed_scale`。每次换算登记序号、标度、方向 `floor`、被舍弃余量、输入/有效分子、分母和商。实例范围与节点范围使用独立的余量键，后续换算会携带各自的余量。

高精度对照通路使用 `decimal.Decimal` 计算同样的输入。每个实例的误差账本保存定点值、Decimal 值、绝对偏差、累计偏差上界和来源链（实例、层摘要、换算序号、余量）。被评估但未准入的候选也会保留 `charged=false` 的报价误差记录。

### 事务与幂等

SQLite 表：

- `ledger_versions`：不可变全量状态版本，含父版本和 SHA-256。
- `current_ledger`：单行当前版本指针。
- `idempotency`：`(instance_id, round_id, source)` 唯一键、请求哈希和结果。

每次操作使用 `BEGIN IMMEDIATE`，在同一事务内读取当前指针、变更配额、插入新版本、校验和更新当前指针。并发进程被串行化，不会双重扣减；重复提交相同键和请求直接返回原结果。`crash_hook` 可模拟版本行写入后、指针切换前或提交前强制终止，恢复时只能看到完整旧版本。

状态快照包含轮次水位、逻辑时钟水位、实例阶段、已提交层摘要、节点可用存储与并发占用、余量保留表、换算登记、退还未结记录、精度标记和轮次历史。

### 抢占回收

当高优先级实例无法准入时，按以下顺序选择同一节点上的在途受害者：

1. 优先级升序；
2. 阶段靠前优先（读层先于写临时存储）；
3. 已消耗写入额度升序；
4. 实例 ID。

只允许抢占严格低优先级实例。回收链同时检查节点可用存储、全局临时存储上限和节点并发槽位。应用时先释放，再检查并扣减；任何时刻都不会持久化负余额。每个受害者产生 `UNSETTLED` 退款记录，包含已消耗读额度、写额度、释放字节、抢占者和逻辑时钟；可用 `settle_refund` 结算。

### 单轮调度

同一节点的空闲并发槽构成虚拟时隙，多个槽在逻辑偏移 0 同时启动。每个实例预计读层耗时按定点 ticks 计算；如果 `slot_start + read_ticks` 超过本轮预算，后续未启动实例记为 `NOT_STARTED_BUDGET`。节点容量不足记为 `NOT_STARTED_CAPACITY`。这两类都属于“预算内未启动”，与 `FAILED_AFTER_START` 和 `ABANDONED` 分开。预算结束不会结束已启动实例，它们仍保持 `READING_LAYERS`。

## 错误码

关键业务错误码：

| 错误码 | 含义 |
| --- | --- |
| `CS_INVALID_JSON` | JSON 缺失或无法解析 |
| `CS_SCHEMA_INVALID` | 结构/字段缺失 |
| `CS_DUPLICATE_INSTANCE` | 实例 ID 重复 |
| `CS_DUPLICATE_LAYER` | 层摘要重复 |
| `CS_LAYER_MISSING` | 所需层不存在或已提交集合缺层 |
| `CS_LAYER_DIGEST_MISMATCH` | 已提交层集合含额外/重复摘要 |
| `CS_INVALID_QUANTITY` | 正数/非负数约束失败 |
| `CS_INVALID_CONFIG` | 配置字段缺失或非法 |
| `CS_NO_CAPACITY` | 抢占后仍无法准入 |
| `CS_CLOCK_ROLLBACK` | 逻辑时钟回拨超过阈值 |
| `CS_IDEMPOTENCY_CONFLICT` | 同一幂等键请求内容不同 |
| `CS_CONSERVATION_VIOLATION` | 配额、层集合或余额守恒失败 |
| `CS_LEDGER_CORRUPTION` | 当前指针缺失或状态校验和不匹配 |

非法输入在打开数据库前完成校验；通过 CLI 提交非法样本时不会创建数据库文件。

## 验证

`tests/` 覆盖 34 项测试：

- 输入解析、重复 ID、缺失层、非法整数/正数、跨节点层集合；
- 状态迁移、截止先于预算、读层失败和撤回的不同结局码；
- 实例/节点独立余量携带、重复换算一致、层变更撤回；
- 原子准入、守恒审计、抢占链、退款结算、失败抢占回滚；
- 轮次预算、节点并发、多节点分组、轮次幂等；
- 定点/Decimal 双通路、精度敏感标记和误差来源链；
- 校验和损坏检测、强制终止回滚、逐位重放、多进程同键幂等与并发不超扣。

实际结果保存在 `artifacts/verification.json`，包括本轮样本摘要、守恒审计、抢占链、恢复/重放状态 SHA-256 和 pytest 结果。

