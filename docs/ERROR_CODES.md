# Error Contract

Errors are JSON on stderr:

```json
{
  "error": {
    "code": "E_DIGEST_MISMATCH",
    "details": {},
    "message": "..."
  },
  "exit_code": 4,
  "ok": false
}
```

| Code | Exit | Meaning |
| --- | ---: | --- |
| `E_ARGUMENT` | 2 | Invalid CLI combination |
| `E_PLAN_INVALID` | 2 | Plan schema, enum, range, or path is invalid |
| `E_RECORD_INVALID` | 2 | Record shape/type or non-negative integer violation |
| `E_NAN_OR_OVERFLOW` | 2 | NaN, infinity, out-of-range input, or fixed conversion overflow |
| `E_OVERFLOW` | 2 | Signed 64-bit aggregate accumulation overflow |
| `E_ENDPOINT_INVALID` | 2 | Endpoint is not a loopback endpoint |
| `E_PROTOCOL_INVALID` | 2 | Protocol is outside the closed enum |
| `E_DIRECTION_INVALID` | 2 | Direction is outside the closed enum |
| `E_RECORD_SEQ` | 2 | Sequence is not strictly increasing |
| `E_BATCH_EMPTY` | 2 | Empty batch |
| `E_BATCH_TOO_LARGE` | 2 | More than `max_batch_rows` |
| `E_SAMPLE_MISSING` | 2 | Plan, batch, or manifest file is absent/unreadable |
| `E_NO_DATA` | 3 | Verify called without committed groups |
| `E_SPILL_TAMPERED` | 3 | Missing/extra shard, byte digest, metadata, or chain-link failure |
| `E_STATE_CORRUPT` | 3 | Persisted vector/version or chain prefix is inconsistent |
| `E_VERIFY_MISMATCH` | 3 | First group/quantile divergence from full truth |
| `E_DIGEST_MISMATCH` | 4 | Same batch id has different content |
| `E_CLOCK_REGRESSION` | 4 | Clock rewind exceeds `max_clock_regression_ms` |
| `E_PLAN_MISMATCH` | 4 | Existing store belongs to another plan |
| `E_MEMORY_BUDGET` | 5 | Active slice cannot fit a group within the total budget |
| `E_SINGLE_GROUP_TOO_LARGE` | 5 | One group exceeds the total budget |
| `E_READ_BUFFER_DEFERRED` | 5 | Read cannot be materialized; reason is recorded |
| `E_LOCK_TIMEOUT` | 6 | State-directory lock timeout |
| `E_CONCURRENCY` | 6 | Optimistic group version or same-batch race lost |
| `E_SPILL_CONFLICT` | 6 | A spill target was already materialized |
| `E_DB` | 7 | SQLite open/commit failure |
| `E_INTERNAL` | 7 | Unexpected non-business failure |

Invalid input is fully parsed before an intent is written. Business/compute
failures abort the current intent and leave earlier committed batches intact.

