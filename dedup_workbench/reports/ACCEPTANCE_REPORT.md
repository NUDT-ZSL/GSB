# Acceptance Verification Report

- Generated at (UTC): `2026-11-25T19:26:09.031822+00:00`
- Python: `3.10.12`
- NumPy: `1.26.4`
- pytest: `8.2.0`
- Overall status: `PASS`

## Deterministic Sample

- Records: `25`
- Shards: `4`
- Signature blocks: `192`
- Candidate pairs: `27`
- Qualifying near-duplicate pairs: `27`
- Removed docs: `15`
- Removed IDs: `doc-0001, doc-0003, doc-0004, doc-0006, doc-0007, doc-0008, doc-0010, doc-0011, doc-0013, doc-0014, doc-0015, doc-0017, doc-0018, doc-0020, doc-0021`

## Query Consistency

- Checked docs: `25`
- All query records byte-identical to batch export: `True`
- Latency budget probe: `E_LATENCY_BUDGET`

## Incremental Propagation

| Scenario | Recomputed docs | Reused docs | Comparison recompute/reuse | Recomputed nodes |
|---|---:|---:|---:|---|
| `identical_rerun` | 0 | 25 | 0/27 | `none` |
| `threshold_0.95` | 21 | 4 | 0/27 | `aggregate_export, decision_component` |
| `validation_digest` | 21 | 4 | 27/0 | `aggregate_export, decision_component, pair_comparison` |
| `window_1` | 21 | 4 | 0/15 | `aggregate_export, candidate_set, decision_component` |
| `chunk_length_24` | 25 | 0 | 46/0 | `aggregate_export, candidate_set, decision_component, pair_comparison, signature` |
| `content_revision` | 1 | 24 | 0/27 | `aggregate_export, candidate_set, ingest_validation, signature` |

## Recovery

- Forced termination after committed batches: observed=`True`
- Committed after crash: `[0, 1]`
- Skipped on resume: `[0, 1]`
- Executed on resume: `[2, 3, 4]`

## pytest

- Status: `PASS`
- Summary: `..................................                                       [100%]`

```text
..................................                                       [100%]
```
