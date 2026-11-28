# 酒店边缘推理 KV 缓存调度与撤销核对

本项目在本地标准库环境中运行一个完全确定性的客房服务边缘推理模拟器：读入固定种子生成的请求流、取消事件和显存配置，执行批内预填充、逐 token 解码、取消回收、同刻竞争裁决、排队复用、显存核对、持久化续算和统计摘要。不会访问网络、账号或外部推理服务。

## 快速运行

```bash
python -m hotel_edge_kv
```

默认生成 `mixed` 场景并写入 `results/`。也可指定种子、边界场景和续算点：

```bash
python -m hotel_edge_kv --seed 20261128 --scenario boundary --resume-at 20
python -m hotel_edge_kv generate --output-dir results/inputs_only
```

安装后也可使用控制台入口 `hotel-edge-kv`。Python 版本要求 3.10 及以上，运行仅使用标准库。

## 调度语义

- 每个请求包含共享酒店/楼层/房型提示词前缀、住客诉求后缀、生成上限、到达时刻、客房号和工单号。
- 请求按 `(arrival_time, arrival_seq)` 到达并按 FIFO 尝试占用批槽与预填充块。块数按 `ceil((prefix_tokens + suffix_tokens) / block_size_tokens)` 计算。
- 每个全局解码边界，每个活动请求最多生成一个 token；跨越块边界时追加申请缓存块。若暂时无可用块则记录 `decode_stalled`，到下一个解码边界重试。
- 一个逻辑时刻内先处理到达，再处理取消，再处理预填充完成和解码。因此取消与完成同刻时取消优先，被取消请求不会再生成该时刻的 token。
- 预填充取消立即回收；解码取消在下一个全局解码边界回收，生效时刻仍记取消事件时刻。回收时延为 `reclaimed_at - effective_at`。
- 同一请求同一时刻有多个取消事件时，按 `(sequence, event_id)` 最小者裁决，其余事件进入竞争裁决记录。
- 取消释放的块进入空闲堆，批槽与块按排队顺序重新接纳，逐块记录来源请求、来源工单和释放原因。
- 每个事件时刻核对 `used_blocks + free_blocks == total_blocks`，并检查 `used_bytes <= vram_budget_bytes`。

## 输入模型

主要配置字段：

| 字段 | 含义 |
| --- | --- |
| `block_size_tokens` | 每个 KV 缓存块覆盖的 token 数 |
| `total_blocks` | 缓存块总数 |
| `block_bytes` | 每块显存字节数 |
| `vram_budget_bytes` | 显存字节预算 |
| `max_batch_requests` | 批内最大活动请求数 |
| `decode_step_ticks` | 逐 token 解码步长（逻辑 tick） |
| `prefill_duration_ticks` | 预填充占用的逻辑 tick 数 |
| `seed` | 确定性随机种子 |

生成器输出 `Scenario`，也可直接构造 `ServiceRequest` 和 `CancellationEvent` 做边界测试：

```python
from hotel_edge_kv import (
    CancellationEvent,
    EdgeConfig,
    Scheduler,
    ServiceRequest,
)

config = EdgeConfig(
    block_size_tokens=4,
    total_blocks=8,
    block_bytes=128,
    vram_budget_bytes=1024,
    max_batch_requests=2,
    decode_step_ticks=2,
    prefill_duration_ticks=1,
)
requests = [...]
cancellations = [...]
scheduler = Scheduler(config, requests, cancellations)
scheduler.run()
print(scheduler.result_summary())
```

## 结果目录

`python -m hotel_edge_kv` 会在结果目录下生成：

- `inputs/scenario.json`、`inputs/config.json`、`inputs/requests.json`、`inputs/cancellations.json`：确定性输入。
- `ledger/lifecycle.json` / `lifecycle.csv`：逐请求到达、获取块、预填充、每 token、终态轨迹。
- `ledger/block_ledger.json` / `block_ledger.csv`：逐块分配、释放、复用来源和工单对应关系。
- `ledger/lifecycle_by_request.json`：按请求聚合的完整轨迹。
- `checkpoint/resume_checkpoint.json`：生命周期与块账本持久化续算点。
- `reports/cancellation_report.json`：按原因、阶段、原因×阶段的回收统计与逐取消时延。
- `reports/competition_report.json`：取消/完成同刻竞争及同刻多次取消裁决。
- `reports/memory_timeline.json` / `memory_timeline.csv`：逐逻辑时刻显存占用曲线。
- `reports/audit_report.json`：块守恒、预算、重复占用、重复释放、残留块、取消后 token、重复接纳核对；异常均带块标识、请求号、工单号和时间。
- `reports/resume_report.json`：恢复前后快照、已取消请求是否重新接纳、续算终态与不间断基准的逐账本一致性。
- `summary.json` / `summary.md`：机器可读和便于前台复查的摘要。

默认场景特意覆盖：预填充取消、非边界解码取消、边界解码取消、取消与完成同刻竞争、同刻两次取消，以及取消块被后续排队请求复用。

## 测试

```bash
python -m unittest discover -s tests -v
```

边界测试使用构造请求验证块守恒、显存预算、同刻竞争、取消后零 token、双请求不重复占块和 checkpoint 续算一致性。
