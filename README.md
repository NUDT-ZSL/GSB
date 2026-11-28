# 水产边缘网关离线调度与队首阻塞分析

本仓库是一个只使用 Python 3 标准库的离线工具，用于分析多个塘口上报流与增氧机控制指令复用同一条边缘上行连接时的逐帧发送时序。它不连接真实网络服务，工程、轨迹、策略对比和快照都落盘为仓库内 JSON 文件。

固定种子样本包含 3 个塘口、13 条流、98 帧。基线策略下 `P02/HISTORY` 的大块历史曲线会占用唯一的并发流槽位，并与其他流争用带宽；消费者降速和 `1000–1250 ms` 链路瞬断进一步放大等待。内置演示会给出逐帧证据、控制指令超时清单、策略调整前后分布和快照续算一致性结果。

## 快速开始

要求 Linux/WSL Ubuntu 上的 Python 3（建议 3.10+），不安装任何第三方包。

```bash
# 1. 生成固定种子工程、手工边界工程与三个非法输入样本
python3 -m aqua_edge generate --kind all --out-dir samples

# 2. 装载并检查流标识与网络声明
python3 -m aqua_edge load samples/pond_network.json

# 3. 完整调度，逐帧输出开始/完成/队首阻塞/阻塞源，并保存 JSON 轨迹
python3 -m aqua_edge schedule samples/pond_network.json \
  --json-out artifacts/full_trace.json

# 4. 与独立参考调度器逐帧核对
python3 -m aqua_edge verify samples/pond_network.json
python3 -m aqua_edge verify samples/boundary_network.json

# 5. 调整并发上限、优先级和窗口，比较前后分布
python3 -m aqua_edge compare samples/pond_network.json \
  --max-streams 2 \
  --priority-order "P01/AERATOR-01,P02/AERATOR-01,P03/AERATOR-01,P01/DO,P02/DO,P03/DO,P01/PH,P02/PH,P03/PH,P01/TEMP,P02/TEMP,P03/TEMP,P02/HISTORY" \
  --priority "P02/HISTORY=5" \
  --window "P01/DO=6400,P02/DO=6400,P03/DO=6400,P01/PH=4800,P02/PH=4800,P03/PH=4800,P01/TEMP=4000,P02/TEMP=4000,P03/TEMP=4000" \
  --json-out artifacts/policy_compare.json

# 6. 在第 24 个完成帧的事件边界保存快照，再从快照续算
python3 -m aqua_edge snapshot-save samples/pond_network.json \
  --after 24 --out artifacts/snapshot_after_24.json
python3 -m aqua_edge snapshot-resume artifacts/snapshot_after_24.json \
  --json-out artifacts/resumed_trace.json --verify-full
```

也可以直接运行完整演示：

```bash
./run_demo.sh
# 或
python3 -m aqua_edge demo --auto
# 交互终端
python3 -m aqua_edge demo
```

## 终端交互命令

交互入口支持逐条命令执行。`demo_commands.txt` 是可直接回放的固定命令序列。

```text
generate [--kind sample|boundary|all] [--seed N] [--out PATH] [--out-dir DIR]
load <工程.json>
show
schedule [--json-out PATH] [--after N]
adjust max_streams=N priority_order=ID1,ID2 priority=ID=VALUE window=ID=BYTES window_default=BYTES
compare [--json-out PATH]
snapshot save <快照.json> --after N
snapshot load <快照.json>
resume [--json-out PATH] [--verify-full]
help
quit
```

所有 `schedule`、`compare`、`snapshot`、`resume` 操作都会输出终端摘要和可逐帧核对的调度数据。

## 工程格式

规范格式如下，完整实例见 `samples/pond_network.json`。

