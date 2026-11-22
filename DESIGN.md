# Design Notes

## 1. Snapshot model

The manifest is an envelope:

```json
{
  "schema_version": 1,
  "checksum": "sha256(canonical(state))",
  "state": {
    "next_version": 7,
    "generation": 6,
    "stages": {
      "ingest": {"version": 1, "checksum": "...", "cache_key": "..."}
    }
  }
}
```

The committed stage set is always a prefix of:

```text
ingest -> normalize -> quality -> dedupe -> sample -> export
```

The set is inferred from stage names, never from JSON dictionary iteration order. This matters because canonical JSON sorts object keys.

Each snapshot contains:

- `stage`, `stage_index`, global `version`
- `status: committed`
- `cache_key`, `upstream_checksum`, `dependency_digest`
- `content_checksum = sha256(canonical(payload))`
- sorted `completed_sample_ids`
- the complete stage payload

Snapshots are immutable and always written as a whole file. A new version gets a new path. The manifest pointer is replaced atomically after the snapshot has been fsynced and renamed into place.

## 2. Commit protocol

1. Acquire `locks/commit.lock` without waiting.
2. Verify expected manifest head token (stale writer detection).
3. Verify the stage is the next stage in the committed prefix.
4. Verify the upstream checksum.
5. Reject non-contiguous/old requested versions with `E_SNAPSHOT_ROLLBACK`.
6. Write or verify the CAS object (an orphan object alone is not cache-hit eligible).
7. Write the complete snapshot using temp file + `os.replace`.
8. Mutate a copy of manifest state: add stage, increment global version and generation.
9. Write the manifest envelope using temp file + `os.replace`.
10. Publish the `cache_key -> content_checksum` index only after the manifest commit.

A crash before step 9 leaves an orphan snapshot/CAS object but cannot expose a half-committed stage or a cache-hit-eligible artifact. A crash after step 9 exposes a fully written snapshot; reconciliation can finish publishing the key from that committed snapshot.

All pipeline runs additionally hold `locks/pipeline.lock`. This prevents two runners from interleaving expensive work. A stale lock is reclaimed only when its recorded PID is no longer alive.

## 3. Cache keys

```text
cache_key = sha256({
  algorithm_version,
  stage,
  base_input,
  dependency_digest
})
```

- `ingest`: `base_input` is the byte hash of `samples.jsonl`; dependency includes controlled languages and validation fields.
- `normalize`: base is upstream content checksum; dependency is the full template library.
- `quality`: base is upstream checksum; dependency is the quality rule object, including weights.
- `dedupe`: base is upstream checksum; dependency is the dedupe rule object, including threshold/signature parameters.
- `sample`: base is upstream checksum; dependency is the complete quota table and seed.
- `export`: base is upstream checksum; dependency is export format configuration.

On resume/reconciliation, every committed stage is checked against its current expected key and upstream link. The first mismatch atomically truncates the committed suffix. Thus a stale downstream snapshot can never be used with a new upstream snapshot.

## 4. Recovery semantics

- Committed prefix: read and verify snapshots; do not execute their samples.
- Missing next stage: load its upstream payload and execute from item 0.
- Partial in-memory work from a killed stage is not persisted.
- Export files are content-addressed by whole-file checksum and atomically written. A recomputation writes the same bytes and path for the same deterministic content.
- The export stage commits only after re-reading the file and verifying its SHA-256.

The demo's forced-kill path pauses after a commit marker, then sends `SIGKILL` on POSIX or `TerminateProcess` on Windows. The graceful path sends `SIGTERM` on POSIX or writes `state/stop.request` on Windows; the worker stops only in the `after_stage` commit-boundary callback.

## 5. Edit propagation

Input edits are validated and atomically written first. The run then computes expected cache keys:

- sample output: invalidates from `ingest`
- template add/remove/change: invalidates from `normalize`
- cleaning weight: invalidates from `quality`
- quota change: invalidates from `sample`

Only the invalidated suffix is recomputed. Earlier snapshots retain their versions and are reported in `reused_snapshot_stages`.

Affected sample IDs are not inferred from the edit type alone. The engine diffs per-sample records between the old and new snapshots for every stage:

- record added/removed/changed: affected
- record byte-semantically unchanged: reused

This is why changing a template that a sample did not use does not mark that sample as affected.

## 6. Quality and dedupe audit

Quality records contain component scores, raw ratios and the final weighted score. The two score consumers are deterministic:

- tier selection: first configured `score >= min_score`
- dedupe winner: `(-quality_score, sample_id)`

Dedupe clusters contain:

- member IDs
- per-member scores
- representative ID
- complete threshold edge list

Each record also contains direct similar neighbors, the threshold in force, its signature, cluster ID, kept flag and representative. Therefore a reviewer can recompute edges and the representative without trusting stage-global summaries.

## 7. Sampling allocation

The declared source/tier target weights are converted to integer plans with the largest-remainder method when their sum differs from `total_target`. Each cell is ranked deterministically. If some cells cannot fill their plans but candidates remain elsewhere, unused global capacity is reallocated by global rank so total target is prioritized. Every goal reports:

- declared target
- planned target after scaling
- available candidates
- actual selected
- deficit
- explicit reason tags

## 8. Resource estimates

The resident-memory estimate is intentionally conservative and deterministic:

- ingest: `raw_file_bytes * 3 + sample_count * 2048`
- dedupe: `upstream_bytes * 3 + n * signature_size * 16 + pair_count * 48`
- other stages: `upstream_bytes * 4 + sample_count * 2048`

Estimates are reported per stage and checked before cache lookup or execution. Limits return `E_RESOURCE_LIMIT`; parallel over-request is explicitly degraded and tagged.

## 9. Cross-module self-check

`check` verifies:

- contiguous stage prefix and increasing versions
- snapshot/manifest checksum equality
- upstream checksum chain
- current dependency-aware cache keys
- CAS object hashes
- completed ID set equals the stage's expected complete input set
- dedupe threshold/neighbor/winner invariants and signature edge recomputation
- sampling goal counts against selected records
- export row checksum, header checksum, file checksum and ordering

Corrupt snapshots raise `E_SNAPSHOT_CORRUPT` with stage and version details.
