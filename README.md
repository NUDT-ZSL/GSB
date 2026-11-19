# lbdiag: local load-balancer health-check diagnostics

`lbdiag` is a Python 3.10+ standard-library-only workbench for deterministic
health-check, health-state, effective-weight, and precision-error diagnosis.
It drives real HTTP requests against a local loopback service, writes all
diagnostic state to SQLite, and can resume after a process is killed.

The repository includes a fixed sample:

- `samples/diagnostic_sample.json`: seven instances, plan, diagnostics, and
  loopback test-service routes.
- `lbdiag/service.py`: the local deterministic HTTP service.
- `artifacts/validation_report.json`: the actual recorded validation result.

No third-party package, Docker, container, or external network target is used.

## Quick start

Development and validation were performed with Python 3.10.12.

### Windows PowerShell

Terminal 1:

```powershell
py -3 --version
py -3 -m lbdiag serve --sample samples/diagnostic_sample.json
```

Terminal 2:

```powershell
py -3 -m lbdiag run --sample samples/diagnostic_sample.json --db .lbdiag\healthcheck.db --log-dir .lbdiag\logs --pretty
py -3 -m lbdiag status --sample samples/diagnostic_sample.json --db .lbdiag\healthcheck.db --log-dir .lbdiag\logs --pretty
py -3 -m lbdiag explain --sample samples/diagnostic_sample.json --db .lbdiag\healthcheck.db --log-dir .lbdiag\logs --instance beta --window 0 --pretty
py -3 -m lbdiag verify --sample samples/diagnostic_sample.json --db .lbdiag\healthcheck.db --log-dir .lbdiag\logs --pretty
```

### Linux or macOS

```bash
python3 -m lbdiag serve --sample samples/diagnostic_sample.json
# In a second shell:
python3 -m lbdiag run --sample samples/diagnostic_sample.json --db .lbdiag/healthcheck.db --log-dir .lbdiag/logs
python3 -m lbdiag resume --sample samples/diagnostic_sample.json --db .lbdiag/healthcheck.db --log-dir .lbdiag/logs
```

Without `--pretty`, `status` and `explain` emit compact JSON with sorted keys,
so byte-for-byte comparison is straightforward.

## Commands

| Command | Behavior |
| --- | --- |
| `run` | Validate the sample, probe missing checks, and commit complete windows. Running again is idempotent. |
| `resume` | Same recovery engine as `run`, intended for pending checks/windows after termination. |
| `status` | Read one SQLite snapshot and print committed states, windows, pending progress, and sensitive pairs. |
| `explain` | Print persisted pair diagnostics and error-source chains. Optional filters: `--instance`, `--window`. |
| `verify` | Check SQLite integrity, hashes, window boundaries, applied check watermarks, sensitivity completeness, and replay each persisted window/state transition. Pending complete windows are reported but remain recoverable. |
| `serve` | Run the local loopback HTTP test service. This is the only command that uses real sleeps for slow-response routes. |

`run` and `resume` accept `--instance alpha` (repeatable or comma-separated) to
process a subset of instances. Multiple processes may process different
instances against the same database.

## Fixed sample

The sample's logical clock starts at `1000000` ms, advances in `250` ms
injectable steps, uses a `1000` ms check interval, a `200` ms real timeout, and
a `0.01` jitter ratio. It runs 16 checks per instance. An 8-check sliding window
closes first at check 8, then every 4 checks.

The instances exercise these paths:

| Instance | Route | Expected observation |
| --- | --- | --- |
| `alpha` | `/health/alpha` | Constant success |
| `beta` | `/health/beta` | Three `200` responses followed by one `503`, exposing a `0.02` score difference from alpha |
| `gamma` | `/health/gamma` | Status-code mismatch |
| `delta` | `/health/slow` | Server sleeps 800 ms; the 200 ms real budget actively aborts and classifies timeout |
| `epsilon` | port `18766` | Connection refused |
| `zeta` | `/health/malformed` | Non-HTTP response |
| `eta` | `/health/body-mismatch` | HTTP 200 but response-body SHA-256 differs |

`alpha` and `beta` have equal configured weights. Their window scores are
`1.0` and `0.98`, which is inside the `0.03` tolerance. The final effective
weight ordering margin is `0.2`, inside the computed propagation bound
`0.600022`, so all three alpha/beta windows are precision-sensitive.

## Deterministic scheduling

