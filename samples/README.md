# Deterministic sample

`deterministic_session.json` is consumed by both the Tkinter workbench and the
CLI.  It is intentionally explicit about transaction numbers so allocations
are reproducible.  Every `read` invokes both the fast path and authoritative
replay and compares the canonical JSON byte-for-byte.

`query_sequence.json` is the extracted stable query order for review and is
checked against the executable script by the test suite.

The script covers:

- read-your-own-write;
- a transaction that commits after another transaction's snapshot was taken;
- visibility of older versions while a newer creator remains active;
- rollback of an intermediate writer;
- committed tombstones and rolled-back resurrection attempts;
- a latest-snapshot query after all transactions have terminated.

Run it with:

```bash
python -m mvcc_workbench reset --session-dir run
python -m mvcc_workbench run-script --session-dir run --script samples/deterministic_session.json
```
