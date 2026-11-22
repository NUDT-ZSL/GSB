# quicsim-local

本地、确定性、单进程或多进程均不访问真实网络的 QUIC 丢包恢复与拥塞控制推演引擎。项目使用注入逻辑时钟、整数定点窗口账本、SQLite 事务轨迹和双路径逐事件比对。

## 快速开始

要求 Python 3.11+。当前验证环境使用 Python 3.12.16：

```bash
# 已安装 uv 时可复现环境
uv python install 3.12
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python numpy pytest

# 或使用已有 Python 3.11+ 环境
python3.11 -m venv .venv
.venv/bin/pip install -e '.[test]'

# 完整演示：基本场景、丢失/PTO 场景、强制终止与恢复
./scripts/demo.sh

# 测试
.venv/bin/python -m pytest -q
```

项目不使用 Docker、容器、外网或真实 QUIC 对端。SQLite 由标准库 `sqlite3` 提供。

本机实测运行时：Python `3.12.16`、numpy `2.5.5`、pytest `9.2.0`、SQLite `3.53.1`。

## 目录

```text
data/                可重复的本地 JSON 场景
state/               SQLite 数据库、锁文件和演示证据
quicsim/scene.py     场景解析、规范化与业务校验
quicsim/model.py     包状态、流缓冲、区间代数、定点窗口和状态序列化
quicsim/engine.py    单路径事件驱动状态机与全量重算能力
quicsim/storage.py   SQLite schema、事务、文件锁、恢复水位校验
quicsim/pipeline.py  双路径提交、版本轨迹、status/verify
quicsim/cli.py       run/resume/status/verify
tests/               29 个本地确定性测试
scripts/demo.sh      一键演示与证据留存
docs/DESIGN.md       状态迁移、提交与并发恢复技术说明
```

## 场景格式

顶层字段：

- `scene_id`：非空字符串。
- `connection`：`initial_max_data`、`initial_max_streams`、`initial_max_stream_data`、`max_udp_payload_size`、`packet_overhead_bytes`、`ack_delay_factor`。
- `congestion`：初始拥塞窗口/SSTHRESH（包）、最小窗口、包号阈值、拥塞避免参数、持久拥塞参数、PTO 初始 RTT、最小 RTT、粒度与退避上限。
- `observations`：本地观测配置；每个事件始终写入完整规范化快照。
- `events`：从 `seq=1` 连续编号、`time_ms` 单调不减的事件数组。

`max_udp_payload_size` 必须位于 QUIC 允许的 `[1200, 65527]`，且必须大于 `packet_overhead_bytes`。

事件类型：

| 类型 | 关键字段 | 语义 |
|---|---|---|
| `write_stream` | `stream_id, offset, length, fin` | 写入应用缓冲；不直接发送 |
| `send` | `space, count, ack_ranges?` | 在指定包号空间分配单调包号并发送 STREAM/ACK 帧 |
| `ack` | `ranges[], ack_delay_ms, received_at_ms` | 跨空间 ACK；区间为闭区间，可重叠、乱序 |
| `start_probe` | 可选 `packet{space,number}` | 启动 PTO 探测并固定本次 deadline |
| `probe_timeout` | 可选 `packet{...}` | 只有活动探测已到期才计为超时 |
| `abandon_probe` | 可选 `reason` | 区分启动前放弃和启动后放弃 |
| `path_mtu` | `mtu` | 更新有效路径 MTU |
| `max_data` | `max_data` | 只允许连接级额度增加 |
| `max_stream_data` | `stream_id, max_stream_data` | 只允许流级额度增加 |
| `connection_close` | `error_code, reason?` | 关闭连接 |

示例见 `data/basic_scene.json` 与 `data/loss_pto_scene.json`。

## 核心机制

### 多包号空间与 ACK