`lbdiag` never reads the wall clock for scheduling or persisted logical time.
The only real-time source is `time.monotonic()` for enforcing the socket
timeout and measuring whether the real request exceeded its budget.

For instance `i` and check index `k`:

```text
scheduled_at_ms =
    logical_clock_start_ms
    + k * interval_ms
    + deterministic_signed_jitter_ms
```

The jitter is derived from SHA-256 of:

```text
sample_sha256:instance_id:check_index
```

The first 64 bits are mapped to `[-1, 1]` and multiplied by
`jitter_ratio * interval_ms`, then rounded to an integer millisecond using
half-even rounding. No Python hash randomization, UUID, system time, or random
module is involved.

Window boundaries use the deterministic logical check sequence. Window `0`
ends at check index `window_length - 1`; later windows close every
`window_step` checks. Each committed window also records its first and last
logical scheduled millisecond.

An injected clock value below the persisted high-water mark minus
`clock_rollback_tolerance_ms` returns `E_CLOCK_ROLLBACK` before any check is
inserted or window is committed.

## Response classification

| Classification | Primary code |
| --- | --- |
| Success | `E_OK` |
| Status code differs | `E_STATUS_MISMATCH` |
| Real total budget exceeded and socket actively aborted | `E_TIMEOUT` |
| TCP connection refused | `E_CONN_REFUSED` |
| Invalid HTTP/truncated/oversized response | `E_MALFORMED_RESPONSE` |
| Expected response-body digest differs | `E_SAMPLE_MISMATCH` |
| Other probe I/O failure | `E_PROBE_IO` with malformed-response classification |

Unexpected exceptions in one probe are converted into an isolated failed
observation, so other instances continue. The socket is explicitly shut down
and closed on timeout. A timed-out response is never counted as success after
the fact.

Validation and persistence errors use separate codes including
`E_BAD_SAMPLE`, `E_BAD_INSTANCE`, `E_BAD_PLAN`, `E_BAD_DIAGNOSTICS`,
`E_CLOCK_ROLLBACK`, `E_SAMPLE_CONFLICT`, `E_CHECK_CONFLICT`,
`E_WINDOW_CONFLICT`, and `E_STATE_INCONSISTENT`.

## Window statistics and weight

For each committed window:

```text
success_rate = successful checks / checks in window
health_score = sum(class_weight[class] for each check) / checks in window
effective_weight = configured_weight * slow_start_factor * health_score
```

Class weights come from `plan.score_weights`. Success must have weight `1.0`;
the other classes may be in `[0, 1]`.

p50 and p95 quality quantiles use nearest-rank over the sorted per-check class
weights:

```text
index = ceil(probability * count) - 1
```

Ratios, scores, slow-start factors, effective weights, and quantiles are
converted to fixed point with:

```text
fixed = round_half_even(raw_decimal * fixed_scale)
```

Every conversion records:

- quantity name
- raw float
- exact decimal text
- fixed value
- scale
- `ROUND_HALF_EVEN`
- rounding direction (`none`, `toward_zero`, or `away_from_zero`)

Every window stores a `rounding_trail`; pair records store the same information
in their error-source chain. State-threshold decisions and effective-weight
calculation consume the quantized rate and score, while the original floats
remain available for diagnosis.

## Health state machine

States are `HEALTHY`, `SUSPECT`, and `UNHEALTHY`. Consecutive counters advance
once per newly consumed check. The overlapping part of a sliding window is
never counted twice.

- `HEALTHY -> SUSPECT` if failures reach the instance failure threshold or
  window success rate is below `healthy_success_rate`.
- `SUSPECT -> UNHEALTHY` if failures reach the failure threshold or rate is
  below `suspect_success_rate`.
- `SUSPECT -> HEALTHY` if successes reach the success threshold and rate is at
  least `recovery_success_rate`.
- `UNHEALTHY -> SUSPECT` under the same recovery condition.
- Otherwise the state remains unchanged.

After entering `HEALTHY`, the slow-start age resets. For later healthy windows
the age advances to `ramp_steps`; the factor follows either a linear or power
curve from `initial_ratio` to `1.0`. `SUSPECT` uses `suspect_ratio`, and
`UNHEALTHY` has zero effective weight.

State transitions are only computed inside the SQLite window transaction from
persisted state plus newly committed check observations. No in-memory state is
trusted after restart.

## Precision sensitivity

For each pair with both windows at the same index:

