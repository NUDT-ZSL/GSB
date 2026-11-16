# JSON 行协议

## 传输

- 仅监听 `127.0.0.1`、`::1` 或 `localhost`。
- 每个请求是一行 UTF-8 JSON，以 `\n` 结束。
- 单行上限 4 MiB。
- 发布连接可连续发送多个 `publish`。某条消息校验失败只返回错误，连接保持可用。
- 消费者连接首个成功请求必须是 `subscribe`；之后只接受 `ack`。

## 发布请求

```json
{
  "type": "publish",
  "semantics": "at_least_once",
  "message": {
    "version": 3,
    "delivery_id": "m-001",
    "body": {"text": "hello"},
    "timestamp_ms": 1700000000000,
    "retry_count": 0,
    "partition_key": "orders",
    "idempotency_seq": 1001
  }
}
```

`semantics` 可写为 `delivery_semantics`，二者不能同时出现。接受 `at_most_once`、`at_least_once`、`exactly_once`，连字符形式会在解析时归一化。省略时默认为 `at_least_once`。

### 版本字段集合

字段集合是严格的。出现当前版本不支持的字段返回 `E_FORMAT`。

| 版本 | 必需字段 |
|---|---|
| v1 | `version`, `delivery_id`, `body` |
| v2 | v1 + `timestamp_ms`, `retry_count` |
| v3 | v2 + `partition_key`, `idempotency_seq` |

规则：

- `version` 必须是整数 1、2 或 3；未知值返回 `E_VERSION`。
- `delivery_id` 必须是非空字符串。
- `body` 必须是合法且非 `null` 的 JSON 值。
- `timestamp_ms` 是严格整数，不接受小数或指数形式。
- `retry_count` 是非负整数。
- `partition_key` 是非空字符串。
- `idempotency_seq` 是非负整数；`exactly_once` 要求 v3 且值大于 0。

## 发布响应

新消息被接受：

```json
{
  "ok": true,
  "type": "publish_ack",
  "accepted": true,
  "duplicate": false,
  "delivery_id": "m-001",
  "global_offset": 7,
  "source_version": 3,
  "semantics": "exactly_once",
  "idempotency_seq": 1001,
  "dedup_key": "idem:1001"
}
```

重复幂等键：

```json
{
  "ok": true,
  "type": "publish_ack",
  "accepted": false,
  "duplicate": true,
  "delivery_id": "m-001",
  "global_offset": 7,
  "source_version": 3,
  "semantics": "exactly_once",
  "idempotency_seq": 1001,
  "dedup_key": "idem:1001",
  "existing_result": {
    "delivery_id": "m-001",
    "global_offset": 7,
    "accepted_at": "2026-01-01T00:00:00Z"
  }
}
```

## 订阅

```json
{"type":"subscribe","consumer_id":"viewer-v2","version":2,"partition_key":"orders"}
```

- `consumer_id` 必须非空；一个活动订阅占用一个消费者标识。
- `version` 必须是 1、2 或 3，表示消费者可接受的声明版本。
- 可省略 `partition_key`；非空时只接收该分区消息。
- 可省略 `semantics`，仅作为提示记录。

响应：

```json
{"ok":true,"type":"subscribe_ack","consumer_id":"viewer-v2","version":2,"partition_key":"orders","session_id":"sess-000001","protocol":"message-interop/1"}
```

之后服务端推送 `delivery`：

```json
{
  "type": "delivery",
  "delivery_id": "m-001",
  "source_delivery_id": "m-001",
  "consumer_id": "viewer-v2",
  "consumer_version": 2,
  "consumer_offset": 3,
  "global_offset": 7,
  "attempt": 2,
  "payload": {
    "version": 2,
    "delivery_id": "m-001",
    "body": {"text": "hello"},
    "timestamp_ms": 1700000000000,
    "retry_count": 1
  },
  "conversion": {
    "conversion_id": "conv-000007-8829dfd9-02",
    "source_delivery_id": "m-001",
    "delivery_id": "m-001",
    "source_version": 3,
    "declared_version": 2,
    "direction": "downgrade",
    "source_fields": ["version", "delivery_id", "body", "timestamp_ms", "retry_count", "partition_key", "idempotency_seq"],
    "target_fields": ["version", "delivery_id", "body", "timestamp_ms", "retry_count"],
    "stripped_fields": ["partition_key", "idempotency_seq"],
    "filled_fields": [],
    "mutated_fields": ["retry_count"],
    "field_origins": {
      "version": "declared",
      "delivery_id": "source",
      "body": "source",
      "timestamp_ms": "source",
      "retry_count": "mutated"
    }
  },
  "semantic": {
    "mode": "at_least_once",
    "decision": "redeliver_unacked",
    "reason": "previous delivery was not acknowledged",
    "retry_count": 1
  }
}
```

### 转换元数据

- `direction`：`same`、`upcast`、`downgrade`。
- `source_fields` / `target_fields`：源版本与声明版本定义的字段集合。
- `stripped_fields`：源有、目标版本无，按源字段顺序记录。
- `filled_fields`：目标有、源无，按目标字段顺序记录。
- `mutated_fields`：源字段因重试语义被改写；目前为 `retry_count`。
- `field_origins`：
  - `source`：直接来自源消息；
  - `default`：为高版本补齐的确定性默认值；
  - `derived`：由语义推导，如低版本源的重试次数；
  - `mutated`：源字段因重试被改写；
  - `declared`：目标载荷的 `version` 值。

`semantic.idempotency_seq` 和 `semantic.dedup_key` 即使在 v1/v2 载荷中被剥离，也保留在投递元数据中，因此降级不破坏恰好一次判定。

## 确认

```json
{"type":"ack","consumer_id":"viewer-v2","delivery_id":"m-001","offset":3}
```

响应：

```json
{"ok":true,"type":"ack_ack","acked":true,"already_acked":false,"consumer_id":"viewer-v2","delivery_id":"m-001","offset":3}
```

- `consumer_id` 必须匹配当前订阅。
- `offset` 必须匹配该消费者最近一次该消息的 `consumer_offset`。
- 重复确认返回 `already_acked=true`，不会追加确认记录。

## 错误码

| 错误码 | 含义 |
|---|---|
| `E_FORMAT` | JSON、字段集合、字段类型或值格式错误 |
| `E_VERSION` | 未知消息或订阅版本 |
| `E_SEMANTIC` | 语义非法或恰好一次缺少有效幂等键 |
| `E_PROTOCOL` | 请求类型、连接状态或行协议错误 |
| `E_CONFLICT` | 消费者已有活动订阅、确认消费者不匹配或偏移冲突 |
| `E_NOT_FOUND` | 确认引用未知消费者/投递 |
| `E_INTERNAL` | 本地持久化失败 |
| `E_VERIFY` | 离线验证工具发现状态不一致 |

错误响应：

```json
{"ok":false,"type":"error","error":{"code":"E_VERSION","message":"unsupported message version 9","details":{"supported_versions":[1,2,3]}}}
```

## 健康检查

```json
{"type":"ping"}
{"ok":true,"type":"pong","protocol":"message-interop/1"}
```