```json
{
  "format": "aqua-edge-project",
  "project_version": 1,
  "name": "三塘口工程",
  "seed": 20261128,
  "network": {
    "uplink_bytes_per_s": 90000,
    "rtt_ms": 140,
    "max_concurrent_streams": 1,
    "frame_size_limit_bytes": 24000
  },
  "consumer": {
    "read_bytes_per_s": 25000,
    "events": [
      {"time_ms": 500, "read_bytes_per_s": 5000},
      {"time_ms": 1500, "read_bytes_per_s": 30000}
    ]
  },
  "outages": [{"start_ms": 1000, "end_ms": 1250}],
  "streams": [
    {
      "stream_id": "P01/AERATOR-01",
      "pond_id": "P01",
      "kind": "control",
      "target_device": "p01-aerator-01",
      "priority": 100,
      "window_bytes": 1600,
      "deadline_ms": 220,
      "frames": [
        {
          "frame_id": "P01-AERATOR-001",
          "logical_time_ms": 100,
          "length_bytes": 220,
          "ready": true
        }
      ]
    }
  ]
}
```

流类型：

- `sensor` / `telemetry`：必须声明 `sensor_type`。
- `control` / `command`：必须声明 `target_device` 和 `deadline_ms`。
- `history` / `bulk`：大块历史数据流，可声明 `sensor_type`。

帧也可以放在顶层扁平的 `frames` 数组中，每帧带 `stream_id`。此时顶层数组必须按逻辑时刻非降序排列，装载后会自动重组成逐流有序序列。同一工程不能同时使用顶层 `frames` 和流内 `frames`。

优先级数值越大越先获得发送槽位。同一逻辑时刻竞争时，裁决键依次为：

1. 流优先级降序；
2. `stream_id` 字典序升序；
3. 流内帧序号升序；
4. `frame_id` 字典序升序。

## 输入拒绝码

失败时进程返回非零状态，并输出 `code + message + details`，不会静默跳过：

| 业务码 | 含义 |
| --- | --- |
| `E_ORDER` | 同一流内逻辑时刻倒序、顶层扁平帧倒序、速率/瞬断事件顺序错误 |
| `E_DUPSTREAM` | 流标识重复定义 |
| `E_FRAMELEN` | 帧长度缺失、非整数、为零或负数 |
| `E_FRAMELIMIT` | 帧长度超过 `frame_size_limit_bytes` |
| `E_FRAMEDUP` | 帧标识重复 |
| `E_PROJECT` | 工程字段、流元数据或 JSON 结构不完整 |
| `E_SNAPVERSION` | 快照版本不符、期望版本外的必需字段缺失或状态损坏 |
| `E_STREAM` | 策略调整引用了不存在的流 |
| `E_ARGUMENT` | 命令行参数非法 |

`samples/invalid_*.json` 分别演示 `E_ORDER`、`E_DUPSTREAM`、`E_FRAMELEN`。

## 调度语义

调度器使用浮点毫秒事件时间，规则固定如下：

- 上行链路总带宽为 `network.uplink_bytes_per_s`，所有正在发送的流平均分享带宽。
- `max_concurrent_streams` 是同时处于发送状态的流数量上限。
- 每条流同一时刻最多一个在途帧，流内严格按装载顺序发送。
- 帧在 `logical_time_ms` 且 `ready=true` 后就绪。控制指令时限从该就绪时刻开始计算，即 `deadline_at = logical_time_ms + deadline_ms`。
- 发送耗时等于帧长除以当前分得的带宽；完成的帧释放带宽和并发槽位。
- 流窗口在帧开始发送时扣减，在“接收端读取完成 + RTT”后归还。消费者读取是全局队列，按帧完成顺序排队；读取速率事件会重算尚未读完信用的归还时刻。
- 链路瞬断期间不启动新帧，正在发送的帧暂停剩余字节；只能在 `end_ms` 或之后恢复。暂停时间和排队期间遇到的瞬断都会写入轨迹。
- `queue_delay_ms = start_ms - logical_time_ms`，即题目定义的队首阻塞时延。
- 统计分位值使用线性插值；每流、每塘口均输出均值、最大值、p50、p95、p99 和超时帧数。

阻塞归因类型：

