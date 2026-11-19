# Validation Record

Validation was run locally on 2026-11-20 with Python 3.12.13 and SQLite 3.53.1.
NumPy 2.5.5 was used only to generate the deterministic sample.

## Clean sample run

Commands:

```text
python colagg.py clean
python colagg.py load
python colagg.py execute
python colagg.py status
python colagg.py verify
python colagg.py execute
```

Observed:

```json
{
  "results_sha256": "c18bd0d7922b7b19b2e29b29ac6228a42e732f74bef88781601af89cd8a1a721",
  "result_digest": "314c5610c7f60e44ba8fa60d9170040caa0c07ecc98a5e37b7f1665bbb392ec0",
  "aggregates": 12,
  "windows": 26,
  "sensitive_aggregates": 8,
  "sensitive_windows": 20,
  "processed_batches": 3,
  "finalized_operators": 2,
  "status_batches": {
    "committed": 3,
    "failed": 0,
    "max_logical_clock": 1020,
    "partial": 0,
    "pending": 0,
    "total": 3
  },
  "verify_ok": true,
  "verify_checks": {
    "aggregate_comparisons": 12,
    "batch_output_digests": 18,
    "batch_status_counts": {
      "committed": 3
    },
    "output_file_matches": true,
    "sqlite_quick_check": true,
    "state_comparisons": 6,
    "window_comparisons": 26
  },
  "second_run_byte_identical": true
}
```

## Forced termination

`python colagg.py execute --crash-after 2` exited with status 137 after two
committed batches. At that point status showed two committed batches and one
pending batch. `python colagg.py resume` processed exactly one missing batch,
and `python colagg.py verify` passed with zero mismatches. The final
`results.json` was byte-identical to the uninterrupted run.

## Failure isolation

A 32-bit fixed-point fixture produced `E_FIXED_OVERFLOW` only for `big_sum`.
`small_sum` and `small_count` remained committed with one batch each. The batch
was marked `partial`, and the result file contained only the two successful
operators plus the failure spectrum.

A post-load batch mutation without a matching digest produced
`E_BATCH_DIGEST_MISMATCH`. The batch was marked failed and every uncommitted
operator entered a persisted failed state.

## Concurrency

Two simultaneous workers, each limited with `--max-batches 1`, committed two
different batches and both exited with code 0. After one final execute, the
applied ledger contained 18 rows and zero duplicate
`(operator_id, batch_seq)` keys. Verification passed.

## Automated tests

```text
python -m unittest discover -s tests -v

Ran 10 tests
OK
```

