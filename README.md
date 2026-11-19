# 本地 TCP 状态与拥塞控制推演引擎

这是一个完全本地、确定性的 TCP 连接状态与拥塞控制推演引擎。所有连接事件由
JSON 脚本和注入的逻辑时钟驱动，不创建真实网络连接。核心状态机不读取系统墙钟，
窗口计算使用整数定点表示；同一场景重复运行会得到逐字节一致的状态、cwnd 与阻塞
轨迹。

## 快速运行

环境已验证为 Python 3.10.12、NumPy 2.2.6。若没有系统 `pip`，可先创建带种子包的
虚拟环境：

```bash
uv venv --seed --python 3.10 .venv
.venv/bin/python -m pip install -r requirements.txt
```

也可使用常规方式：

```bash
python3 -m pip install numpy
```

运行固定样本：

```bash
.venv/bin/python -m tcp_engine run \
  --link samples/link.json \
  --script samples/script.json \
  --congestion samples/congestion.json \
  --observations samples/observations.json \
  --db artifacts/state.sqlite \
  --run-id demo

.venv/bin/python -m tcp_engine verify \
  --link samples/link.json \
  --script samples/script.json \
  --congestion samples/congestion.json \
  --observations samples/observations.json \
  --db artifacts/state.sqlite \
  --run-id demo

.venv/bin/python -m tcp_engine report \
  --db artifacts/state.sqlite \
  --run-id demo
```

也可使用单文件场景 `--scene samples/scene.json`。四文件与单文件场景使用相同的
规范化源摘要。

## 命令行

| 子命令 | 用途 |
| --- | --- |
| `run` | 分配新轨迹版本并从头/指定事件段推演 |
| `resume` | 读取 SQLite 中最后一个未完成版本，从连续已提交事件之后继续 |
| `status` | 输出当前完整版本、最新版本、提交水位与持久化状态 |
| `verify` | 从头重放活动轨迹，逐事件对照持久化结果并重新执行双路径一致性检查 |
| `report` | 以稳定排序 JSON 输出状态迁移链、cwnd 轨迹、阻塞统计、重传与观测样本 |

公共选项：

- `--db PATH`：本地 SQLite 文件，父目录自动创建。
- `--run-id ID`：同一场景的逻辑运行标识。
- `--scene PATH` 或四个 `--link/--script/--congestion/--observations` 文件。
- `--pretty`：缩进 JSON；默认输出紧凑且键排序的 JSON。
- `run --segment-start N --segment-end M --version-tag TAG`：多进程分段推演。

成功时输出稳定 JSON 到 stdout，退出码为 0。业务错误以 JSON 写入 stderr，退出码
为 2。强制终止测试钩子 `TCP_ENGINE_CRASH_AFTER=N` 会在第 N 个事件事务提交后
调用 `os._exit(70)`，用于验证恢复路径。

## 固定样本

`samples/` 包含链路、脚本、拥塞配置、观测配置四份文件，以及等价单文件场景。
固定样本共 24 个事件，覆盖：

- `LISTEN -> SYN-SENT -> ESTABLISHED`
- 慢启动、拥塞避免、固定点 cwnd 小数舍入
- 三次重复 ACK 触发快速重传、快速恢复部分确认与恢复退出
- 对端窗口缩放、窗口收缩、拥塞阻塞与接收窗口阻塞
- 三次 RTO 超时重传与指数退避
- `FIN-WAIT-1 -> FIN-WAIT-2 -> TIME-WAIT -> CLOSED`

运行演示：

```bash
./scripts/demo.sh
```

该脚本在独立数据库上运行两次、比较 `run`/`report` 输出字节、执行 `verify`，
并通过强制终止与 `resume` 验证只重放未提交事件。

并发分段演示：

```bash
./scripts/concurrent.sh
```

该脚本启动四个进程处理 `0..6/6..12/12..18/18..24` 事件段，随后重复提交第一段，
验证幂等和最终完整轨迹。

