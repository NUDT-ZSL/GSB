# Loopback JSON-Lines Protocol

## Transport

- TCP, IPv4 loopback only (`127.0.0.1` / `localhost` resolved by the client to IPv4).
- UTF-8 JSON object per line.
- Maximum request/response frame: 32 MiB.
- Protocol version: `1`.
- Request ids are echoed exactly; a mismatch is a protocol error.

## Request

```json
{"protocol_version":1,"request_id":"...","op":"DIGEST_QUERY","payload":{"block_id":"B-SINGLE","shard_layout":[{"index":0,"offset":0,"byte_size":512}]}}
```

Operations:

| Op | Payload | Data result |
| --- | --- | --- |
| `PING` | `{}` | node id, protocol version, persistent block ids |
| `HELLO` | `{}` | node id, loopback marker |
| `DIGEST_QUERY` | `block_id`, optional `shard_layout` | byte size, full SHA-256, shard SHA-256 list |
| `BLOCK_GET` | `block_id` | byte size, SHA-256, base64 block bytes |
| `BLOCK_PUT` | `block_id`, `data_base64`, `expected_digest`, `expected_byte_size` | committed byte size and digest |

## Response

Success:

```json
{"protocol_version":1,"request_id":"...","ok":true,"data":{}}
```

Business failure:

```json
{"protocol_version":1,"request_id":"...","ok":false,"error":{"code":"E_BLOCK","message":"...","field_path":"protocol.block_id","node_id":"n1","block_id":"B-SINGLE","details":{}}}
```

The client re-raises the corresponding domain exception so callers do not need to
parse transport errors manually.

## Durability contract

`BLOCK_PUT` performs:

1. validate expected byte size and full SHA-256;
2. create a temporary file in the node directory;
3. write, flush, and `fsync` the temporary file;
4. `os.replace` it over the logical block file;
5. append and `fsync` a `PUT_COMMIT` protocol journal record.

Startup (`NODE_RECOVERY_SCAN`) scans persistent block files and replays journal
metadata. It explicitly reports `exit_report: false`; a forcibly stopped process is
never assumed to have written any shutdown artifact.

## Logging

Each node writes JSONL protocol records with timestamp, event, node id, block id,
field path, and request context. CLI-level failures additionally write
`ANOMALY_<CODE>` records.
