# Tunnel Workbench

`tunnel-workbench` is a local, deterministic tunnel/VPN forwarding workbench. It
fragments inner payloads, sends every fragment to loopback UDP/TCP echo targets,
reassembles by sequence number, and persists forwarding watermarks, in-flight
intervals, retransmission state, profile versions, and the logical clock in
SQLite.

The implementation is intentionally restricted to loopback targets. Link loss
and delay are deterministic functions of the run ID, profile version, fragment
sequence, and retransmission generation; no external network is used.

## Requirements

- Python 3.11 or newer
- `flask` and `numpy`
- SQLite through the Python standard library `sqlite3`

The verified environment used Python 3.12.13, Flask 3.1.3, NumPy 2.5.5, and
SQLite 3.53.1.

## Quick Start

POSIX shell:

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install flask numpy
python -m pip install -e .

# Terminal 1: UDP :19001 and TCP :19002
python -m tunnel_workbench echo --config samples/config.json

# Terminal 2
python -m tunnel_workbench load \
  --config samples/config.json --db artifacts/verification/workbench.db
python -m tunnel_workbench forward \
  --config samples/config.json --db artifacts/verification/workbench.db \
  --tunnel alpha --payload-index 1 --rounds 1
python -m tunnel_workbench resume \
  --config samples/config.json --db artifacts/verification/workbench.db \
  --tunnel alpha --rounds 1
python -m tunnel_workbench status --db artifacts/verification/workbench.db
python -m tunnel_workbench verify \
  --config samples/config.json --db artifacts/verification/workbench.db
```

Windows command prompt:

```bat
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install flask numpy
.venv\Scripts\python -m pip install -e .
mkdir artifacts\verification artifacts\tmp artifacts\runtime

start "workbench echo" .venv\Scripts\python -m tunnel_workbench echo --config samples\config.json
.venv\Scripts\python -m tunnel_workbench load --config samples\config.json --db artifacts\verification\workbench.db
```

`TunnelFileLock` uses `fcntl` on POSIX and `msvcrt.locking` on Windows. The
database and lock directory must be writable. Fragments are staged
transactionally in SQLite, so no payload is written to temp files; the optional
`artifacts\tmp` and `artifacts\runtime` directories are reserved for local
operation and diagnostics.

## Sample Input

`samples/config.json` defines:

- Tunnels `alpha` (UDP) and `beta` (TCP), including loopback listeners, outer
  MTU, payload length distributions, link weights, and encryption-placeholder
  overhead.
- Initial link-profile versions with RTT, loss, bandwidth, and jitter.
- Fragment threshold, per-round concurrency, logical-time budget,
  retransmission budget, tolerance, and logical-clock step.

`samples/profile_alpha_v2.json` increases alpha RTT and loss for the adaptive
MTU scenario. `samples/config_lossy.json` deliberately drops every attempt to
exercise retransmission exhaustion.

Useful commands:

```bash
# Advance several rounds in one process (0 means until terminal or no progress).
python -m tunnel_workbench forward ... --rounds 0

# Atomically replace one tunnel profile; lower versions are rejected.
python -m tunnel_workbench profile-replace \
  --db artifacts/verification/adaptive.db \
  --tunnel alpha --profile samples/profile_alpha_v2.json

# Request an active abort. An engine currently waiting on a send observes this.
python -m tunnel_workbench abort --db DB --tunnel alpha
```

## HTTP API

Start only on a loopback address:

```bash
python -m tunnel_workbench serve \
  --config samples/config.json --db artifacts/verification/workbench.db \
  --host 127.0.0.1 --port 8765