## 场景格式

### 链路

```json
{
  "rtt_ms": 40,
  "bandwidth_bps": 1000000,
  "receiver_window": 4096,
  "window_scaling": true,
  "window_scale": 2
}
```

`rtt_ms` 是正整数毫秒；`receiver_window` 是有效接收窗口字节数。启用窗口缩放时
`window_scale` 必须在 0..14；关闭时必须为 0。脚本中的
`advertised_window.window` 是原始通告窗口，有效值按 `window << window_scale`
计算。带宽被严格解析并作为场景描述记录；本样本模型不额外做发送速率整形。

### 脚本事件

事件按 `ts_ms` 非降序排列，`event_id` 必须唯一，缺省为 `evt-0000` 形式。
序列号是有效载荷字节偏移，从 0 开始；SYN/FIN 不额外占用本模型的字节序号。

| `type` | 必要字段 | 语义 |
| --- | --- | --- |
| `listen` | - | 进入 `LISTEN` |
| `connect` | - | `LISTEN/CLOSED -> SYN-SENT` |
| `syn_received` | - | `LISTEN -> SYN-RECEIVED` |
| `establish` | - | `SYN-SENT/SYN-RECEIVED -> ESTABLISHED` |
| `app_write` | `bytes` | 应用追加待发送字节，随后按窗口自动发送 |
| `app_read` | `bytes` | 应用读取已到达的对端字节 |
| `ack` | `ack_no`, 可选 `window` | 累计确认，可同时更新通告窗口 |
| `dup_ack` | 可选 `ack_no/count` | 重复 ACK；`count` 缺省为 1 |
| `timeout` | 可选 `seq` 或 `range` | RTO 事件，必须到达注入时钟的 deadline |
| `advertised_window` | `window` | 对端原始通告窗口变化 |
| `peer_data` | `bytes` | 对端数据到达 |
| `app_close` | - | 本地主动关闭 |
| `peer_fin` | - | 对端 FIN 到达 |
| `fin_ack` | - | 本地 FIN 被确认 |
| `time_wait_expire` | - | TIME-WAIT 定时器到期 |
| `reset` | - | 连接复位到 CLOSED |

超时事件若引用 `seq` 或 `range`，加载时会检查它不超过事件前已写入的总字节数；
运行时再检查它位于当前 `[snd_una, snd_nxt)`。提前于 RTO deadline 的超时返回
`E_TIMEOUT_NOT_DUE`，当前事件不提交，状态保持不变。

### 拥塞控制

```json
{
  "mss": 1000,
  "initial_cwnd": 4000,
  "initial_ssthresh": 8000,
  "fast_retransmit": true,
  "fast_recovery": true,
  "ca_algorithm": "newreno",
  "ca_increment_rounding": "floor",
  "initial_rto_ms": 200,
  "max_rto_ms": 1600,
  "rto_backoff": 2,
  "max_retries": 3,
  "dup_ack_threshold": 3,
  "timeout_cwnd": 1000,
  "fixed_point_scale": 1024,
  "clock_tolerance_ms": 0,
  "rto_reset_on_ack": true,
  "retry_scope": "consecutive"
}
```

`fixed_point_scale` 必须是 2 的幂。`ca_increment_rounding` 支持 `floor`、`ceil`、
`nearest`。`retry_scope=consecutive` 表示有效 ACK 前进后重置超时重试计数；
`connection` 表示整个连接累计。

### 观测

```json
{
  "sample_every_n_events": 2,
  "sample_times_ms": [570, 1770, 4970],
  "sample_event_types": ["connect", "timeout", "app_close", "peer_fin"],
  "include_all_events": false,
  "round_decimals": 6
}
```

报告始终包含完整 `cwnd_trace`，`observations` 按上述采样规则筛选。

## 推演语义

### 定点窗口

