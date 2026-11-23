# 本地时序指标采集引擎：分区落盘、配额与令牌账本

这是一个只依赖 Python 标准库运行时的本地确定性引擎。它把多条指标流按固定哈希路由到存储分区，在租户字节配额与全局落盘令牌的双重约束下裁决缓冲或落盘，并通过 SQLite 账本、分区 JSONL 数据文件和文件锁保证幂等、并发一致性与强制终止后的可恢复性。

所有业务时间判断只使用命令行或事件中注入的逻辑时钟毫秒值，不读取系统墙钟。`time.monotonic()` 仅用于文件锁等待超时，不参与业务裁决。

## 交付内容

| 路径 | 内容 |
| --- | --- |
| `metric_engine/` | 配置校验、定点账本、哈希路由、SQLite 事务、调度、恢复与 CLI |
| `scripts/generate_samples.py` | 生成全部本地确定性样本 |
| `scripts/run_acceptance.py` | 自动跑主场景、强杀恢复场景与测试，并生成验证报告 |
| `samples/` | 指标流、分区、配额、令牌、事件、路由清单与恢复计划 |
| `tests/` | 28 个 pytest 测试，覆盖校验、幂等、借调、预算、精度、并发和崩溃恢复 |
| `artifacts/validation_report.json` | 最近一次完整验收的环境、命令、计数和摘要 |

## 环境准备

要求 Python 3.11+、`numpy==1.26.4`、`pytest==8.2.2`。运行时不需要 NumPy，安装它是为固定实验环境。

Linux / WSL:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install numpy==1.26.4 pytest==8.2.2
python --version
python -c "import sqlite3; print(sqlite3.sqlite_version)"
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install numpy==1.26.4 pytest==8.2.2
python --version
```

当前实际验证环境为 Python 3.11.18、SQLite 3.53.1、numpy 1.26.4、pytest 8.2.2。文件锁同时实现了 `fcntl` 与 Windows `msvcrt` 分支。

## 快速开始

生成样本并初始化工作区：

```bash
python scripts/generate_samples.py
python -m metric_engine --workspace runtime load
```

跑两阶段主场景：

```bash
python -m metric_engine --workspace runtime ingest --events samples/events.json
python -m metric_engine --workspace runtime flush --clock-ms 2500
python -m metric_engine --workspace runtime ingest --events samples/events_phase2.json
python -m metric_engine --workspace runtime flush --clock-ms 6500
python -m metric_engine --workspace runtime status
python -m metric_engine --workspace runtime verify
```

`--workspace` 与 `--compact` 可放在子命令前后。重复 `load` 相同配置为幂等返回 `unchanged`；配置哈希不同则拒绝并返回 `E_CONFIG_CONFLICT`。

工作区固定目录：

```text
runtime/
  state/engine.sqlite3       # 账本、版本快照、缓冲、配额、令牌、审计
  data/partition-XXXX.jsonl  # 每分区 append-only 落盘记录
  data/partition-XXXX.lock   # 分区文件锁
  buffer/partition-XXXX.json # 由 SQLite 权威状态生成的缓冲快照
  logs/engine.jsonl          # 无墙钟的业务操作日志
