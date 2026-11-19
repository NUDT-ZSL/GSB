# Deterministic sample

- `table.json`: `accounts` table with integer/text/integer/text columns.
- `sessions.json`: ten ordered concurrent sessions. Start clocks are non-decreasing and include repeatable-read, read-committed, commit, rollback, delete/reinsert, and range reads.
- `config.json`: logical clock step 10ms, 32 retained views, 2ms clock-regression tolerance, transaction-number GC watermark 1000, and 2000ms logical query budget.

Typical run:

```powershell
python -m mvcc_app init --table samples/table.json --config samples/config.json --sessions samples/sessions.json --replay
python -m mvcc_app read --txn-id 102 --clock-ms 2010 --where '[{"column":"id","op":"eq","value":1}]'
python -m mvcc_app gc --clock-ms 2200
python -m mvcc_app verify --runs 2 --gc-clock-ms 2200
```

The sample is self-contained and uses no network or external data.

