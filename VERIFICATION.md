# Verification Record

Environment:

- Date: 2026-11-23
- Python: 3.10.12
- NumPy: 1.26.4
- OS used for execution: Linux local workspace
- Network/services: none
- Container: none

## Automated tests

Command:

```bash
python -m unittest discover -v
```

Result:

```text
Ran 15 tests in 8.163s
OK
```

Covered cases:

1. Full six-stage run and byte-identical output across two fresh workspaces.
2. Complete no-op reuse with no committed sample recomputation.
3. Dedupe winner, neighbor, threshold and cluster invariants.
4. Quota manifest consistency and 30/30 target completion.
5. Export header/content/file checksums and declared ordering.
6. Duplicate ID, invalid language, template placeholder, weight, threshold and quota error codes/fields.
7. Sample-count and memory `E_RESOURCE_LIMIT`; parallel request degradation.
8. `E_SNAPSHOT_ROLLBACK`, `E_STAGE_BUSY`, and corrupted snapshot `E_SNAPSHOT_CORRUPT` with stage/version.
9. Quota edit: reuse ingest/normalize/quality/dedupe, recompute sample/export.
10. Template removal: normalize affected exactly the 5 samples that used that template; 55 reused.
11. Weight edit: reuse ingest/normalize, recompute quality/dedupe/sample/export.
12. Output edit: ingest/normalize affected exactly the edited sample; downstream diff follows actual selection changes.
13. Partial stage payload is rejected with `E_STATE_INCONSISTENT`.
14. Real subprocess `SIGKILL` after quality commit, graceful boundary stop after dedupe commit, and mid-quality-stage `os._exit(137)` with no quality commit; all resumed exports match uninterrupted full runs byte-for-byte.
15. Oversized quota target produces explicit global and per-goal underfill reasons while still committing a complete prefix.

## Current local full run

Generated sample file SHA-256:

```text
f1f470b1a70721eefe06fc6f55cedf7d066d9dc6a968e6560158e02363740c78
```

Committed versions:

```text
ingest=1 normalize=2 quality=3 dedupe=4 sample=5 export=6
```

Stage output counts:

```text
ingest=60 normalize=60 quality=60 dedupe=60 sample=30 export=30
```

Dedupe retained 51 representatives and removed 9 samples. Quality-tier/source quota goals were all met; total target and actual were both 30, underfill 0.

Export content checksum:

```text
f14475a2b0cfbf1f9f2ace8a4d2da697456f4425858cdf2679909f6acbfa83ae
```

Export file bytes SHA-256:

```text
6a6d739c8414edb66fa2462344c30d1ea2542bb77c475a50234d7953b2b9bf0d
```

Self-check:

```text
ok=true complete=true failed_checks=0 checks=58 committed=all six stages
```

## Forced termination after committed stage

Command:

```bash
python -m sft_pipeline demo --kill-stage quality --mode kill --json
```

Observed:

- external termination returned `-9`
- committed prefix at kill: `ingest`, `normalize`, `quality` at versions 1/2/3
- recovery reused those three snapshots
- recovery recomputed `dedupe`, `sample`, `export` at versions 4/5/6
- full export file SHA-256 == recovery export file SHA-256
- byte_identical = `true`
- version_sequences_identical = `true`

## Graceful signal after committed stage

Command:

```bash
python -m sft_pipeline demo --kill-stage dedupe --mode term --json
```

Observed:

- worker received `SIGTERM` while paused at the post-commit boundary
- worker printed `GRACEFUL_STOP:dedupe` and exited 0
- `state/graceful_stop.json` recorded stage `dedupe`, version 4
- recovery reused ingest/normalize/quality/dedupe
- recovery recomputed sample/export
- final exports were byte-identical to the uninterrupted run

## Mid-stage hard kill

Automated subprocess test used:

```text
worker --fault-stage quality --fault-after-items 5
```

Observed:

- worker exited with code 137 during quality processing
- committed prefix remained exactly `ingest`, `normalize`
- no quality snapshot was committed
- no quality cache artifact was published
- restart rebuilt quality from its stage start, then completed dedupe/sample/export
- resumed export was byte-identical to the uninterrupted run

## Edit propagation observations

- Quota edit reused `ingest/normalize/quality/dedupe`; only `sample/export` were recomputed. New versions were 7 and 8.
- Template `T-GEN-B` removal reused `ingest`; only the 5 samples that had selected that template were marked affected in normalize; 55 were reported reused.
- Weight edit reused `ingest/normalize`; quality and all dependent suffix stages were recomputed.
- Output edit restarted at ingest, marked only the edited sample affected in ingest/normalize, and reported downstream affected IDs based on actual record changes rather than blindly marking every sample.

## Error-code observations

The test suite confirmed these exact business codes and fields:

```text
E_DUP_SAMPLE        sample_id
E_LANG_INVALID      lang
E_TEMPLATE_INVALID  templates[0].pattern
E_CONFIG_INVALID    quality.weights.length
E_CONFIG_INVALID    dedupe.threshold
E_CONFIG_INVALID    quotas.targets[0].target
E_RESOURCE_LIMIT    limits.max_samples_per_stage
E_RESOURCE_LIMIT    limits.max_resident_memory_mb
E_STAGE_BUSY        concurrent commit lock
E_SNAPSHOT_ROLLBACK old version replacement
E_SNAPSHOT_CORRUPT  quality snapshot checksum, stage=quality, version included
```
