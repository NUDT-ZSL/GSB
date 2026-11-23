# Verification Record

Verification date: 2026-11-24. All commands ran locally without Docker or
external network targets.

## Environment

- OS: Linux (Ubuntu 22.04 userland)
- Python: 3.12.13
- Flask: 3.1.3
- NumPy: 2.5.5
- SQLite: 3.53.1
- Python requirement: `>=3.11`

Windows-specific command syntax and the `msvcrt` lock fallback are implemented,
but this run was executed on Linux. The Windows path was not executed here.

## Automated Tests

Command:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Result:

```text
Ran 15 tests in 8.951s
OK
```

Covered assertions include:

- Invalid configuration returns a business code and creates no SQLite file.
- Duplicate names, non-loopback listeners, oversize payloads, invalid loss,
  invalid jitter, and invalid policy values have distinct error codes.
- Repeated fragmentation produces byte-identical headers and fragment digests.
- Reassembly reports duplicate, missing, and digest failures separately.
- Full alpha payload `96` advances as `40 + 40 + 16` bytes over two rounds.
- Logical clock observations are exactly `50 ms` after round 1 and `75 ms`
  after round 2 for the sample policy.
- Loss `1.0` exhausts two retransmissions and ends
  `E_RETRANSMIT_EXHAUSTED`, generation `2`, trigger `PROFILE_LOSS`.
- Missing fragment bytes are replayed unchanged after a profile-driven MTU
  reduction; old fragments stay at profile version 1 and new fragments use
  version 2.
- A hard `SIGKILL` test exits with code `137`, produces empty stdout, leaves one
  committed and one in-flight fragment, and resumes only the unconfirmed
  fragment.
- Two tunnel processes complete concurrently; a second process for the same
  locked tunnel returns `E_TUNNEL_LOCKED`.
- Duplicate confirmation inserts only one confirmation row.
- Active abort ends `ABORTED_ACTIVE` with `E_ACTIVE_ABORT`.
- Profile rollback returns `E_PROFILE_ROLLBACK`.
- Updating alpha does not alter beta fragment bytes.
- `verify` detects deliberately tampered offsets and allocation frontiers.
- Flask load, forward, status, verify, and rollback endpoints work.

## Normal Forwarding

Echo:

```bash
python -m tunnel_workbench echo --config samples/config.json
```

Forward:

```bash
python -m tunnel_workbench load \
  --config samples/config.json --db artifacts/verification/workbench.db
python -m tunnel_workbench forward \
  --config samples/config.json --db artifacts/verification/workbench.db \
  --tunnel alpha --payload-index 1 --rounds 1
python -m tunnel_workbench resume \
  --config samples/config.json --db artifacts/verification/workbench.db \
  --tunnel alpha --rounds 1
python -m tunnel_workbench forward \
  --config samples/config.json --db artifacts/verification/workbench.db \
  --tunnel beta --payload-index 1 --rounds 1
python -m tunnel_workbench status --db artifacts/verification/workbench.db
python -m tunnel_workbench verify \
  --config samples/config.json --db artifacts/verification/workbench.db
```

Observed:

```text
alpha round 1: PARTIAL, NOT_STARTED_BUDGET, watermark=2, clock=50
alpha round 2: COMPLETED, watermark=3, clock=75
beta round 1:  COMPLETED over TCP, watermark=2
verify:        ok=true, anomalies=[]
```

The generated JSON is in:

- `artifacts/verification/alpha-round1.json`
- `artifacts/verification/alpha-round2.json`
- `artifacts/verification/beta-round1.json`
- `artifacts/verification/status.json`
- `artifacts/verification/verify.json`

## Adaptive MTU and Profile Version

Commands:

```bash
python -m tunnel_workbench load \
  --config samples/config.json --db artifacts/verification/adaptive.db
python -m tunnel_workbench forward \
  --config samples/config.json --db artifacts/verification/adaptive.db \
  --tunnel alpha --payload-index 1 --rounds 1
python -m tunnel_workbench profile-replace \
  --db artifacts/verification/adaptive.db --tunnel alpha \
  --profile samples/profile_alpha_v2.json
python -m tunnel_workbench resume \
  --config samples/config.json --db artifacts/verification/adaptive.db \
  --tunnel alpha --rounds 4
python -m tunnel_workbench verify \
  --config samples/config.json --db artifacts/verification/adaptive.db
```

Observed:

```text
Before update: PARTIAL, current_mtu=220, watermark=2, profile version 1
After update:  COMPLETED, current_mtu=107, watermark=3
Round profiles used: [1, 2]
Verify: ok=true, anomalies=[]
```

The existing 40-byte fragments were not resized. The remaining 16-byte tail was
emitted under profile version 2 after the MTU reduction.

Artifacts:

- `artifacts/verification/adaptive-before.json`
- `artifacts/verification/adaptive-profile-v2.json`
- `artifacts/verification/adaptive-after.json`
- `artifacts/verification/adaptive-verify.json`

## Retransmission Exhaustion

With `samples/config_lossy.json` and loss `1.0`:

```text
CLI exit: 3
status: FAILED
outcome: FAILED_AFTER_SEND
business_code: E_RETRANSMIT_EXHAUSTED
trigger_source: PROFILE_LOSS
retransmission_generation: 2
retransmission_count: 2
verify: ok=true, anomalies=[], business_links includes the failure code
```

Artifacts:

- `artifacts/verification/lossy-forward.json`
- `artifacts/verification/lossy-verify.json`

## Exit Codes

- `0`: successful command, or completed/partial run without failure
- `2`: business or validation error
- `3`: forwarding run ended `FAILED_AFTER_SEND`
- `4`: `verify` found recomputation anomalies
- `137`: forced-kill test process; no exit report was generated

## Real Flask Server Smoke Test

`serve --host 127.0.0.1 --port 18765` was started and queried over a real
loopback HTTP socket:

```text
/api/health  200, ok=true
/api/status  200, ok=true, 2 tunnels
/            200, HTML dashboard contains "Tunnel Workbench"
```

