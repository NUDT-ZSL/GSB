# Verification Record

Date: 2026-11-17. All checks run locally against the loopback peer with
the deterministic sample manifests; no external site, account or service
was contacted. No containers were used.

## Environment

- Python 3.11.0rc1 (target: Python 3.11 on Windows, Tkinter bundled)
- h2 4.4.1, hyperframe 6.1.0, hpack 4.2.0 — installed from PyPI and
  vendored as offline wheels in `vendor/wheels/`
- `python3 scripts/check_env.py` -> `env check: OK`
  (tkinter warning on this headless Linux dev box only; the GUI was
  verified against a locally provided Tk 8.6 runtime under Xvfb, and
  Tkinter ships with the standard Windows Python installer)

## Test suite

`python3 -m unittest discover -s tests` -> **Ran 50 tests, OK**

Coverage: manifest strong validation (E_DECL paths), planner
determinism and frame/window limits, ledger accounting and first-
deviation reconciliation, fairness rows/starvation/fallbacks, policy
determinism and stable tie-break, snapshot roundtrip / whole-replace /
E_REPLAY tamper rejection, loopback end-to-end reconciliation for all
three samples and all three strategies, and a raw misbehaving client
triggering peer-side E_PROTO (DATA before HEADERS).

## Demo scenarios (`python3 scripts/run_demo.py`, exit 0)

| scenario | manifest | strategy | frames | stalls | fairness note |
|---|---|---|---|---|---|
| 1 small, single strategy | small-3-streams v1 | arrival (forced) | 23 | 14 | no starvation |
| 2a switch strategy | medium-6-streams v2 | window_first (forced) | 39 | 19 | stream 4 starved |
| 2b switched | medium-6-streams v2 | weighted_rr (forced) | 39 | 19 | starvation cleared |
| 3 scale up, auto | large-12-streams v3 | weighted_rr (auto) | 50 | 12 | low-weight streams flagged |

Every scenario: `progress_consistent=True`,
`window_reconciliation=ok`, `diagnostics: none` — the observed frame
sequence and every peer WINDOW_UPDATE increment match the plan
byte-for-byte. Reports are written to `artifacts/reports/`.

Auto-selection record (scenario 3): candidates weighted_rr=0ms,
window_first=1ms, arrival=1ms estimated stall cost; all within the 8ms
tie threshold, so the stable priority order selected `weighted_rr`; the
reason string and all candidate costs are stored in the report together
with the send plan.

## Snapshot verification

- save -> `verify_replay: ok` (50 frames, weighted_rr)
- session manifest version bumped 3 -> 4, then restore: `replaced=True`,
  whole session state replaced from the snapshot (no merge), replay ok
- tampered snapshot (one frame byte count +1): rejected with
  `E_REPLAY: snapshot plan is not reproducible: first divergence at
  frame 3 [$.plan.frames[3]]`
- tampered predicted grants: rejected with E_REPLAY (unit test)

## Declaration validation

`samples/manifest_invalid_weight.json` (weight 0) is rejected wholesale:
`E_DECL: field 'weight' must be in [1, 256], got 0 [$.streams[0].weight]`.
Further E_DECL cases (frame size, window range, dependency cycle,
duplicate ids, concurrency bound, ...) are covered by unit tests.

## GUI smoke (Tk 8.6 under Xvfb)

Three scenarios executed through the real UI handlers (load manifest ->
run -> render -> save/load snapshot): timeline, window curves, fairness
table and snapshot list all rendered; reconciliation `ok` for each run;
snapshot save/load through the UI succeeded.

## Determinism

- same manifest + same strategy simulated twice -> identical plan dicts
- same session run twice -> identical plan frames, identical predicted
  grants and identical observed grants (unit test)
- fairness output sorted by stream id; snapshot listing sorted by name
