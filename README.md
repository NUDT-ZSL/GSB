# HTTP/2 Multiplexing Diagnostic Workbench

A local, fully deterministic HTTP/2 multiplexing diagnostic workbench.
One JSON declaration describes a connection's initial conditions; the
workbench runs a minimal HTTP/2 peer on a loopback TCP port, multiplexes
the declared streams from the client side, chooses between send-scheduling
strategies, and diagnoses flow-control windows, priority fairness and
protocol state-machine behaviour. Everything runs locally — no real site,
account or external service is ever contacted.

## Environment

- Python 3.11 (Tkinter ships with the standard Windows installer;
  verified importable on 3.11)
- `h2` 4.4.1, `hyperframe` 6.1.0, `hpack` 4.2.0 (frame codec + HPACK)
- Offline wheels are vendored in `vendor/wheels/`:
  `pip install --no-index --find-links vendor/wheels h2 hyperframe hpack`
- No Docker, no containers, loopback TCP only

Quick environment check:

```bash
python3 scripts/check_env.py
```

## Layout

```
h2workbench/        workbench package
  manifest.py       declaration loading + strong validation (E_DECL)
  peer.py           loopback HTTP/2 peer, deterministic WINDOW_UPDATE rule
  client.py         client engine: executes the plan, records observations
  planner.py        deterministic send-plan simulator (byte-exact)
  scheduler.py      strategies: weighted_rr / window_first / arrival
  policy.py         scale-adaptive strategy selection with stable tie-break
  ledger.py         per-stream + connection window ledger, reconciliation
  fairness.py       priority fairness + starvation diagnostics
  session.py        orchestration: plan -> run -> reconcile -> report
  snapshot.py       versioned snapshots, whole-replace restore (E_REPLAY)
  gui.py            Tkinter visualization workbench
samples/            deterministic declaration manifests (small/medium/large)
scripts/run_demo.py three-scenario reproducible demo
scripts/run_gui.py  Tkinter UI entry point
scripts/run_peer.py standalone loopback peer
tests/              unittest suite (50 tests)
docs/               design notes, verification record, original task
artifacts/          generated reports and snapshots
```

## Run

```bash
python3 scripts/run_demo.py     # 3 deterministic scenarios + diagnostics
python3 scripts/run_gui.py      # Tkinter visualization workbench
python3 -m unittest discover -s tests
```

## Declaration manifest

`samples/manifest_small.json` shows the schema: connection initial windows
(client/server send directions), max frame size, max concurrent streams,
the deterministic peer rule (grant increment / latency / response size),
the policy tie threshold, and the stream set (weight, dependency parent,
request body size, path). Any violation — window or frame size out of
protocol range, weight outside 1..256, dependency cycle, concurrency above
the local bound — rejects the whole manifest with `E_DECL` and the exact
field path (e.g. `$.streams[0].weight`).

## Diagnostic codes

- `E_DECL`   declaration validation failure (whole manifest rejected)
- `E_PROTO`  connection/stream state-machine violation (frame type + stream)
- `E_WINDOW` window ledger vs peer increment mismatch (first deviating
             stream + byte count)
- `E_REPLAY` snapshot replay not reproducible (first diverging frame)

See `docs/DESIGN.md` for the deterministic peer rule, the strategy
selection rule and the snapshot semantics, and `docs/VERIFICATION.md`
for the recorded verification results.
