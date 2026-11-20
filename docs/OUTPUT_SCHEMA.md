# JSON 输出与业务码

## 错误信封

CLI 失败时向 stderr 输出；HTTP 失败时返回 JSON 和对应状态码。

```json
{
  "ok": false,
  "error": {
    "code": "E_CHUNK_MISMATCH",
    "message": "chunked decoding drifted from whole-sentence decoding",
    "field": "confidence",
    "exit_code": 3,
    "data": {
      "block_index": 0,
      "first_deviation_step": 2,
      "deviation_field": "confidence",
      "expected": 0.991762084973,
      "actual": 1.001762084973,
      "completed_stage": "chunk_prefix_merge"
    }
  }
}
```

`exit_code` 始终是整数。Windows 与 POSIX 使用相同映射；业务码不依赖特定进程退出码承载。

| code | exit_code | 必带诊断字段 |
|---|---:|---|
| `E_CONFIG_INVALID` | 2 | `field` |
| `E_TOKEN_UNKNOWN` | 2 | `field`, `data.index`, `data.token` |
| `E_ALIGN_INVALID` | 2 | `field` |
| `E_DUP_CASE` | 2 | `field`, `data.case_id` |
| `E_CHUNK_MISMATCH` | 3 | `block_index`, `first_deviation_step`, `deviation_field` |
| `E_INCONSISTENT` | 3 | `block_index`, `category` |
| `E_SELF_CHECK` | 3 | `failed_checks` |
| `E_LATENCY_BUDGET` | 4 | `observed_ns`, `estimated_ns`, `budget_ns`, `completed_stage`, `decoded_steps` |
| `E_RESOURCE_LIMIT` | 5 | `field`, 当前值与上限 |
| `E_STAGE_BUSY` | 6 | `stage`, `owner`, `waited:false` |
| `E_CACHE_CORRUPT` | 7 | `entry_key`, `version`, `category` |
| `E_SNAPSHOT_ROLLBACK` | 8 | `kind`, 当前/尝试版本 |

## 已就绪块查询

`GET /api/status` 或 `StreamingSession.get_ready_blocks()`：

```json
{
  "case_id": "local-seed-20261120",
  "plan_id": "sha256...",
  "ready_block_count": 1,
  "block_count": 3,
  "all_ready": false,
  "ready_tokens": ["tgt_08", "tgt_15"],
  "blocks": [
    {
      "block_index": 0,
      "start_step": 0,
      "end_step_exclusive": 4,
      "tokens": ["tgt_08", "tgt_15", "tgt_22", "tgt_05"],
      "confidences": [0.992635636377, 0.988781372282, 0.991762084973, 0.980399991085],
      "projections": [[0.1, -0.2]],
      "source_version": 1,
      "align_version": 1,
      "cache_entry_keys": ["sha256..."],
      "timing": {},
      "semantic_hash": "sha256...",
      "degraded": false,
      "degradation_category": null
    }
  ]
}
```

## 单块候选

`GET /api/blocks/{block_index}/candidates` 或 `query_candidates(block_index)`：

```json
{
  "block_index": 0,
  "start_step": 0,
  "end_step_exclusive": 4,
  "steps": [
    {
      "step": 0,
      "block_index": 0,
      "token": "tgt_08",
      "confidence": 0.992635636377,
      "projection": [0.12, -0.08],
      "candidate_log_normalizer": 7.91,
      "attention_log_normalizer": 0.11,
      "context_source_indices": [0, 1, 2, 3],
      "state_hash": "sha256...",
      "candidates": [
        {
          "token": "tgt_08",
          "score": 8.94,
          "probability": 0.992635636377,
          "rank": 1,
          "has_current_alignment": true,
          "aligned_source_indices": [0],
          "attention_weights": [1.0]
        }
      ]
    }
  ]
}
```

## 计时记录

```json
{
  "stage": "chunk_first_block",
  "observed_ns": 11369119,
  "observed_ms": 11.369119,
  "estimated_ns": 186900,
  "estimated_ms": 0.1869,
  "step_count": 4,
  "candidate_score_count": 96,
  "cache_entry_count": 5,
  "budget_ns": 250000000,
  "within_budget": true
}
```

- `stage="chunk_first_block"` / `"chunk_incremental_block"`：分块路径。
- `stage="whole_decode"`：整段路径。
- `observed_ns`：实际解码循环耗时。
- `estimated_ns`：由 step 数、候选打分次数和缓存规模计算。

## 失效报告

```json
{
  "reason": "source_tokens",
  "first_affected_step": 4,
  "affected_steps": [4, 5, 6, 7, 8, 9, 10],
  "reused_steps": [0, 1, 2, 3],
  "affected_cache_entries": ["sha256..."],
  "reused_cache_entries": ["sha256..."],
  "old_source_digest": "sha256...",
  "new_source_digest": "sha256...",
  "old_align_version": 1,
  "new_align_version": 1,
  "old_block_size": 4,
  "new_block_size": 4,
  "details": {
    "changed_source_indices": [5],
    "invalidated_source_dependencies": {"5": [4]},
    "alignment_changed": false,
    "config_changed": false,
    "reuse_entry_boundary_max": 4,
    "reused_payload_hashes": {"0": "sha256..."}
  },
  "set_check": {
    "disjoint": true,
    "covers_all_steps": true
  }
}
```

## 快照状态

```json
{
  "bundle_fingerprint": "sha256...",
  "snapshots": {
    "vocabulary": {
      "kind": "vocabulary",
      "version": 1,
      "checksum": "sha256...",
      "object_id": 1
    }
  }
}
```

## CLI

| 命令 | 输出 |
|---|---|
| `mtstream decode` | 分块结果、整段结果、一致性布尔值、分离计时、缓存数量 |
| `mtstream selfcheck --out FILE` | 六组自检及完整报告 |
| `mtstream invalidate --mode ...` | 失效步集合、缓存集合和 `set_check` |
| `mtstream generate` | 固定种子样本路径、case id |
| `mtstream pool-check` | 临时目录回读和本地进程池哈希 |
| `mtstream serve` | 启动 `127.0.0.1` 本地诊断台 |

