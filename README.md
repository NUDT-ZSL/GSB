# 本地中断合并核算与偏差账本引擎

本项目从零实现一个可离线运行的网络接口中断合并（interrupt coalescing）参数核算引擎。它把微秒窗口和帧数阈值换算到统一固定点时间尺度，逐条流量记录推演中断，使用 SQLite 事务账本保存每个换算、每条记录的双通路结果、预算归因和恢复状态。

实现特点：

- 定点路径只使用整数、`Fraction` 与十进制字符串，重复输入逐位确定性。
- float64 路径独立使用 `numpy.float64` 推进和计算。
- 每个队列/方向分别保留帧阈值换算余量，余量在后续换算中继续参与。
- 每条记录、每次换算、每个参数批次和每次诊断均可在 SQLite 中追溯。
- 记录提交、参数替换与诊断快照均可从强制终止点恢复。
- `run`、`load` 和记录推进均支持重复执行而不重复累计。

## 环境

要求 Python 3.11 或更高版本、`numpy`、`pytest`。本工作区实际验证使用：

```text
Python 3.12.13
numpy 2.5.5
pytest 9.2.0
SQLite 3.53.1
```

Linux/macOS：

```bash
./scripts/bootstrap.sh
.venv/bin/python -m pytest
.venv/bin/python scripts/run_demo.py
```

离线 wheel 目录：

```bash
WHEEL_DIR=/path/to/wheels ./scripts/bootstrap.sh
```

Windows PowerShell：

```powershell
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
.\scripts\bootstrap.ps1
.\.venv\Scripts\python.exe -m pytest
```

运行前可检查数据库旁路文件：

```powershell
Get-ChildItem .\artifacts\*.sqlite* -Force -ErrorAction SilentlyContinue
```

`*.sqlite.lock` 是跨进程 advisory lock 的稳定旁路文件；命令退出后不持有操作系统锁，但文件本身按设计保留，避免删除-重建 inode 产生锁竞争。SQLite WAL 在最后一个连接关闭后由 SQLite 回收。

## 快速运行

生成确定性样本：

```bash
python scripts/generate_samples.py
```

完整命令行：

```bash
python -m ic_ledger load samples/sample.json --db artifacts/demo.sqlite
python -m ic_ledger run --db artifacts/demo.sqlite
python -m ic_ledger diagnose --db artifacts/demo.sqlite
python -m ic_ledger status --db artifacts/demo.sqlite
python -m ic_ledger verify --db artifacts/demo.sqlite
python -m ic_ledger resume --db artifacts/demo.sqlite
```

所有结果均输出 UTF-8、`sort_keys=True` 的 JSON。`--compact` 在子命令前后均可使用。

## 输入格式

样本顶层字段：

```json
{
  "schema_version": 1,
  "interfaces": [
    {
      "name": "eth0",
      "queues": [
        {
          "queue": 0,
          "weight": 4,
          "max_irq_rate": 100000,
          "coalesce": {
            "rx_usecs": 30,
            "rx_frames": 7,
            "tx_usecs": 20,
            "tx_frames": 3
          }
        }
      ]
    }
  ],
  "traffic": [
    {
      "record_seq": 1000,
      "iface": "eth0",
      "queue": 0,
      "direction": "rx",
      "logical_clock_us": 10,
      "arrival_frames": 2,
      "frame_bytes": 1000
    }
  ],
  "config": {
    "fixed_scale": 1000,
    "avg_frame_interval_us": "1.3333",
    "float_tolerance": "1e-9",
    "error_bound_budget_us": "0.01",
    "clock_step_us": 10,
    "irq_budget_per_second": {
      "eth0:0": 25000
    }
  }
}
```

约定：

- `fixed_scale` 表示每微秒的固定点 tick 数，必须是正整数。
- `avg_frame_interval_us` 使用十进制字符串或整数解析为精确有理数。
- `frame_bytes` 是一条记录中到达流量的总字节数。
- `direction` 可省略，默认 `rx`；`tx` 记录使用该队列的 tx 合并参数。
- `traffic` 中 `record_seq` 必须严格递增；同一队列的逻辑时钟不得倒退。
- 外部 `coalescing`/`params` 参数集也受支持；若与队列内嵌参数同时存在，二者必须完全一致。
- `irq_budget_per_second` 按 `iface:queue` 指定；缺省时使用该队列的 `max_irq_rate`。

## 推演语义

对每个队列的 rx/tx 方向分别维护固定点与 float64 状态：

