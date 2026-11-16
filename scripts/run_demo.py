"""Deterministic end-to-end demo, fully offline.

Outputs (stdout + artifacts/demo_result.json):
  * happy-path replay executed twice with bit-identical fingerprints/edges;
  * every anomaly scenario's business code and rollback verification;
  * a v0 -> latest lineage diff report with rule attribution.

Usage: python scripts/run_demo.py
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from lineage_migrator.diff import diff_versions
from lineage_migrator.engine import Engine
from lineage_migrator.injector import run_all_scenarios
from lineage_migrator.models import Sample


def main() -> int:
    sample = Sample.load(PROJECT_ROOT / "samples")
    data_dir = PROJECT_ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    artifacts = PROJECT_ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)

    print("== 1. happy-path replay, two independent runs ==")
    run_records = []
    for run_index in (1, 2):
        db_path = data_dir / f"demo_replay_{run_index}.db"
        if db_path.exists():
            db_path.unlink()
        engine = Engine.bootstrap(db_path, sample)
        engine.replay_all(sample)
        store = engine.store
        run_records.append({
            "run": run_index,
            "versions": store.versions(),
            "final_edges": [edge.__dict__ for edge in store.snapshot_edges(store.last_version())],
        })
        store.close()
    bit_identical = (
        run_records[0]["versions"] == run_records[1]["versions"]
        and run_records[0]["final_edges"] == run_records[1]["final_edges"]
    )
    print(f"bit-identical across runs: {bit_identical}")
    for item in run_records[0]["versions"]:
        print(
            f"  v{item['version']}: fields={item['field_count']} edges={item['edge_count']}"
            f" fingerprint={item['fingerprint'][:16]}..."
        )

    print("\n== 2. anomaly injection (isolated scratch DB per scenario) ==")
    anomaly_results = run_all_scenarios(
        sample,
        PROJECT_ROOT / "samples" / "anomalies.json",
        data_dir / "anomaly_runs",
    )
    code_sequence = []
    for result in anomaly_results:
        code_sequence.append(result["actual_code"])
        print(
            f"  {result['scenario_id']}: expected={result['expected_code']}"
            f" actual={result['actual_code']} matched={result['matched']}"
            f" rolled_back_cleanly={result['rolled_back_cleanly']}"
        )
    print("business code sequence:", code_sequence)

    print("\n== 3. diff report v0 -> last committed version ==")
    inspect_db = data_dir / "demo_replay_1.db"
    from lineage_migrator.store import Store

    store = Store(inspect_db)
    last = store.last_version()
    report = diff_versions(store, 0, last)
    store.close()
    print(
        "summary:",
        f"added={report['summary']['added']}",
        f"removed={report['summary']['removed']}",
        f"endpoint_changed={report['summary']['endpoint_changed']}",
    )

    all_matched = all(item["matched"] for item in anomaly_results)
    all_rolled_back = all(item["rolled_back_cleanly"] for item in anomaly_results)
    output = {
        "bit_identical_replay": bit_identical,
        "replay_runs": run_records,
        "anomaly_results": anomaly_results,
        "business_code_sequence": code_sequence,
        "all_anomaly_codes_matched": all_matched,
        "all_rollbacks_clean": all_rolled_back,
        "diff_v0_to_latest": report,
    }
    out_path = artifacts / "demo_result.json"
    out_path.write_text(json.dumps(output, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    print(f"\nfull result written to {out_path}")

    ok = bit_identical and all_matched and all_rolled_back
    print("OVERALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