`initial`、`handshake`、`application` 各自维护 `next_pn` 与包状态。ACK 闭区间先按 `space` 拆分，再转换成半开区间合并重叠部分；已确认包不会重复增加窗口或再次进入丢包判定。每个包的 `transitions` 记录 `status`、`trigger`、`event_seq` 和 `time_ms`。

### 丢包与持久拥塞

- 包号阈值：`largest_acked - packet_number >= packet_threshold`。
- 时间阈值：`now - sent_at > current_pto`。
- 两种来源都调用同一个状态迁移入口，只处理 `in_flight` 包，因此同一包只丢失一次。
- 每次事件出现新丢失时只执行一次窗口缩减。
- 连续丢失包覆盖的发送时间达到 `persistent_congestion.threshold_ms` 时进入持久拥塞，窗口降至 `min_cwnd_packets`。

### 定点拥塞窗口

窗口使用整数定点 `SCALE=1_000_000` 包。拥塞避免每次确认登记：

```text
numerator   = alpha_num * SCALE * SCALE + carry_before
denominator = alpha_den * cwnd_fixed
increment   = floor(numerator / denominator)
carry_after = numerator % denominator
```

`carry_before`、`carry_after`、`quotient`、`cwnd_before_fixed` 与 `cwnd_after_fixed` 均写入状态和 SQLite `rounding_ledger`。下一次增量继续使用前次余数，不使用任何浮点数。

### 联合可发送判定

每个事件快照包含：

- `congestion.remaining_packets` 与 `remaining_fixed`。
- `flow_control.remaining_bytes`。
- 各流 `sendable_ranges`、`sendable_bytes`、流级剩余额度。
- `sendable.sendable_packets`、`sendable_bytes`、`flow_controlled_new_bytes`、`already_consumed_bytes` 与限制来源 `limiters`。

发送包数取 `min(cwnd 包号槽位, 数据所需包数)`。连接级和流级流控以绝对流偏移记账；`flow_controlled_new_bytes` 受两个额度约束，`sendable_bytes` 同时包含可在已消费偏移内重传的字节，并通过 `already_consumed_bytes` 单独登记。流量控制不足不会回退拥塞窗口，反之亦然。所有流、包号空间和限制来源均按稳定顺序输出。

### PTO

```text
PTO = srtt + max(4 * rttvar, granularity)
      + ceil(ack_delay_factor * latest_ack_delay)
```

只使用注入的 `time_ms`、平滑 RTT 与 ACK 延迟。启动探测时固定 deadline；延迟 ACK 会增大 PTO，因此不会使探测提前到期。退避倍率从 1 开始翻倍并受 `max_backoff` 限制。超过上限时状态进入 `failed`，错误为 `E_PTO_BACKOFF_LIMIT`，轨迹仍可验证。

## 双路径与验证

每个待提交事件同时产生：

1. 从已持久化水位恢复的增量路径快照。
2. 从事件 1 开始重新创建引擎并全量重算的快照。

两份快照先做字段级比较，再分别写入 `trace_events(path='incremental'|'full')`。`verify` 只从 SQLite 读取轨迹，并额外执行独立重放，检查：

- 增量与全量轨迹逐事件一致。
- 独立重放与两条持久化轨迹一致。
- 当前状态、运行版本和状态哈希一致。
- `event_commits`、`trace_events`、`trace_verifications` 连续且无遗漏。
- `rounding_ledger` 序号、内容和哈希一致。

首个分歧输出事件号、字段、两条路径值、完整中间快照、当时 ACK 区间/接收时间/ACK 延迟以及舍入登记序号。可故意篡改一条持久化快照来复现该诊断。

## SQLite 与恢复

数据库包含：

- `scenes`：场景哈希与完整 JSON。
- `sessions` / `runs`：每次提交的新版本和状态。
- `current_state`：原子替换的当前版本指针、水位、状态 JSON 与哈希。
- `event_commits`：`(session_id,event_seq)` 唯一提交账本。
- `trace_events`：不可变、按版本保存的增量/全量快照。
- `trace_verifications`：逐事件双路径结论。
- `rounding_ledger`：拥塞避免舍入序列。
- `divergence_reports`：提交时或验证时发现的分歧。

