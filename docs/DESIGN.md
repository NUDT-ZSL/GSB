# Design

## State Model

Each group has an additive `StateVector`:

```text
count, bytes_sum, packets_sum, bytes_sq_sum, packets_sq_sum,
first_clock_ms, last_clock_ms, first_record_seq, last_record_seq
```

Mean byte and packet rates are derived by exact integer division at report
time. All additive values are checked against `INT64_MAX` before mutation.

`groups` stores the committed vector, group version, chain tail digest, and a
`spill_sum` vector. `spill_sum` is the merge of every committed shard in that
group's chain. The difference between `state` and `spill_sum` is the next
cold-spill delta. Exact sample values live in `samples`, ordered by
`(clock_ms, record_seq)`, so quantiles can be selected with bounded memory.

## Memory Budget

The plan's total budget is `memory_budget_bytes`. A deterministic fraction
(`read_buffer_fraction`, default 0.25) is reserved for spill-read buffers and
the remainder is the active-table slice.

The active-group estimate is:

```text
640 + 24 * dimension_count + UTF-8 length(canonical group key)
```

Inserting or loading a group evicts the least-recently-used groups, with
`group_id` as the stable tie-break, until the estimated active bytes fit. A
single group larger than the total budget raises `E_SINGLE_GROUP_TOO_LARGE`; a
group that fits the total but not the active slice raises `E_MEMORY_BUDGET`.
Neither path loops.

If a spill shard plus 256 bytes of read overhead exceeds the read-buffer
reserve, the engine records `READ_BUFFER_TOO_SMALL`, delays that readback, and
uses the exact committed SQLite state as the recovery base. The deferred event
and reason are committed in the next batch stats. It is not an approximation;
the next cold spill reconciles the chain.

Active state is cleared at batch boundaries. This confines active memory to one
batch and removes process-residency as an input to later state decisions.

## Spill Shard

Each shard is canonical JSON containing:

```text
schema_version, plan_id, batch_id, spill_seq, group_id, group_key,
group_key_json, dimensions, state_version, previous_sha256, delta
```

The SHA-256 is over the exact file bytes. A shard is written to a unique temp
file in the spill directory, fsynced, renamed with `os.replace`, and the
directory is fsynced on POSIX. The SQLite manifest row is inserted only in the
batch's final transaction. An interrupted pre-commit run therefore leaves
only unreferenced files, which `resume` deletes.

Readback verifies the file digest, sequence, group identity, plan, dimensions,
and `previous_sha256` link. Shards are merged in spill sequence order by
additive fields and min/max time fields. `verify` additionally requires the
merged chain to equal the committed group vector and reports the first
divergent group with both states and the chain.

## Batch Protocol

1. Parse the plan, the complete batch, the closed enums, endpoint syntax,
   numeric bounds, and within-batch sequence. No store is opened on parse
   failure.
2. Read SQLite watermarks and reject sequence rewind or excessive clock
   regression before writing an intent.
3. Insert a `processing` intent with the canonical content digest.
4. Aggregate up to `max_batch_rows` using the budgeted LRU table and spill
   deltas.
5. In one `BEGIN IMMEDIATE` transaction:
   - update group vectors with optimistic version checks;
   - insert unique `(batch_id, group_id)` version records;
   - insert sample rows;
   - insert the spill manifest;
   - advance the logical watermark and consumed sequence;
   - append the batch digest-chain hash;
   - mark the batch committed and persist its result JSON.
6. A duplicate batch with the same digest returns the stored result marked
   `idempotent_replay`; a different digest returns `E_DIGEST_MISMATCH`.

The watermark is `max(previous_watermark, max(batch clock))`. Inputs may
arrive slightly out of order within `max_clock_regression_ms`, but the
watermark never decreases.

## Recovery

On startup, `resume`:

1. acquires the state-directory lock;
2. removes spill/temp files absent from the committed manifest;
3. loads every `processing` intent in stable batch order;
4. verifies the batch digest against the intent;
5. discards the partial in-memory attempt and redoes that batch from its
   committed SQLite/chain base;
6. optionally continues all later batches from a supplied manifest.

Group mutations are never committed before the batch transaction, so there is
no partial group version to compensate. The hash-killed recovery test writes a
real shard, leaves one processing intent, resumes, and produces byte-identical
`status` and `verify` output to the uninterrupted run.

## Quantiles and Fixed Point

For `n` values ordered by `(value, clock_ms, record_seq)` and quantile `q`,
type-7 interpolation uses:

```text
h = (n - 1) * q
lo = floor(h)
raw = values[lo] + (h - lo) * (values[min(lo + 1, n - 1)] - values[lo])
```

The float path emits `raw` as a JSON number. The fixed path multiplies the
exact decimal raw value by `fixed_scale` and rounds half up. Each fixed result
records `scale`, `rounding`, `raw_value`, `anchor_raw_values`,
`interpolation`, and `order_by`.

## SQLite Tables

- `batches`: intent/commit status, canonical digest, input path, clock and
  sequence boundaries, result JSON.
- `groups`: current vector, version, spill baseline, spill chain, tail digest.
- `batch_groups`: one optimistic-versioned delta per batch/group.
- `samples`: exact ordered values for quantiles.
- `spill_files`: shard sequence, group, version, path, byte digest, chain link.
- `meta`: plan fingerprint, watermark, consumed sequence, digest chain,
  counters, next spill sequence, and persisted memory maxima.