所有 cwnd/ssthresh 内部以 `cwnd_fixed = bytes * fixed_point_scale` 保存。
慢启动增量为确认字节数。拥塞避免使用：

```text
delta_fixed = round(MSS_fixed * ACKed_fixed / cwnd_fixed)
```

每一步都记录 `mode/numerator/denominator/quotient/remainder/direction`。超时和快速
重传的 ssthresh 使用 `max(floor(flight/2), 2*MSS)`，同样记录舍入方向。轨迹中的
`cwnd_fixed` 是权威值，`cwnd_bytes` 是精确十进制字符串。

### 发送窗口

```text
flight_limit = min(floor(cwnd), peer_advertised_window)
additional   = max(0, flight_limit - in_flight)
sendable_now = min(additional, app_end - snd_nxt)
```

新发送不会使在途字节超过该上限。窗口缩小不会回退已发送区间，因此已有在途字节可
暂时大于新上限。每个事件后输出 `flight_limit_bytes`、`in_flight_bytes`、
`sendable_now_bytes` 与 `blocked_reason`：

- `none`：当前有可发送空间。
- `app_empty`：应用没有待发送字节。
- `congestion`：cwnd 是主要限制。
- `receiver_window`：对端窗口是主要限制。
- `both`：cwnd 与接收窗口同时达到限制。
- `state:<STATE>`、`failed`：连接状态不允许发送或已失败。

### 拥塞控制

- 慢启动阶段 cwnd 按确认字节增长，跨过 ssthresh 的剩余确认量转入拥塞避免。
- 达到重复 ACK 阈值后，快速重传最老未确认段；启用快速恢复时
  `cwnd = ssthresh + threshold*MSS`。
- 部分确认在 NewReno 恢复期内重传新的最老未确认段，并保持恢复；
  确认到 `recover_seq` 后退出恢复，`cwnd = ssthresh`。
- 超时按逻辑时钟 deadline 判定，触发后
  `ssthresh=max(flight/2,2*MSS)`、`cwnd=timeout_cwnd`、RTO 指数退避，
  重传最老未确认段。
- 超时重试超过预算时返回 `E_RETRY_EXHAUSTED`，事件仍原子提交，状态进入
  `FAILED`，供 `status`/`report` 诊断。

### 状态机

支持 `LISTEN`、`SYN-SENT`、`SYN-RECEIVED`、`ESTABLISHED`、`FIN-WAIT-1`、
`FIN-WAIT-2`、`CLOSE-WAIT`、`LAST-ACK`、`TIME-WAIT`、`CLOSED`，以及诊断失败态
`FAILED`。每次真实迁移记录触发事件、逻辑时间戳、前置状态、目标状态和原因。
非法迁移抛出 `E_BAD_TRANSITION`，候选状态被丢弃。

该模型不实现 CLOSING 状态：`FIN-WAIT-1` 中收到对端 FIN 时采用确定性简化，
直接进入 `TIME-WAIT`。TIME-WAIT 时长由脚本事件控制，不读取墙钟。

## 一致性与持久化

### 双路径校验

每个事件执行两条路径：

1. 增量事件驱动推演。
2. 从场景初态开始、对当前事件前缀做全量重算。

比较 cwnd、ssthresh、状态、发送/接收窗口、在途区间、重传、迁移和完整状态快照。
不一致返回 `E_INCONSISTENCY`，`details.first_divergence` 包含首个分歧事件、两条
路径的值以及事件依赖。

源摘要是对四份 JSON 规范化对象计算的 SHA-256。`run` 首次创建 `run_id` 后，任何
摘要变化的 `run`、`resume` 或 `verify` 都返回 `E_DIGEST_MISMATCH`，不会分配新
版本或推进状态。

### SQLite 事务协议

数据库使用 WAL、`synchronous=FULL` 和 `busy_timeout=30000`。建库、PRAGMA、建表、
版本分配、事件提交和最终指针切换均由同一 `.lock` 文件锁串行化。

