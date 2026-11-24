# 输入格式与集成说明

本项目不依赖第三方包，所有路径使用 `pathlib`，可在 Windows PowerShell、CMD 或 Linux/macOS 运行。

## v1 单文件快照

```json
{
  "schema": "sbom-audit.v1",
  "snapshot_id": "v1-baseline",
  "generation": 1,
  "ecosystem": "generic",
  "observation_clock_ms": 1795123200000,
  "records": [
    {
      "id": "lib-core",
      "version": "1.5.0",
      "algorithm": "sha256",
      "digest": "64 位小写或大写十六进制串",
      "signature_status": "signed",
      "published_relative_s": -604800
    }
  ]
}
```

`published_relative_s` 只与快照声明的 `observation_clock_ms` 相加：

```text
published_at_ms = observation_clock_ms + published_relative_s * 1000
```

不会读取系统当前时间。

## v2 分片与 SQLite

分片文件匹配 `v2_shard_*.json`，每条记录可给 `canonical_key`。SQLite 元数据库至少包含：

```sql
CREATE TABLE snapshot_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE component_metadata(
  canonical_key TEXT PRIMARY KEY,
  snapshot_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  published_at_ms INTEGER NOT NULL,
  tombstone INTEGER NOT NULL,
  revoked INTEGER NOT NULL,
  revoked_at_ms INTEGER,
  shard TEXT NOT NULL
);
```

SQLite 中的代数、绝对毫秒、墓碑和吊销标记会与分片交叉校验；任一侧缺失或不一致都会进入拒绝清单。

## 依赖声明

```json
{
  "edges": [
    {"from": "pkg:generic/app", "to": "pkg:generic/lib", "constraint": ">=1.0.0,<2.0.0"}
  ]
}
```

支持 `=`、`==`、`>`、`>=`、`<`、`<=`、`^`、`~`、`*` 和逗号/空白分隔的 AND 约束。`A -> B` 表示 `A` 依赖 `B`；当 `B` 失效时，反向边找到 `A` 及所有传递依赖方。

## 跨摘要算法

当前白名单版本为 `hash-whitelist-v1`：

- `sha256`：256 bit，64 个十六进制字符。
- `hmac-sha256`：256 bit，64 个十六进制字符。

样本同步声明支持：

- `raw-equal` / `synced`：位宽一致后直接比较归一化十六进制摘要。
- `hmac-over-digest`：按 `HMAC_SHA256(key, bytes.fromhex(sha256_digest))` 推导可比较值。
- 缺少同步标记：返回 `E_HASH_NOT_COMPARABLE` 和冲突条目，但不直接判定篡改。

## 增量批次

```json
{
  "batch_id": "batch-revoke-lib-util",
  "records": [
    {
      "canonical_key": "pkg:generic/lib-util",
      "component_id": "lib-util",
      "version": "2.0.0",
      "algorithm": "sha256",
      "digest": "...",
      "signature_status": "signed",
      "published_at_ms": 1795123199000,
      "generation": 3,
      "snapshot_kind": "incremental",
      "snapshot_id": "revocation-batch-1",
      "source_ref": "incremental/revoke_lib_util.json",
      "record_index": 1,
      "tombstone": false,
      "revoked": true
    }
  ]
}
```

重复的 `delivery_id` 或完全相同的自然键保留首次记录，并递增 `duplicate_count`。每个批次在同一个 `BEGIN IMMEDIATE` 事务中写批次、记录和边；增量结果与全量重算不一致时返回 `E_RECOMPUTE_DIVERGED` 并回滚。

## 调度预算

`Budget` 字段：

- `concurrency`：worker 数。
- `batch_timeout_ms`：真实 `time.perf_counter()` 墙钟上限。
- `queue_limit`：除运行 worker 外允许等待的批次数；总槽位为 `concurrency + queue_limit`。
- `max_digest_bytes_per_component`：单构件摘要计算字节量上限。

`Batch.work_items` 可声明真实分块摘要工作量：

```json
{
  "batch_id": "budget-probe",
  "work_items": [
    {"canonical_key": "pkg:generic/big", "bytes": 16777216, "algorithm": "sha256"}
  ]
}
```

工作量按 64 KiB 分块真实执行 SHA-256/HMAC 更新，并在块间检查墙钟与预算；没有使用 `sleep` 模拟超时。

## 检查点恢复

检查点同时写入 SQLite `checkpoints` 表和 JSON 文件。JSON 写入流程为：

1. 在同目录创建 `.checkpoint.json.*.tmp`。
2. 写入并 `fsync`。
3. 重新解析临时 JSON。
4. `os.replace()` 原子替换正式文件。

恢复时先隔离并返回 `E_HALF_WRITE_RESIDUE` 临时残留，再核对：

- `checkpoint_version`
- `hash_whitelist_version`
- `merge_policy_version`
- 外层检查点摘要
- 报告 `view_digest`
- SQLite/JSON 副本一致性
- 水位之后已提交批次的重放状态

任一策略版本变化均要求整体重建，不允许旧口径增量续接。