- `BANDWIDTH`：同连接大块帧或并发槽位占满，轨迹给出具体源 `stream_id/frame_id`。
- `WINDOW_RTT`：流窗口被已发送但未归还的帧占满，且信用已读完、只等 RTT。
- `CONSUMER_SLOW`：消费者读取速率下降，信用读取队列排队累积。
- `OUTAGE`：链路瞬断。
- `WINDOW_CAPACITY`：单帧长度超过该流窗口，窗口永远无法容纳。
- `NOT_READY`：输入帧 `ready=false`，保持流内顺序并标记未发送。

阻塞判定按等待时间段累计，最终取累计时长最大的阻塞类型/源；JSON 中的 `wait_breakdown` 保留全部时间段聚合，可人工复核。

## 轨迹、统计与输出

`schedule --json-out` 输出的每一帧包含：

- `logical_time_ms`、`ready_time_ms`、`length_bytes`；
- `start_ms`、`end_ms`、`transfer_ms`、`queue_delay_ms`；
- `blocking_type`、`blocking_stream_id`、`blocking_frame_id`、`blocked_by`、`wait_breakdown`；
- `outage_wait_ms`、`outage_pause_ms`、`outage_total_ms`；
- 控制流截止时刻 `deadline_at_ms`、`on_time`；
- 流语义、塘口号、优先级、窗口和最终状态。

`summary` 包含逐流/逐塘口时延分布、控制指令按时判定、超时指令清单、阻塞源按时长汇总以及未发送帧清单。超时清单的每条记录都带 `blocking_type`、`blocking_stream_id` 和 `blocking_frame_id`。

`compare` 会从同一工程重新构建完整前后轨迹，不续接旧调度状态。输出逐塘口和逐流的均值、p95、最大值、超时数及差值，并给出引用具体流标识和调整项的建议，例如把 `P02/HISTORY` 降到控制/告警流之后、提高 `max_concurrent_streams`、增加 `P03/DO.window_bytes` 等。

## 快照与续算

快照版本为 `aqua-edge-snapshot/1`，包含：

- 当前有效工程（包括已应用的策略）；
- 当前逻辑时刻、已完成帧数、各流下一个待发帧和窗口；
- 在途帧的剩余字节、发送开始时刻和瞬断暂停量；
- 未归还的消费/RTT 信用、消费者速率事件游标；
- 已有轨迹、待发帧的历史等待片段。

`snapshot-resume --verify-full` 会从快照工程再跑一次不中断全量调度，并逐帧核对 `logical_time_ms`、`start_ms`、`end_ms` 和 `queue_delay_ms`。测试覆盖多个切点，包括链路瞬断附近。版本不符或必需字段缺失统一返回 `E_SNAPVERSION` 并打印期望版本。

## 代码结构与验收

```text
aqua_edge/
  model.py       工程解析、流语义和输入校验
  scheduler.py   事件调度、窗口/消费/RTT、瞬断、阻塞归因、快照状态
  reference.py   独立实现的全量参考调度器
  analysis.py    分布、超时、阻塞源、策略对比和建议
  policy.py      并发/优先级/窗口调整
  snapshot.py    版本化快照 I/O
  trace.py       轨迹文档与逐帧比较
  generator.py   固定种子工程和非法边界样本
  report.py      终端表格和摘要
  cli.py         一次性命令与交互式演示
tests/test_aqua_edge.py
samples/         确定性样本
artifacts/       演示产生的轨迹、对比和快照
```

运行验收：

```bash
python3 -m unittest discover -v
python3 -m aqua_edge verify samples/pond_network.json
python3 -m aqua_edge verify samples/boundary_network.json
./run_demo.sh
```

`verify` 使用独立参考调度器，核对固定种子工程和手工边界工程的每帧开始/完成时刻与队首阻塞时延。`run_demo.sh` 重新生成全部样本，并产出：

- `artifacts/full_trace.json`：基线完整逐帧轨迹；
- `artifacts/reference_check.json`：独立参考调度器逐帧核对结果；
- `artifacts/snapshot_after_24.json`：第 24 个完成帧后的快照；
- `artifacts/policy_compare.json`：调整前后完整轨迹与对比；
- `artifacts/optimized_trace.json`：优化策略逐帧轨迹；
- `artifacts/resumed_trace.json`：快照续算轨迹及一致性结论。
