# Architecture

## Data flow

```text
spec.json
  -> strict loader (E_SPEC, field paths)
  -> node block files / loopback TCP digest queries
  -> ReplicaBlockState[] (actual SHA-256 + shard digests)
  -> deterministic digest groups
       frequency -> total priority -> sorted node-id tuple
  -> truth digest + representative source + exact mismatch set
  -> floor fixed-point ratio alert and repair-cost model
  -> error-bound tie channel + stable source ordering
  -> RepairTask[]
  -> loopback BLOCK_GET / BLOCK_PUT with post-write digest verification
  -> manifest synchronization
  -> before/after analysis
  -> sealed JSON ledger + SQLite mirror
```

## Modules

| Module | Responsibility |
| --- | --- |
| `spec.py` | JSON duplicate-key rejection, schema validation, field paths, DAG cycle check, logical block-to-file mapping |
| `integrity.py` | Actual full-block SHA-256 and manifest shard verification; `E_BLOCK`/`E_INTEGRITY` |
| `consensus.py` | Digest groups, truth selection, mismatch set, ratio alert, order-independent state digest |
| `fixed.py` | Integer fixed-point quantization, ratio comparison bounds, weighted cost terms, source ranking and tie channel |
| `planner.py` | Per-mismatch repair tasks, source selection, transfer/work estimates, after-plan distribution |
| `node_store.py` | Temp-file + fsync + `os.replace` block writes, protocol journal, cold-scan recovery |
| `protocol.py` | Loopback-only JSON-lines TCP server/client and framing/error handling |
| `analysis.py` | Local/TCP analysis orchestration and repair execution |
| `grid.py` | Grid perturbation, invariant checks, explained flip events |
| `ledger.py` | Sealed JSON + SQLite persistence, version/content comparison, whole-view replacement and read-only backups |
| `presentation.py` / `gui.py` | Stable diagnostic row builders and Tkinter Treeview UI |
| `demo.py` / `samples.py` | Three deterministic experiments and generated sample scenarios |

## Determinism boundaries

Ordered explicitly:

- nodes by `node_id`;
- blocks by `block_id`;
- digest groups by `-count`, `-priority_sum`, sorted node-id tuple;
- truth source by priority descending then node id;
- cost candidates by estimated cost, priority descending, node id;
- repair tasks by block id and target node id;
- ledger rows by phase/node/block or block/target.

Never used for business decisions:

- Python `dict` insertion order as a tie-breaker;
- hash iteration order;
- floating-point values;
- wall-clock time;
- TCP connection order or ephemeral port assignment.

## Whole-view ledger replacement

The current identity is `group_state_digest = SHA-256(spec_digest + sorted replica
content digests)`. A repair changes block contents and therefore the state digest.
On reopen, the caller supplies the current expected state digest. If either the
format version or state digest differs, the view is `stale`; `reconcile` writes the
entire supplied current payload and backs up both previous JSON and SQLite files as
read-only. There is no field-level merge path.

## Crash behavior

Node writes are atomic per block. The protocol journal records requests and
`PUT_COMMIT` events, but startup recovery always scans actual block files first.
Journal records are supporting evidence, not a substitute for data. No clean-exit
report exists; a forced termination leaves only block files and journal lines.