`run/resume` 每个批次在 `flock` 文件锁和 `BEGIN IMMEDIATE` 中执行。新轨迹完整写入后再替换 `current_state` 指针；读取方通过 SQLite 快照事务只能看到完整提交版本。恢复先检查已提交事件数、最大序号、两条轨迹行数和状态哈希，只从 `watermark_event` 之后继续。缺失轨迹或多余事件会返回 `E_RECOVERY`，不会声称轨迹完整。

同一场景上的多个 `resume` 进程通过锁串行推进；`(session_id,event_seq)` 主键保证同一事件只提交一次。重复 `run` 已完成场景时返回 `idempotent=true`，不生成新版本。

## 业务错误码

| 错误码 | 含义 |
|---|---|
| `E_BAD_SCENE` | 场景结构、整数范围或协议参数非法 |
| `E_ACK_RANGE` | ACK 下界大于上界，或超出该空间已发送包号 |
| `E_FC_REGRESS` | `MAX_DATA` / `MAX_STREAM_DATA` 回退 |
| `E_PTO_UNSENT` | 探测事件引用尚未发送的包号 |
| `E_PTO_BACKOFF_LIMIT` | PTO 退避超过配置上限，连接进入失败态 |
| `E_TRACE_DIVERGENCE` | 双路径轨迹分歧 |
| `E_STATE_EXISTS` | 已存在未完成会话却使用 `run` |
| `E_RECOVERY` | 提交水位、轨迹或状态不完整 |
| `E_SCENE_MISMATCH` | 数据库场景哈希与请求场景不一致 |

非法场景在打开数据库前完成全量预检，因此不会创建数据库、锁文件或任何状态。

## 命令行

```bash
# 新建并推进；已完成场景的重复执行是幂等的
.venv/bin/python -m quicsim run \
  --scene data/basic_scene.json --db state/engine.sqlite --commit-every 5

# 从中断水位恢复，也可限制单次推进事件数
.venv/bin/python -m quicsim resume \
  --scene data/basic_scene.json --db state/engine.sqlite \
  --commit-every 5 --max-events 3

# 稳定排序 JSON：包号空间最大包号、largest_acked、在途摘要、
# cwnd/ssthresh、流控余量、PTO 状态与逻辑时钟水位
.venv/bin/python -m quicsim status --db state/engine.sqlite

# 只读双路径、独立重放、事件账本与舍入账本验证
.venv/bin/python -m quicsim verify \
  --db state/engine.sqlite --scene data/basic_scene.json

# 实际强制退出注入，用于恢复测试
.venv/bin/python -m quicsim run \
  --scene data/basic_scene.json --db state/crash.sqlite \
  --commit-every 2 --crash-after 2
```

退出码：`0` 成功且轨迹完整；`1` 验证不通过；`2` 业务/场景/恢复错误；`3` 连接进入诊断失败态；`4` 双路径分歧。

## 实际验证结果

```text
$ .venv/bin/python -m pytest -q
.............................                                            [100%]
29 passed
```

本地样本验证覆盖：

- `basic-cross-space`：30 事件、三个包号空间、跨空间重叠 ACK、慢启动转拥塞避免、4 次定点舍入、路径 MTU、探测未到期/超时/启动前后放弃、关闭；双路径与独立重放全部一致。
- `loss-and-pto`：18 事件，包号阈值丢失 8 包、时间阈值丢失 2 包，发送 17 包；重传与流量控制账本保持完整；全部验证通过。
- 强制终止测试在已提交水位 4 处退出码 137，`resume` 后水位 30，最终状态与未中断基线逐字段一致。
- 4 个并发 `resume --max-events 1` 进程各自只提交一个事件，事件 1–4 各有唯一版本；随后完整恢复并通过验证。