```

## 配置格式

完整示例见 `samples/`。最小字段如下：

```json
{
  "streams": [{
    "tenant": "tenant-a",
    "metric": "api.latency",
    "tags": {"host": "a1"},
    "sample_period_ms": 500,
    "sample_bytes": 6
  }]
}
```

分区配置：

```json
{
  "partition_count": 4,
  "buffer_per_partition": 3,
  "flush_round_limit": 4,
  "round_time_budget_ms": 4000,
  "hash_seed": "metric-engine-demo-v1",
  "token_cost_per_sample": 1
}
```

配额配置：

```json
{
  "window_ms": 5000,
  "fixed_scale": 1000,
  "comparison_tolerance_bytes": "0.2",
  "tenants": {
    "tenant-a": {
      "window_limit_bytes": "20.0",
      "borrow_limit_bytes": "4.0",
      "allow_borrow": true
    }
  }
}
```

令牌配置：

```json
{
  "capacity_tokens": 3,
  "refill_rate_per_second": 1,
  "clock_step_ms": 250,
  "fixed_scale": 1000
}
```

事件示例：

```json
{
  "events": [{
    "event_id": "evt-1",
    "tenant": "tenant-a",
    "metric": "api.latency",
    "tags": {"host": "a1"},
    "logical_time_ms": 1000,
    "sample_seq": 1
  }]
}
```

`event_id` 省略时由流身份、逻辑时间、样本序号和样本字节数生成稳定 SHA-256；同一语义样本无论重复提交多少次都只保留一条账本记录。

## 算法与事务语义

**确定性分区路由**

路由材料为 `{seed, tenant, metric, sorted(tags)}`，经规范 JSON 和 SHA-256 后对分区数取模。相同配置和流在任何进程中映射到相同分区；`samples.partition_arrival_seq` 每分区从 1 单调递增且不跳号，丢弃样本也占用到达序号。全局核对键为 `(logical_time_ms, sample_seq, partition, partition_arrival_seq, sample_id)`。

**配额窗口与借调**

落盘按注入的落盘逻辑时钟归属窗口 `floor(clock_ms / window_ms)`。窗口内 `used_units`、`borrowed_units`、`return_obligation_units` 与 `quota_charges` 在同一 SQLite 事务中更新。基础余量不足时，仅允许借调的租户可在 `borrow_limit_bytes` 内借用，借用逐样本写入 `borrow_ledger` 并标记 `OUTSTANDING`。窗口推进时旧窗口冻结，记录 `frozen_remaining_units = max(0, limit + borrow_limit - used)`。

**令牌桶**

令牌余额按 `elapsed_ms * refill_rate_units_per_second / 1000` 向下取整补充，并按 `clock_step_ms` 量化逻辑时间；逻辑时间、余额和补充余量均记录到精度审计表。默认每个样本消耗 `token_cost_per_sample` 个令牌。

**联合裁决与调度**

每轮按 `tenant, logical_time, sample_seq, partition, partition_arrival_seq` 排序。先检查租户配额，再检查全局令牌，任一不足就保留在缓冲区并记录首个约束：`tenant_quota` 或 `global_token`。通过后检查逻辑时间预算，预算单位为样本的 `sample_period_ms`。

每轮受 `flush_round_limit` 限制的样本真正落盘，其他候选记录 `DEFERRED_ROUND_LIMIT` 并保留。若下一候选所需逻辑时间超过本轮剩余预算，则本轮剩余候选全部放弃，记录：

- `start_state=NOT_STARTED`、`outcome=NOT_STARTED_BUDGET`：预算内未启动；
- `terminal_state=ABANDONED`、样本 `decision=ABANDONED_BUDGET`：主动放弃；
- 崩溃恢复时 `outcome=PERSISTED_THEN_FAILED`、`terminal_state=PERSISTED_AFTER_RECOVERY`：数据已写但账本未提交，随后只重做缺失账本。

`start_state=STARTED` 只在追加文件前设置，因此已启动落盘不会被误记为未启动。

**定点与精度敏感**

配额和令牌分别使用配置声明的 `fixed_scale` 换算为整数账本单位。每次换算登记标度、舍入方向、被舍弃余量和累计误差上界；加减乘除步骤记录来源 `scope/step/tenant/window/partition/register_id`。当不同租户或分区的可用额度差小于 `comparison_tolerance_bytes`，且一个通过、另一个失败时，写入 `precision_sensitive` 标记及双方误差来源链。

**提交、版本与恢复**

所有状态变更使用 SQLite WAL、`PRAGMA synchronous=FULL` 与 `BEGIN IMMEDIATE`。每次事务生成严格递增版本号和完整状态 JSON 快照，再原子替换单行 `ledger_pointer`；低版本或过期期望版本返回 `E_STALE_VERSION`。

落盘协议为：在数据库事务内完成预检并启动样本，持有分区文件锁追加带 `record_hash` 的 JSONL，`fsync` 后更新配额、令牌和已落盘表，最后提交 SQLite。若进程在文件追加后、提交前被强制终止，SQLite 回滚，下次 `resume` 扫描完整数据行：

1. 截断无换行的半写尾行；
2. 对数据文件存在但 `persisted_samples` 缺失的样本补做配额和令牌扣减；
3. 依靠 `quota_charges/token_charges` 主键与事务保证不重复计费；
4. 检测并重建陈旧或半写的缓冲快照。

恢复应在崩溃后、继续写入前执行。分区文件追加受跨平台文件锁保护，配额和令牌受 SQLite 写事务保护。

## 命令行

| 子命令 | 作用 |
| --- | --- |
| `load` | 解析全部配置；仅在全部合法后初始化 SQLite |
| `ingest` | 批量校验并接入本地事件 JSON；缓冲满时自动触发一轮 flush |
| `flush --clock-ms N` | 按注入逻辑时钟执行确定性落盘轮次 |
| `resume [--clock-ms N]` | 修复半写数据尾、重做缺失账本、重建缓冲快照 |
| `status` | 输出稳定排序 JSON：租户字节、令牌、缓冲、精度敏感链 |
| `verify` | 核对版本链、数据文件、配额扣减、令牌扣减、到达序号和缓冲摘要 |

稳定业务错误码包括 `E_BAD_STREAM`、`E_BAD_TAG`、`E_BAD_PARTITION_CONFIG`、`E_BAD_QUOTA`、`E_BAD_TOKEN`、`E_BAD_EVENT`、`E_CLOCK_REGRESSION`、`E_STALE_VERSION`、`E_CONFIG_CONFLICT`、`E_RECOVERY_CONFLICT` 与 `E_VERIFY_FAILED`。非法配置在创建任何持久目录之前失败。

## 实际验收结果

最近一次 `python scripts/run_acceptance.py` 的结果写入 `artifacts/validation_report.json`：

- pytest：`28 passed`。
- 主场景：两阶段共接入 9 条，落盘 6 条，保留缓冲 3 条，无丢弃。
- 租户 A 在窗口 `1` 使用 21 字节，其中借调 1 字节，归还义务 1 字节。
- 全局令牌容量 3，最终余额 0；两轮 flush 后 `verify.ok=true`，清单摘要为 `e5c0e93b68b50302b83735192fe0cb4e640ff9e17ff5a91455ed79b2a0fa4155`。
- 精度敏感标记 2 条，均为租户 B/C 的 0.1 字节可用额度差，容差 0.2 字节；来源链包含登记序号与换算步骤。
- 强杀场景在数据追加后以退出码 86 终止；恢复重做 1 条缺失样本，数据文件已存在 1 行，最终落盘 5 条。
- 恢复后配额扣减 5 条、令牌扣减 5 条、`REDO_MISSING_SAMPLE=1`，无重复扣减；`verify.ok=true`。

恢复实验可单独重跑：

```bash
python scripts/generate_samples.py
python -m metric_engine --workspace runtime-recovery load
python -m metric_engine --workspace runtime-recovery ingest --events samples/events_recovery.json
METRIC_ENGINE_FAILPOINT=after_data_append_before_commit \
  python -m metric_engine --workspace runtime-recovery flush --clock-ms 2500