主要表：

- `runs`：场景名、源摘要、当前完整轨迹指针。
- `versions`：每次推演的版本、状态、事件总数、失败码、轨迹链摘要。
- `trace_events`：`(run_id, version, event_index)` 主键下的事件与完整结果。
- `version_heads`：连续已提交水位、最新完整状态快照和链摘要。
- `staging_tags`：并发分段 worker 共享的版本标签。

每个事件在同一个 `BEGIN IMMEDIATE` 事务中提交结果行和状态水位；不存在事件行已写
但状态未写或反向的半提交。版本只有全部事件连续提交后才更新 `runs.current_version`。
读者只读取该指针指向的完整版本，因此看不到正在写入的分段轨迹。

进程被强制终止后，`resume` 加载 `version_heads` 中最后一个连续提交状态，从
`contiguous_count` 继续，不重新推进此前已提交事件。重复提交同一事件/版本且内容
相同时为 no-op；内容不同返回 `E_CONFLICT`。

## 业务错误码

| 错误码 | 含义 |
| --- | --- |
| `E_BAD_JSON` | JSON 不可读或顶级结构错误 |
| `E_BAD_LINK` | RTT、窗口、缩放字段非法 |
| `E_BAD_SCRIPT` | 事件类型/排序/字段/超时引用非法 |
| `E_BAD_CONGESTION` | 拥塞配置非法 |
| `E_BAD_OBSERVATION` | 观测配置非法 |
| `E_BAD_SEGMENT` | 事件分段范围非法 |
| `E_BAD_TRANSITION` | 当前状态不允许该事件 |
| `E_BAD_ACK` | ACK 越过 `snd_nxt` 或倒退到 `snd_una` 之前 |
| `E_TIMEOUT_IDLE` | 无在途数据或无活动定时器 |
| `E_TIMEOUT_NOT_DUE` | 逻辑时钟尚未到达 RTO deadline |
| `E_RETRY_EXHAUSTED` | 超时重试预算耗尽，连接进入 FAILED |
| `E_CLOCK_REWIND` | 逻辑时钟回拨超过容差 |
| `E_DIGEST_MISMATCH` | 源摘要与持久化真值不一致 |
| `E_INCONSISTENCY` | 双路径轨迹不一致 |
| `E_TRACE_INCOMPLETE` | 尚无可读的完整轨迹 |
| `E_TRACE_CORRUPT` | 轨迹缺洞、摘要或链校验失败 |
| `E_CONFLICT` | 同版本/事件已有不同内容或被追加 |
| `E_NOTHING_TO_RESUME` | 没有未完成版本 |

## 测试与实测结果

```bash
.venv/bin/python -m unittest discover -v
./scripts/demo.sh
./scripts/concurrent.sh
```

2026-11-20 实测：

- Python 3.10.12，NumPy 2.2.6。
- 18 个单元/集成测试全部通过。
- 固定样本：24 个事件、6 次状态迁移、5 次重传，源摘要
  `994c4f0e8b3041004873e29a8ca290b598ac2ae7e6e412f496e22236c2c0f94e`。
- 两次独立运行的 `run` 输出逐字节一致，`report` 输出逐字节一致，SHA-256 为
  `ae81cf64ab92f4bdc8b60b4cc75aef4f7ac90b4559a635c0a230f0c98ae6c1b2`。
- 轨迹链摘要：
  `3178e8908a741a56232b1c3e98d2b5afbfd92fb0c74f3ed52de726156c158b92`。
- 强制终止测试在第 7 个事件后退出；`resume` 只重放剩余 17 个事件，
  最终 24 个事件 `verify` 一致，恢复报告与干净运行报告逐字节一致。
- 四进程分段提交最终版本为 `COMPLETE`，24 个事件全部验证；重复提交第一段
  幂等成功。

详细记录见 `docs/verification.md`。

