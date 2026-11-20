# 设计说明

## 1. 边界与目标

本项目只处理本地回环地址上的确定性隧道实验：

- 输入：三个 JSON 文档（对端、链路画像、转发策略）。
- 发送端：确定性分片、直发/聚合裁决、逻辑时钟调度、SQLite 状态持久化。
- 接收端：本地回环 UDP/TCP 回显服务，按 `message_id + seq` 重组并回报 SHA-256。
- 不实现真实 VPN 封装、认证、密钥协商或外部网络访问。

## 2. 数据模型

### 对端

```json
{
  "name": "udp-alpha",
  "protocol": "udp",
  "listen": {"host": "127.0.0.1", "port": 39001},
  "outer_mtu": 1200,
  "payload_lengths": [96, 384, 640, 1024],
  "link_weight": 1.0,
  "encrypted": true
}
```

`payload_lengths` 是可观测的内层载荷长度分布。每个样本都必须满足：

```text
payload_len <= outer_mtu - 64(header) - crypto_overhead
```

### 链路画像

`links.profiles.<peer>` 包含 `rtt_ms`、`loss_rate`、`bandwidth_mbps`、
`jitter_ms`。`links.scenarios` 可以按注入逻辑时钟列出链路变化；场景事件只
影响对应 peer。

### 策略

`fragment_threshold_bytes` 是外层数据报的尺寸阈值：

```text
fragment_capacity =
    min(outer_mtu, fragment_threshold_bytes) - 64 - crypto_overhead
```

若 `payload_len + 64 + crypto_overhead <= fragment_threshold_bytes`，该载荷
可以单包直发；否则拆分为连续分片。

## 3. 分片格式

固定 64 字节大端头：

| 字段 | 说明 |
|---|---|
| magic/version | `TKF1` / 1 |
| flags | final、encrypted |
| header_len | 固定 64 |
| sequence | 从 0 连续递增 |
| fragment_count | 发送时预计的总片数 |
| payload_len/original_len | 当前片与原始载荷长度 |
| message_id | 128-bit 确定性消息 ID |
| payload_digest | 原始载荷 SHA-256 前 128 位 |
| crypto_overhead | 加密占位字节数 |
| header_crc | 头部 CRC32 |

加密对端在头部与载荷之间写入确定性的零字节占位，不执行真实加密。分片
载荷摘要使用完整报文 SHA-256；相同输入重复调用得到逐字节一致的头、载荷
和摘要。

聚合报文格式为 `TKA1` 头 + 若干个 `uint16 长度 + 分片报文`，整个外层
数据报不超过当前 MTU。已发送分片的原始字节会持久化；MTU 下调后重传时
不得重新分片或改尺寸。

## 4. 重组诊断

接收方以序列号存储分片，并按序拼接。业务错误码区分：

- `E_OUT_OF_ORDER`：严格顺序模式观察到乱序。
- `E_MISSING_FRAGMENT`：结束片已出现但中间序号缺失。
- `E_DUPLICATE_FRAGMENT`：相同序号、相同字节的重复片。
- `E_CONFLICTING_FRAGMENT`：相同序号、不同字节。
- `E_DIGEST_MISMATCH`：整体长度或 SHA-256 不匹配。
- `E_BAD_FRAGMENT`：魔数、版本、长度、CRC 或字段非法。

网络接收器使用宽容重复模式：重复片被计数但仍可完成重组，以支持“确认
丢失后的幂等重传”。库 API `Reassembler` 默认采用严格重复诊断。

## 5. 算法裁决

裁决只读取以下显式观测项：

- 当前内层载荷字节数；
- 对端载荷长度分布的 p50/p95/max；
- MTU、分片阈值、加密占位开销；
- 链路带宽、RTT、丢包率、抖动；
- 每轮并发上限与逻辑时间预算；
- 配置列出的采样点。

选择规则：

1. `wire_load <= fragment_threshold_bytes`：选择 `direct`。
2. 高负载且聚合窗口至少为 2：选择 `aggregate`。
3. 高负载但带宽/MTU/并发只允许窗口 1：回退 `direct`，并给出原因。

输出同时包含被否决算法、触发条件、估计轮数、聚合窗口、显式采样点及每个
采样点的选择。NumPy 百分位计算固定使用 `method="linear"`，因此同一环境
下结果稳定。

## 6. 逻辑时钟与 I/O 超时

调度器只使用注入的 `logical_clock_ms`：

- 每个发送事件、聚合窗口边界、链路变化生效时刻；
- 重传退避 `min(max_backoff, base_backoff * 2^(attempt-1))`；
- 每轮允许的并发槽位 `min(max_concurrency, round_budget / step)`。

系统墙钟不参与任何调度或裁决。真实网络等待使用 `select`/socket 的非阻塞
超时；`monotonic` 仅用于判断 I/O 等待是否耗尽，不写入发送轨迹。`threading.Event`
可在等待期间主动中止。

## 7. SQLite 状态与原子性

主要表：

- `tunnels`：当前 MTU、逻辑时钟水位、算法、确认水位、重传计数、重组摘要、
  最终 trace。
- `fragments`：每个 `(tunnel_id, message_id, seq)` 的固定报文、原始偏移、
  状态、尝试次数、最后发送逻辑时刻。
- `fragment_acks`：幂等确认日志，主键为 `(tunnel_id, message_id, seq)`。

每次 `BEGIN IMMEDIATE` 写事务同时持有数据库文件锁。读取使用 WAL 已提交
快照，不会看到半更新状态。

发送前先把分片从 `pending` 原子改为 `inflight` 并增加 attempt；只有收到
回显确认后才改为 `confirmed`。确认水位只从 0 开始连续推进，绝不把未确认
分片计为已送达。

## 8. 崩溃恢复

强制终止后可能出现：

1. 分片已标记 `inflight`，但报文未发出；
2. 报文已发出，但确认事务未提交；
3. 部分分片已确认，尾部仍待发。

`resume` 先在同一隧道会话锁下把所有 `inflight` 原子恢复为 `pending`，保留
所有 `confirmed` 行和原报文 BLOB。恢复器只重发未确认序号，回显端按幂等
重复处理。确认水位和已确认字节数不会重复累加。

同一隧道发送会话使用独立 advisory file lock。两个实例同时操作同一隧道时，
后启动者得到 `E_TUNNEL_LOCKED`；不同隧道可并行。SQLite 主键和确认日志
保证同一版本同一序号最多确认一次。

## 9. 已知边界

- 回显状态在单进程内存中；`verify` 的远端摘要检查要求当前回显进程接收过
  该消息。回显重启后应使用 `verify --no-echo` 检查持久化状态。
- 本项目不模拟 IP 层 MTU 分片，也不实现真实加密。
- 强制终止不保证生成退出报告；恢复依赖 SQLite 已提交状态。
