# JSONL TCP 协议

## 1. 分帧与连接

- 监听地址必须是显式给定的回环地址，例如 `127.0.0.1:19160`。
- 每个请求和响应都是一个 UTF-8 JSON 对象，以 `\n` 结束。
- 一个 TCP 连接可顺序发送多个请求；客户端按行读取对应响应。
- 单行上限 2 MiB；超过上限返回 `E_PROTOCOL` 并关闭连接。

通用响应字段：

```json
{
  "ok": true,
  "code": "OK",
  "request_id": "req-1",
  "type": "append"
}
```

失败响应示例：

```json
{
  "ok": false,
  "code": "E_FENCE",
  "message": "partition already has an active delivery window",
  "request_id": "req-2",
  "type": "pull",
  "holder": "consumer-a",
  "fields": {
    "partition_key": "orders.eu",
    "window_start": 1,
    "window_end": 3
  }
}
```

## 2. 业务码

| 业务码 | 含义 |
| --- | --- |
| `OK` | 成功。 |
| `E_APPEND` | 分区键、生产者序列号或消息体不合法。 |
| `E_FENCE` | 当前分区已有未到期窗口，或提交遇到已胜出的超时。 |
| `E_COMMIT` | 没有窗口、窗口不属于该会话、提交越窗/回退/跳跃或解阻参数不合法。 |
| `E_POISON` | 位点重投超过上限，分区阻塞。 |
| `E_LEDGER` | SQLite 打开、版本、恢复校验或账本约束失败。 |
| `E_VERIFY` | 独立验证器发现顺序或账本不一致。 |
| `E_PROTOCOL` | JSON、命令类型、limit 或超时参数不合法。 |
| `E_INTERNAL` | 事务或内部执行错误。 |

## 3. `append`

请求：

```json
{
  "type": "append",
  "request_id": "eu-1",
  "partition_key": "orders.eu",
  "producer_session": "producer-eu",
  "producer_seq": 1,
  "body": "eu-01"
}
```

约束：

- `partition_key` 匹配 `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`。
- `producer_seq > 0`，且同一生产者会话内严格递增。
- `body` UTF-8 字节数不超过 `--max-body-bytes`（默认 65536）。

成功响应：

```json
{
  "ok": true,
  "code": "OK",
  "request_id": "eu-1",
  "type": "append",
  "partition_key": "orders.eu",
  "offset": 1,
  "next_offset": 2
}
```

返回前，消息和位点已经持久化提交。

## 4. `pull`

请求：

```json
{
  "type": "pull",
  "request_id": "pull-1",
  "partition_key": "orders.eu",
  "consumer_session": "consumer-a",
  "limit": 3,
  "timeout_ms": 5000
}
```

成功响应：

```json
{
  "ok": true,
  "code": "OK",
  "request_id": "pull-1",
  "type": "pull",
  "partition_key": "orders.eu",
  "session": "consumer-a",
  "window": {
    "holder": "consumer-a",
    "start_offset": 1,
    "end_offset": 3,
    "deadline_unix_ms": 1794859506304,
    "ttl_ms": 5000
  },
  "messages": [
    {
      "partition_key": "orders.eu",
      "offset": 1,
      "producer_session": "producer-eu",
      "producer_seq": 1,
      "body": "eu-01",
      "appended_unix_ms": 1794859500000,
      "retry_count": 0
    }
  ],
  "retry_counts": {}
}
```

同一个分区已有未到期窗口时返回 `E_FENCE` 和 `holder`。没有新消息时返回成功、空 `messages`，不创建窗口。

## 5. `commit`

请求：

```json
{
  "type": "commit",
  "request_id": "commit-1",
  "partition_key": "orders.eu",
  "consumer_session": "consumer-a",
  "offset": 2
}
```

提交值是当前窗口中已经成功处理的连续前缀终点。成功响应：

```json
{
  "ok": true,
  "code": "OK",
  "request_id": "commit-1",
  "type": "commit",
  "partition_key": "orders.eu",
  "committed_offset": 2,
  "confirmed_offset": 2,
  "window": {
    "holder": "consumer-a",
    "start_offset": 3,
    "end_offset": 3,
    "deadline_unix_ms": 1794859506304,
    "ttl_ms": 5000
  }
}
```

提交到窗口尾部时不返回 `window`。

## 6. `fail`

请求：

```json
{
  "type": "fail",
  "request_id": "fail-1",
  "partition_key": "orders.eu",
  "consumer_session": "consumer-a",
  "offset": 3,
  "reason": "E_INJECTED_TRANSIENT_PROCESSING_FAILURE"
}
```

成功响应表示窗口已关闭，下一次拉取将从 `redelivery_from` 开始整体重投。若重投超限，返回 `E_POISON`，并携带：

```json
{
  "blocked_offset": 3,
  "blocked_retry_count": 4
}
```

## 7. `unblock`

人工清理阻塞位点：

```json
{
  "type": "unblock",
  "request_id": "unblock-1",
  "partition_key": "orders.eu",
  "operator": "operator@local",
  "expected_blocked_offset": 3
}
```

`expected_blocked_offset` 可省略；提供时必须与账本一致。解阻会清除未提交区间的重投计数，并记录 `unblock` 事件。

## 8. `status` / `inspect`

按分区返回账本和窗口。可由 `partition_key` 过滤：

```json
{
  "type": "status",
  "request_id": "status-1",
  "partition_key": "orders.eu"
}
```

响应中的 `status` 包含格式版本、最大重投次数、每个分区的尾位点、已提交/已确认位点、窗口、重投计数、待重投起点、阻塞信息、账本版本和状态修订号。

## 9. `ping`

```json
{"type":"ping","request_id":"ping-1"}
```

返回 `OK`，用于就绪探测。