1. 微秒阈值固定点值为 `rx_usecs * fixed_scale` 或 `tx_usecs * fixed_scale`。
2. 帧阈值固定点值按 `frames * avg_frame_interval_us * fixed_scale` 计算。
3. 帧阈值使用向下取整：

   ```text
   total = exact + residue_before
   ticks = floor(total)
   residue_after = total - ticks
   ```

   `residue_after` 逐队列/方向保留，下次换算继续参与。每次换算登记 scale、精确有理值、取整方向、舍弃余量、换算前后余量和 float64 对应值；微秒窗口换算也登记为 `rounding=exact` 的精确换算。
4. 时间窗口到期条件为 `elapsed_us >= usecs`。
5. 帧阈值达成条件为 `pending_frames >= frames`。
6. 同一记录同时命中时只计一次中断，`fixed_trigger` 记为 `both`。
7. 参数为 0 表示该条件禁用；两个参数均为 0 时每条记录立即产生一次中断，来源为 `immediate`。
8. 中断后窗口起点重置为当前逻辑时钟，帧计数清零，并立即为下一窗口进行帧阈值换算。

`frame_bytes` 为记录级总字节。吞吐固定点结果使用精确有理数：

```text
duration_us = max(last_clock_us - first_clock_us, clock_step_us)
throughput_bps = total_bytes * 8 * 1_000_000 / duration_us
irq_rate_per_second = interrupt_count * 1_000_000 / duration_us
```

float64 路径使用相同输入，但以 `numpy.float64` 进行时间比较和乘除。每条记录保存两个路径的值、三类指标（中断速率、吞吐、窗口占用）各自的绝对/相对偏差、最大偏差和首个分歧信息。换算误差与乘加误差也分别保存为 `conversion_error_us`、`arithmetic_error_us`，总上界为 `error_bound_us`。

## 偏差与误差上界

固定点换算余量累计上界：

```text
conversion_error_us = cumulative_discarded_ticks / fixed_scale
arithmetic_error_us = duration_us / 2^52
error_bound_us = conversion_error_us + arithmetic_error_us
```

固定点有效中断速率比 float64 速率高出的保守上界为：

```text
rate_error_bound =
  interrupt_count * 1_000_000 * error_bound_us
  / (duration_us * (duration_us - error_bound_us))
```

预算超限时：

```text
total_excess = max(0, fixed_irq_rate - budget)
residue_excess = min(total_excess, rate_error_bound)
residue_ratio = residue_excess / total_excess
```

- `residue_excess == total_excess`：归因 `conversion_residue`。
- `0 < residue_excess < total_excess`：归因 `mixed_traffic_and_residue`。
- `residue_excess == 0`：归因 `real_traffic`。

诊断读取当时已提交的 runtime 与 ledger，不使用重放结果倒推；每个 `(queue, param_version, ledger_watermark, batch_version)` 只保存一份快照。

## 失效闭包

参数批次整体提交，但 coalescing 版本按队列指纹复用：

- coalescing 参数或影响推演的配置变化：该队列新建 `param_version`，旧 ledger/conversion 标记无效，runtime 删除，所有中断计数、吞吐、余量、归因需重算。
- 仅 weight 变化：中断计数、原始吞吐、换算余量 ledger 可安全复用；加权吞吐结论与归因重算。
- 仅预算或最大中断速率变化：原始 ledger 复用；归因重算。
- 未变化队列：不触碰 active 参数指针、runtime 或 ledger 行。

`load` 输出 `invalidation.changed` 和 `invalidation.reused`，包括触发来源、记录区间、ledger 行数、ledger 摘要和每个组件的 `reuse`/`recompute` 决定。

显式请求低于当前活动批次号时返回 `E_PARAM_VERSION_ROLLBACK`；同一版本绑定不同 payload 时返回 `E_PARAM_VERSION_CONFLICT`。

## 事务、崩溃与并发

SQLite 使用 WAL、`synchronous=FULL`、`BEGIN IMMEDIATE` 和 `busy_timeout=30s`。每次记录推进的以下内容在单个事务中提交：

- `record_ledger`
- `conversions`
- `committed_records`
- `queue_runtime`（窗口、帧计数、中断数、余量、误差上界、水位）

`committed_records` 与 ledger 均有 `(iface, queue, direction, record_seq, param_version)` 唯一约束。多个进程并发推进时，旁路文件锁串行化临界区，SQLite 事务提供第二层一致性；同一记录重复提交只返回 `idempotent=true`。

参数导入使用两阶段：

1. 事务提交 operation journal、完整 payload 和 staging 行。
2. 最终事务创建 `TEMP TABLE tmp_param_import`，插入全部目标参数，整体创建批次、替换 active 指针、写失效闭包、合并流量并提交。

