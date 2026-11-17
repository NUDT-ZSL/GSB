"""End-to-end demonstration and acceptance harness.

Runs deterministic subprocess CLI invocations to prove:

1. uninterrupted processing passes the independent verifier;
2. interrupted -> checkpointed -> resumed processing is byte-identical;
3. snapshot parameter-version mismatch triggers whole-state replacement;
4. injected E_SNAPSHOT / E_BATCH / E_ORDER faults report stable codes;
5. E_SCHEMA / E_EXPR validation paths reject bad inputs.

Only input files and CLI parameters change; engine source is never modified.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_TABLES = os.path.join(ROOT, "sample", "tables.json")
SAMPLE_QUERIES = os.path.join(ROOT, "sample", "queries.json")


def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "aggwin.cli", *args],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )


def _write_json(path: str, document) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)


def _truncate_tables(source: str, target: str, limits: dict[str, int]) -> None:
    document = json.load(open(source, encoding="utf-8"))
    for table in document["tables"]:
        if table["name"] in limits:
            table["rows"] = table["rows"][:limits[table["name"]]]
    _write_json(target, document)


def main() -> int:
    runtime = os.path.join(ROOT, "runtime", "demo")
    shutil.rmtree(runtime, ignore_errors=True)
    os.makedirs(runtime, exist_ok=True)
    findings: list[dict] = []

    def record(name: str, process: subprocess.CompletedProcess,
               expected_code: int) -> None:
        ok = process.returncode == expected_code
        payload = None
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError:
            payload = {"raw_stdout": process.stdout[:200]}
        findings.append({
            "scenario": name,
            "expected_exit": expected_code,
            "actual_exit": process.returncode,
            "status": "PASS" if ok else "FAIL",
            "business_code": payload.get("code") if isinstance(payload, dict) else None,
        })

    # 1. Uninterrupted baseline.
    full_snap = os.path.join(runtime, "snap_full")
    full_result = os.path.join(runtime, "result_full")
    proc = _run_cli(["run", "--tables", SAMPLE_TABLES, "--queries", SAMPLE_QUERIES,
                     "--snapshot-dir", full_snap, "--result-dir", full_result,
                     "--log-file", os.path.join(runtime, "full.log")])
    record("uninterrupted_run", proc, 0)

    proc = _run_cli(["verify", "--tables", SAMPLE_TABLES, "--queries", SAMPLE_QUERIES,
                     "--snapshot-dir", full_snap, "--result-dir", full_result])
    record("independent_verification", proc, 0)

    # 2. Interrupt after partial tables, then resume with full tables.
    partial_path = os.path.join(runtime, "tables_partial.json")
    _truncate_tables(SAMPLE_TABLES, partial_path, {"orders": 6, "clicks": 3})
    part_snap = os.path.join(runtime, "snap_resume")
    part_result = os.path.join(runtime, "result_partial")
    proc = _run_cli(["run", "--tables", partial_path, "--queries", SAMPLE_QUERIES,
                     "--snapshot-dir", part_snap, "--result-dir", part_result,
                     "--batch-size", "2"])
    record("interrupted_partial_run", proc, 0)

    resume_result = os.path.join(runtime, "result_resumed")
    proc = _run_cli(["run", "--tables", SAMPLE_TABLES, "--queries", SAMPLE_QUERIES,
                     "--snapshot-dir", part_snap, "--result-dir", resume_result,
                     "--batch-size", "2"])
    record("resumed_run", proc, 0)

    proc = _run_cli(["compare", "--left", full_result, "--right", resume_result])
    record("byte_identical_resume_vs_full", proc, 0)

    proc = _run_cli(["verify", "--tables", SAMPLE_TABLES, "--queries", SAMPLE_QUERIES,
                     "--snapshot-dir", part_snap, "--result-dir", resume_result])
    record("verify_resumed_outputs", proc, 0)

    # 3. Whole-state replacement on parameter digest mismatch.
    queries_doc = json.load(open(SAMPLE_QUERIES, encoding="utf-8"))
    changed = json.loads(json.dumps(queries_doc))
    for query in changed["queries"]:
        if isinstance(query, dict) and query.get("id") == "q_window_sum_orders":
            query["frame"]["upper"] = 1
    changed_queries = os.path.join(runtime, "queries_changed.json")
    _write_json(changed_queries, changed)
    replaced_result = os.path.join(runtime, "result_replaced")
    proc = _run_cli(["run", "--tables", SAMPLE_TABLES, "--queries", changed_queries,
                     "--snapshot-dir", part_snap, "--result-dir", replaced_result])
    replaced_ok = proc.returncode == 0 and json.loads(
        proc.stdout).get("replaced_state") is True
    findings.append({
        "scenario": "whole_state_replacement_on_param_mismatch",
        "status": "PASS" if replaced_ok else "FAIL",
        "expected_exit": 0,
        "actual_exit": proc.returncode,
        "business_code": None,
    })

    # 4a. E_SNAPSHOT: corrupt the latest snapshot, resume must fail cleanly.
    corrupt_snap = os.path.join(runtime, "snap_corrupt")
    corrupt_result = os.path.join(runtime, "result_corrupt")
    _run_cli(["run", "--tables", partial_path, "--queries", SAMPLE_QUERIES,
              "--snapshot-dir", corrupt_snap, "--result-dir",
              os.path.join(runtime, "result_corrupt_seed"), "--batch-size", "2"])
    pointer = open(os.path.join(corrupt_snap, "latest.snapshot"),
                   encoding="utf-8").read().strip()
    SnapshotStore = None
    from aggwin.snapshot import SnapshotStore as _Store
    _Store.corrupt(os.path.join(corrupt_snap, pointer))
    proc = _run_cli(["run", "--tables", SAMPLE_TABLES, "--queries", SAMPLE_QUERIES,
                     "--snapshot-dir", corrupt_snap, "--result-dir", corrupt_result])
    record("injected_corrupt_snapshot_E_SNAPSHOT", proc, 4)

    # 4b. E_BATCH: mid-window boundary surfaced with strict exit.
    proc = _run_cli(["run", "--tables", SAMPLE_TABLES, "--queries", SAMPLE_QUERIES,
                     "--snapshot-dir", os.path.join(runtime, "snap_batch"),
                     "--result-dir", os.path.join(runtime, "result_batch"),
                     "--batch-size", "2", "--inject-mid-window",
                     "--strict-batch"])
    record("injected_mid_window_E_BATCH", proc, 5)

    # 4c. E_ORDER: force a zero out-of-order allowance.
    proc = _run_cli(["run", "--tables", SAMPLE_TABLES, "--queries", SAMPLE_QUERIES,
                     "--snapshot-dir", os.path.join(runtime, "snap_order"),
                     "--result-dir", os.path.join(runtime, "result_order"),
                     "--ooo-ratio-limit", "0"])
    record("injected_out_of_order_E_ORDER", proc, 6)

    # 5a. E_SCHEMA: duplicate table name.
    bad_schema = os.path.join(runtime, "bad_schema.json")
    bad_doc = json.load(open(SAMPLE_TABLES, encoding="utf-8"))
    bad_doc["tables"].append(json.loads(json.dumps(bad_doc["tables"][0])))
    _write_json(bad_schema, bad_doc)
    proc = _run_cli(["run", "--tables", bad_schema, "--queries", SAMPLE_QUERIES,
                     "--snapshot-dir", os.path.join(runtime, "snap_schema"),
                     "--result-dir", os.path.join(runtime, "result_schema")])
    record("invalid_schema_E_SCHEMA", proc, 2)

    # 5b. E_EXPR: subquery rejection.
    bad_queries = os.path.join(runtime, "bad_queries.json")
    _write_json(bad_queries, {"version": 1, "queries": [
        "SELECT * FROM (SELECT amount FROM orders)"]})
    proc = _run_cli(["run", "--tables", SAMPLE_TABLES, "--queries", bad_queries,
                     "--snapshot-dir", os.path.join(runtime, "snap_expr"),
                     "--result-dir", os.path.join(runtime, "result_expr")])
    record("subquery_E_EXPR", proc, 3)

    report_path = os.path.join(runtime, "acceptance_report.json")
    summary = {
        "overall_status": "PASS" if all(item["status"] == "PASS"
                                        for item in findings) else "FAIL",
        "scenarios": findings,
    }
    _write_json(report_path, summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["overall_status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