1. Compute the absolute fixed-point score difference.
2. Mark it score-sensitive only when `difference < float_tolerance`.
3. Propagate score rounding/tolerance into an upper bound for effective-weight
   ordering.
4. Allocation depends on the score difference only when the difference is
   nonzero, both weights are positive, and the effective-weight ordering margin
   is within that bound.
5. Mark the pair precision-sensitive only when both conditions hold.

This avoids a false positive when scores are within tolerance but configured
weight differences make the ordering independent of that score difference.

The persisted chain includes both instances, their window scores, score
difference, tolerance comparison, score/weight rounding bounds, effective
weights, ordering margin, allocation-dependency verdict, and window index.

## SQLite and recovery

Defaults:

- DB: `.lbdiag/healthcheck.db`
- logs: `.lbdiag/logs/diagnostics.jsonl`
- instance locks: `.lbdiag/locks/<sample-prefix>/<instance>.lock`

The database uses WAL mode, foreign keys, a 30-second busy timeout, and
`synchronous=FULL`. Main tables:

| Table | Contents |
| --- | --- |
| `meta` | Format version, canonical sample config, hashes, logical watermark |
| `instances` | Validated static instance configuration |
| `instance_state` | State, consecutive counters, recovery age, effective weight, applied watermark |
| `check_results` | One idempotent row per instance/check, including pending observations |
| `windows` | Complete committed window summary and source hashes |
| `sensitivity` | Committed pair diagnostics and error-source chains |

Probe results can be durable before a window is full. Window summary, state
transition, effective weight, and newly available sensitivity pairs are then
written in one `BEGIN IMMEDIATE` transaction. Readers use a deferred read
transaction and only see committed window rows.

A portable file lock (Windows `msvcrt`, POSIX `fcntl`) serializes each instance
across processes. SQLite uniqueness and immediate transactions serialize
commits. Re-submitting the same probe or window returns a no-op; conflicting
content returns a conflict error rather than incrementing counters again.

After a kill:

- an uncommitted SQLite transaction rolls back automatically;
- persisted probe rows may be reused;
- `run`/`resume` computes only complete missing windows;
- already committed windows are skipped;
- counters and effective weights are reconstructed from persisted state.

`verify` treats trailing complete-but-uncommitted windows as recoverable
pending work, not corruption.

## Validation

Run the unit/integration suite:

```bash
python3 -m unittest discover -v -s tests -t .
```

On Windows:

```powershell
py -3 -m unittest discover -v -s tests -t .
```

Run the fixed-port end-to-end validation (port `18765` must be free):

```bash
python3 scripts/run_validation.py
```

Actual result on Python 3.10.12:

- 24/24 automated tests passed.
- Two fresh full runs were byte-identical; an idempotent rerun was also
  byte-identical.
- `status` repeated byte-for-byte.
- `verify` reported zero errors.
- All classification codes were observed:
  `E_OK`, `E_STATUS_MISMATCH`, `E_TIMEOUT`, `E_CONN_REFUSED`,
  `E_MALFORMED_RESPONSE`, and `E_SAMPLE_MISMATCH`.
- Alpha/beta windows 0, 1, and 2 were marked precision-sensitive.
- A forced `os._exit(137)` after 12 durable alpha probes left 1 committed
  window and 8 successful consecutive checks; `resume` finished at 16 checks,
  3 windows, and 16 successes without duplication.
- Two parallel processes handling alpha and beta independently still produced
  exactly 3 windows each and 3 alpha/beta sensitive pair records.

See `artifacts/validation_report.json` for the recorded report.
The unittest transcript is stored in `artifacts/test_results.txt`.

The deterministic crash hook used by tests is:

```text
LBDIAG_TEST_CRASH_AFTER_PROBES=12
```

It performs `os._exit(137)` immediately after the configured number of newly
inserted probes, before the following window commit.

## Notes

- Port `18765` is the sample service port; `18766` is intentionally left closed.
- If ports must change, update all instance addresses and `test_service.port`,
  then set `diagnostics.sample_sha256` to `null` or recompute the canonical
  digest. The digest excludes the `sample_sha256` field itself.
- The service's sequence counters reset when `serve` restarts. The fixed beta
  cycle has length four and each full run performs 16 requests, so repeated
  fresh runs remain deterministic.
- Default `status` output is intentionally complete and therefore fairly large;
  it contains all committed windows and pairs for reproducible comparison.
- The workbench validates literal loopback IP addresses only. It does not
  resolve hostnames and does not contact non-loopback targets.