`resume` 会识别未完成 journal：未提交最终事务则重新完成 staged import；已有活动批次且版本被后续批次取代则标记 `aborted_superseded`；已提交则只做状态修复。

可注入的强制终止点：

```bash
IC_CRASH_POINT=param_after_stage
IC_CRASH_POINT=param_before_commit
IC_CRASH_POINT=param_after_commit
IC_CRASH_POINT=record_before_commit  IC_CRASH_RECORD=1000
IC_CRASH_POINT=record_after_commit   IC_CRASH_RECORD=1000
```

命中时进程以退出码 `99` 强制执行 `os._exit`。记录事务回滚后，`resume` 只提交未完成记录，不重复累计中断或余量。

## 命令

| 命令 | 作用 |
|---|---|
| `load SAMPLE --db DB [--version N]` | 完整校验后导入；非法输入不创建数据库、不修改现有状态 |
| `run --db DB [--queue IFACE:QUEUE] [--max-records N]` | 推进未提交记录；`N` 限制本次新增提交数 |
| `diagnose --db DB [--queue ...]` | 从已提交账本计算预算与余量归因，并持久化快照 |
| `resume --db DB [--max-records N]` | 恢复半提交参数导入并继续记录推进 |
| `status --db DB` | 稳定排序输出水位、中断数、余量、换算摘要、分歧标记 |
| `verify --db DB [--queue ...]` | 从样本重放并逐字段对比 runtime、ledger、float64 值和余量 |

## 业务错误码

主要错误码：

```text
E_INPUT_NOT_FOUND       E_INPUT_JSON           E_SCHEMA
E_IFACE_NAME            E_IFACE_DUPLICATE      E_QUEUE_INDEX
E_QUEUE_DUPLICATE       E_QUEUE_WEIGHT         E_MAX_IRQ_RATE
E_RX_USECS              E_TX_USECS             E_RX_FRAMES
E_TX_FRAMES             E_PARAM_DUPLICATE_CONFLICT
E_RECORD_SEQ            E_RECORD_QUEUE         E_ARRIVAL_FRAMES
E_FRAME_BYTES           E_RECORD_CLOCK         E_RECORD_DIRECTION
E_CONFIG_FIXED_SCALE    E_CONFIG_FRAME_INTERVAL
E_CONFIG_TOLERANCE      E_CONFIG_IRQ_BUDGET
E_CONFIG_ERROR_BOUND    E_CONFIG_CLOCK_STEP
E_PARAM_VERSION_ROLLBACK                       E_PARAM_VERSION_CONFLICT
E_FLOW_CONFLICT         E_QUEUE_SELECTION      E_QUEUE_NOT_FOUND
E_RECORD_OUT_OF_ORDER   E_RECORD_NOT_FOUND     E_VERIFY_FAILED
```

CLI 业务错误退出码为 `2`；`verify` 发现不一致或未完成时为 `3`；成功为 `0`。

## 实际验证

本工作区执行结果：

```text
python -m pytest -q
45 passed
```

确定性样本 `samples/sample.json` 包含 3 个队列、75 条记录：

```text
eth0:0  30 条  9 次固定点中断
eth0:1  20 条  6 次固定点中断，首个分歧 record_seq=1060，metric=window_occupancy
eth1:0  25 条  8 次固定点中断
全局记录水位 = 1740
```

已验证：

- 两种触发同时命中只计一次，触发来源为 `both`。
- 帧阈值余量在后续换算中继续参与；相同输入重复换算逐位一致。
- 第二个完整 `run` 的 75 条记录全部为幂等提交。
- `verify` 重放后 runtime、ledger、float64 中间值、绝对/相对偏差和余量逐字段一致。
- `record_before_commit` 崩溃后无记录水位，`resume` 完成后与无崩溃数据库状态一致。
- `record_after_commit` 崩溃后恢复不会重复计入。
- `param_after_stage`、`param_before_commit` 崩溃后 `resume` 完成参数版本整体替换。
- 三个进程并发（其中两个同时推进同一队列）后记录数与中断数正确，重复提交均幂等。
- weight 变化只重算加权吞吐与归因，三个队列的原始 ledger 摘要均保持不变。
- coalescing 变化只重置目标队列；其他队列的 param_version、水位、中断数和 ledger 摘要不变。
- 非法样本返回对应业务错误码，且数据库文件不创建或现有状态不变。

完整报告由以下命令重新生成：

```bash
python scripts/run_demo.py
```

报告写入 `artifacts/verification-report.json`。