```

Endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Same-origin HTML dashboard |
| `GET` | `/api/health` | Liveness |
| `POST` | `/api/load` | Load `{"path": ...}` or `{"config": {...}}` |
| `GET` | `/api/status` | Stable-sorted per-tunnel/run status |
| `POST` | `/api/forward` | `tunnel`, `payload_index`, `rounds`, optional `transport` |
| `POST` | `/api/resume` | Resume the latest non-terminal run |
| `POST` | `/api/abort` | Active abort by `run_id` or `tunnel` |
| `GET` | `/api/verify` | Recompute persisted state and return anomalies |
| `GET` | `/api/profiles/<tunnel>` | Version history and current pointer |
| `POST` | `/api/profiles/<tunnel>` | Whole-profile replacement |
| `GET` | `/api/runs/<run_id>` | One persisted run |

Errors use:

```json
{
  "ok": false,
  "error": {
    "code": "E_PROFILE_ROLLBACK",
    "message": "profile replacement must advance the version",
    "details": {}
  }
}
```

## Wire Format

Each fragment has an 80-byte deterministic header:

| Field | Bytes |
|---|---:|
| Magic `TWB1`, protocol version, flags, header length | 8 |
| Tunnel token | 8 |
| Stream token | 8 |
| Sequence number | 4 |
| Fragment payload length | 4 |
| Total payload length | 4 |
| Encryption-placeholder overhead | 4 |
| Bound profile version | 4 |
| Reserved | 4 |
| SHA-256 of the complete inner payload | 32 |

The last fragment carries `FLAG_END`. The stream token includes the run ID, so
different runs do not share reassembly identity even when their payloads match.
`fragment_digest = SHA-256(header || seal || payload)` and
`header_digest = SHA-256(header)`.

Chunk calculation:

```text
capacity = current_outer_mtu - 80 - encapsulation_overhead
chunk    = min(policy.fragment_threshold, capacity)
```

Validation rejects an inner payload larger than the tunnel's initial capacity.
After an MTU reduction, only unsent payload is re-fragmented. Already allocated
unconfirmed fragments are retransmitted from their persisted wire bytes without
resizing.

Strict reassembly distinguishes:

- `E_DUPLICATE_FRAGMENT`
- `E_MISSING_FRAGMENT`
- `E_DIGEST_MISMATCH`
- `E_SEQUENCE_AFTER_END`
- `E_FRAGMENT_CONFLICT`

The echo service treats a byte-identical retransmission as an idempotent ACK.
A completed stream is reset when a new `seq=0` arrives, allowing deterministic
runs to be replayed against a long-lived echo process.

## Logical Clock and Outcomes

The persisted logical clock starts at zero. These values are computed only from
the injected clock, policy, and profile:

- Send timestamps
- Aggregation-window boundary (`send_clock + rtt + jitter`)
- Logical ACK timestamp (`send_clock + rtt`)
- Timeout watermark (`send_clock + rtt + jitter`)
- Retransmission backoff (`rtt * 2^generation`)
- Round deadline and start/deferral decisions

Real monotonic time is used only to wait for a socket reply and to poll for an
active abort. A dropped simulated link performs a real wait for the same
window. No decision reads wall-clock date/time.

Round outcomes are deliberately distinct:

| Outcome | Meaning |
|---|---|
| `NOT_STARTED_BUDGET` | Remaining work was not started in this round; run is resumable |
| `FAILED_AFTER_SEND` | A send occurred but the attempt ultimately failed |
| `ABORTED_ACTIVE` | An abort was observed while waiting or before a send |
| `COMPLETED` | All fragments were committed and receiver digest matched |

Retransmission exhaustion produces `E_RETRANSMIT_EXHAUSTED` with
`trigger_source` (`PROFILE_LOSS` or `ACK_TIMEOUT`) and
`retransmission_generation`.

## Persistence and Recovery

SQLite runs in WAL mode with `synchronous=FULL`, foreign keys, a busy timeout,
and explicit `BEGIN IMMEDIATE` write transactions.

1. A new or retried fragment is committed as `INFLIGHT` before dispatch.
2. An ACK inserts a unique `(run_id, seq)` confirmation and changes the fragment
   to `COMMITTED` in one transaction.
3. `committed_watermark` is recomputed as the contiguous prefix of committed
   sequences.
4. A process killed after intent but before ACK resumes by replaying only
   `INFLIGHT` rows. A process killed after ACK but before confirmation gets an
   idempotent duplicate ACK from the echo.
5. Forced termination emits no exit report. CLI output is produced only after a
   completed engine call.

Per-tunnel file locks prevent two processes from over-sending the same tunnel.
Different tunnels can use separate locks and SQLite write serialization
concurrently. Profile rows are append-only; the pointer to the current version
is replaced atomically and cannot move backwards.

## Error Codes

Validation:

- `E_NAME_DUPLICATE`, `E_NAME_INVALID`
- `E_LISTEN_ADDR_INVALID`, `E_LISTEN_NOT_LOOPBACK`
- `E_MTU_INVALID`, `E_OVERHEAD_INVALID`, `E_PAYLOAD_TOO_LARGE`
- `E_PAYLOAD_LENGTH_INVALID`
- `E_RTT_INVALID`, `E_LOSS_RATE_INVALID`, `E_BANDWIDTH_INVALID`,
  `E_JITTER_INVALID`
- `E_CONCURRENCY_INVALID`, `E_BUDGET_INVALID`
- `E_RETRANSMIT_BUDGET_INVALID`, `E_CLOCK_STEP_INVALID`,
  `E_FRAGMENT_THRESHOLD_INVALID`
- `E_PROFILE_ROLLBACK`, `E_PROFILE_VERSION_CONFLICT`

Runtime and persistence:

- `E_TUNNEL_LOCKED`, `E_RUN_TERMINAL`, `E_NO_RESUMABLE_RUN`
- `E_MTU_EXHAUSTED`, `E_RETRANSMIT_EXHAUSTED`
- `E_ACTIVE_ABORT`, `E_ACK_TIMEOUT`, `E_PROFILE_LOSS`
- `E_ACK_DIGEST_MISMATCH`, `E_STREAM_CONFLICT`

`verify` reports recomputation anomalies such as `E_VERIFY_WATERMARK`,
`E_VERIFY_OFFSET`, `E_VERIFY_CLOCK`, `E_VERIFY_ADAPTIVE_MTU`, and
`E_VERIFY_REASSEMBLY`, along with expected/actual diagnostic fields.

## Tests and Verification

```bash
python -m unittest discover -s tests -v
```

The suite covers 15 scenarios: validation without persistent writes, all
required reassembly errors, deterministic headers, round progression, logical
time, deterministic retransmission exhaustion, adaptive MTU, profile rollback,
unaffected-tunnel stability, hard-kill recovery, duplicate confirmation,
parallel tunnels, same-tunnel lock exclusion, active abort, verify tampering,
and Flask endpoints.

Actual results are recorded in
[`docs/VERIFICATION.md`](docs/VERIFICATION.md) and serialized in
`artifacts/verification/`.
