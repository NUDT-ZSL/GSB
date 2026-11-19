# Actual Validation Record

Validation date: 2026-11-20  
Workspace: `/home/zsl/feedback/uid2d1b8ac8-default-119`

## Environment

```text
Python 3.11.15
numpy 1.26.4
```

Python 3.11 was installed locally with `uv`, and the project uses only the standard library plus the pinned NumPy runtime dependency. No container, GPU, network service, or external data source was used.

## Unit and Integration Tests

Command:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

Result:

```text
Ran 17 tests in 5.810s
OK
```

Covered cases:

- deterministic byte-identical framing features and truth alignment;
- invalid sample rate and invalid frame shift with field-level errors;
- truth frame-count mismatch;
- truth speaker outside the registered set with dialog/frame details;
- exact speaker-similarity tie resolved to the lowest `speaker_id`;
- deterministic regression/aggregation and independent weighted recomputation;
- `E_INCONSISTENT` for mismatched stage dialog sets;
- non-blocking `E_STAGE_BUSY`;
- `E_SNAPSHOT_ROLLBACK` for overwrite attempts;
- snapshot and embedding-cache checksum corruption with stage/version details;
- resource limit with `degradation_category`;
- registry version upgrade, whole-prefix replacement, version continuity, and unchanged-state reuse;
- crash after a committed stage, restart, and byte-identical report comparison;
- crash during an uncommitted stage, discard, and restart from the previous committed snapshot;
- full interactive demonstration with report files and cross-module self-check.

## Acceptance Run

Command:

```bash
PYTHONPATH=src .venv/bin/python scripts/acceptance.py --out artifacts/validation
```

Generated local sample:

```text
dialogs: 4
speakers: 3
sample_rate: 16000
frame_ms: 20
```

Full uninterrupted run:

```text
report_hash:
0078ad2c5b9b7b4f78e43fb59f4583b805aee204c06661fbdd8779a2d9473b4c

snapshot versions:
framing=1, segmentation=2, regression=3, aggregation=4

self-check checksum:
41c41ea7f2433b436510e0698f28d444f668509885c1a39e7de83aa9dfe9aa4f
```

Crash/restart demonstration:

```text
forced child exit code: 75
stop after committed stage: segmentation
reused stages: framing, segmentation
computed stages after restart: regression, aggregation
full report hash:       0078ad2c5b9b7b4f78e43fb59f4583b805aee204c06661fbdd8779a2d9473b4c
recovered report hash:  0078ad2c5b9b7b4f78e43fb59f4583b805aee204c06661fbdd8779a2d9473b4c
report_bytes_identical: true
```

Registry sample append:

```text
registry_version: 1 -> 2
epoch: 1 -> 2
previous generation: 4
replacement snapshot versions: 5, 6, 7, 8
archived old snapshot prefixes: 1
recomputed stages: framing, segmentation, regression, aggregation
registry-v2 report hash:
fcfbe5550d9415f77cd37634092b5740f979dbd38f56ecae0a341d7a25856dac
registry-v2 self-check checksum:
48e08bcba1f92894f24320c7e79ac2dbdbf5a42c2a9962fb67fbb36d65c69f4b
```

Artifacts:

- `artifacts/validation/summary.json`
- `artifacts/validation/full_report.json`
- `artifacts/validation/registry_v2_report.json`
- `artifacts/validation/full_state/`
- `artifacts/validation/demo_workspace/demo/full_report.json`
- `artifacts/validation/demo_workspace/demo/recovered_report.json`

## Feature Export

Command:

```bash
.venv/bin/voice-emotion --workdir artifacts/validation --json features \
  --dataset artifacts/validation/dataset \
  --state artifacts/validation/full_state \
  --dialog-id dlg_0001 \
  --output artifacts/validation/dlg_0001.features.jsonl
```

Result:

```text
num_frames: 320
features_checksum: 7f748da1c06ef43bbfd994a3b64c5d56cc488064934380e7feebfa632add2db4
output lines: 320
```

Each line contains `frame_index`, `energy`, and `zcr` in canonical JSON form.

## Interactive Application

Piped REPL validation:

```text
status
run
report
selfcheck
quit
```

Result: exit code 0. The dashboard showed:

```text
after run:
current stage = complete
last committed stage = aggregation
generation = 4
framing=1/2 dialogs, segmentation=2/2, regression=3/2, aggregation=4/2
report hash = 74251cb9f99b9f220f2284cf327578bbff15849c87f3e60377e58c0c178a66a8
self-check passed
```