python -m metric_engine --workspace runtime-recovery resume
python -m metric_engine --workspace runtime-recovery flush --clock-ms 2500
python -m metric_engine --workspace runtime-recovery flush --clock-ms 6500
python -m metric_engine --workspace runtime-recovery verify
```

Windows PowerShell 设置故障注入：

```powershell
$env:METRIC_ENGINE_FAILPOINT = "after_data_append_before_commit"
python -m metric_engine --workspace runtime-recovery flush --clock-ms 2500
Remove-Item Env:METRIC_ENGINE_FAILPOINT
python -m metric_engine --workspace runtime-recovery resume
```

## 测试

```bash
python -m pytest
python scripts/run_acceptance.py
```

测试使用临时目录和 SQLite，不访问网络、容器、真实监控系统或 GPU。并发测试会启动多个独立 Python 进程，同时重复接入和并行执行不同分区落盘。

## 明确假设

- `sample_period_ms` 是单个样本在一轮逻辑时间预算中的结算成本。
- 每样本令牌成本默认 1，可由 `token_cost_per_sample` 调整。
- 借调只记归还义务，不自动从后续窗口余额扣回。
- 预算耗尽会主动放弃该轮剩余候选，以满足“预算耗尽须放弃”的业务约束；轮条数上限只延后，不放弃。
- 缓冲为 SQLite 权威状态；`buffer/*.json` 是可由 `resume` 检测并重建的审计快照。
- 分区文件是落盘事实载体，SQLite 是账本与幂等事实来源；两者通过 `resume` 收敛。

