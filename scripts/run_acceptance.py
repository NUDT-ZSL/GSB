"""Acceptance run: main pipeline + business-code scenario checks.

Verifies cross-path consistency, dual-budget rejection codes and snapshot
rollback codes, then writes reports/acceptance_report.json.
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mt_pipeline.errors import Codes, PipelineError
from mt_pipeline.pipeline import run_pipeline
from mt_pipeline.snapshots import SnapshotStore
from mt_pipeline.util import read_json, write_json


def expect_code(fn, code):
    try:
        fn()
    except PipelineError as exc:
        return {"expected": code, "observed": exc.code, "pass": exc.code == code,
                "detail": exc.to_dict()}
    return {"expected": code, "observed": None, "pass": False}


def main():
    config = read_json("config/pipeline_config.json")
    results = {}

    report = run_pipeline(config, "data", "reports", "snapshots")
    results["main_run"] = {
        "path": report["decision_record"]["decision"]["path"],
        "self_check": report["self_check"],
        "kept": report["metrics"]["kept"],
        "total": report["metrics"]["total"],
        "keep_rate": report["metrics"]["keep_rate"],
        "consistency": report["consistency"],
    }

    tight_compare = copy.deepcopy(config)
    tight_compare["budgets"]["compare_budget"] = 10
    results["compare_budget"] = expect_code(
        lambda: run_pipeline(tight_compare, "data", "reports/_cb", "snapshots/_cb"),
        Codes.COMPARE_BUDGET)

    tight_memory = copy.deepcopy(config)
    tight_memory["budgets"]["memory_limit"] = 8
    results["resource_limit"] = expect_code(
        lambda: run_pipeline(tight_memory, "data", "reports/_rl", "snapshots/_rl"),
        Codes.RESOURCE_LIMIT)

    store = SnapshotStore("snapshots/_rollback")
    store.save("vocab", 2, ["a", "b"])
    results["snapshot_rollback"] = expect_code(
        lambda: store.save("vocab", 1, ["a", "b"]),
        Codes.SNAPSHOT_ROLLBACK)

    corrupt_dir = Path("snapshots/_corrupt")
    corrupt_dir.mkdir(parents=True, exist_ok=True)
    (corrupt_dir / "vocab.snapshot.json").write_text("{not json", encoding="utf-8")
    results["snapshot_corrupt"] = expect_code(
        lambda: SnapshotStore(corrupt_dir).load("vocab"),
        Codes.SNAPSHOT_CORRUPT)

    results["all_pass"] = all(
        r.get("pass", True) for k, r in results.items() if isinstance(r, dict))
    write_json("reports/acceptance_report.json", results)
    print(f"acceptance all_pass={results['all_pass']}")
    for key, val in results.items():
        if isinstance(val, dict) and "observed" in val:
            print(f"  {key}: expected={val['expected']} observed={val['observed']}")


if __name__ == "__main__":
    main()
