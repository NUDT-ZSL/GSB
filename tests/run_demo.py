"""End-to-end acceptance driver.

Exercises:
1. an incremental edit sequence (scale switch, channel toggle, weight change,
   fusion strategy switch) with reuse/recompute ledgers,
2. parallel vs single-thread identical output,
3. incremental final state vs cold full rebuild field-level equality,
4. parser business codes and per-user exception isolation.
"""

from __future__ import annotations

import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from recall_bench.config import load_config  # noqa: E402
from recall_bench.runner import BenchmarkRunner  # noqa: E402
from recall_bench.util import atomic_write_json, read_json  # noqa: E402

SAMPLE_DIR = os.path.join(ROOT, "samples")
CONFIG_PATH = os.path.join(SAMPLE_DIR, "recall_config.json")


def _load_scales(sample_dir: str) -> list[dict]:
    manifest = read_json(os.path.join(sample_dir, "generation_manifest.json"))
    by_id = {row["scale_id"]: row for row in manifest["scales"]}
    profiles = read_json(os.path.join(sample_dir, "scale_profiles.json"))
    for profile in profiles:
        profile.update({
            "log_path": by_id[profile["scale_id"]]["log_path"],
            "dictionary_path": by_id[profile["scale_id"]]["dictionary_path"],
            "input_fingerprint": by_id[profile["scale_id"]]["input_fingerprint"],
        })
    return profiles


def _write_config(config: dict, path: str) -> None:
    atomic_write_json(path, config)


def _runner(sample_dir: str, results_dir: str, config_path: str) -> BenchmarkRunner:
    config = load_config(config_path)
    manifest = read_json(os.path.join(sample_dir, "generation_manifest.json"))
    scales = _load_scales(sample_dir)
    return BenchmarkRunner(sample_dir, results_dir, config, scales, manifest)


def comparable(report: dict) -> dict:
    return {key: value for key, value in report.items()
            if key not in ("report_fingerprint", "incremental",)}


def strip_ledger(report: dict) -> dict:
    cloned = json.loads(json.dumps(report, sort_keys=True))
    for result in cloned.get("scale_results", []):
        result.pop("ledger", None)
    cloned.pop("incremental", None)
    cloned.pop("report_fingerprint", None)
    cloned.pop("config_hash", None)
    if "config" in cloned and "runtime" in cloned["config"]:
        cloned["config"]["runtime"].pop("max_workers", None)
    return cloned


def main() -> int:
    work = os.path.join(ROOT, "results", "demo_work")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    incr_cfg_path = os.path.join(work, "config_incr.json")
    base_config = load_config(CONFIG_PATH)
    _write_config(base_config, incr_cfg_path)

    print("== step 1: baseline all scales ==")
    runner = _runner(SAMPLE_DIR, os.path.join(work, "incr"), incr_cfg_path)
    report = runner.run()
    _print_ledger(report)

    print("== step 2: rerun unchanged -> everything reused ==")
    runner = _runner(SAMPLE_DIR, os.path.join(work, "incr"), incr_cfg_path)
    report = runner.run()
    _print_ledger(report)
    assert report["incremental"]["recomputed_count"] == 0, "unchanged rerun must fully reuse cache"

    print("== step 3: disable collaborative channel ==")
    cfg = load_config(incr_cfg_path)
    cfg["channels"]["collaborative"]["enabled"] = False
    _write_config(cfg, incr_cfg_path)
    runner = _runner(SAMPLE_DIR, os.path.join(work, "incr"), incr_cfg_path)
    report_toggle = runner.run()
    _print_ledger(report_toggle)
    recomputed_artifacts = {row["artifact"] for row in report_toggle["incremental"]["recomputed"]}
    assert "recall_lexical" not in recomputed_artifacts, "lexical must stay cached when CF toggles"
    assert "parsed" not in recomputed_artifacts, "parsed data must stay cached on config change"

    print("== step 4: change channel weight only ==")
    cfg = load_config(incr_cfg_path)
    cfg["channels"]["popularity"]["weight"] = 0.40
    _write_config(cfg, incr_cfg_path)
    runner = _runner(SAMPLE_DIR, os.path.join(work, "incr"), incr_cfg_path)
    report_weight = runner.run()
    _print_ledger(report_weight)
    recomputed_artifacts = {row["artifact"] for row in report_weight["incremental"]["recomputed"]}
    assert "recall_popularity" not in recomputed_artifacts, "recall output independent of fusion weight"
    assert "fusion" in recomputed_artifacts, "fusion must recompute when weights change"

    print("== step 5: switch fusion strategy to rrf ==")
    cfg = load_config(incr_cfg_path)
    cfg["fusion"]["strategy"] = "rrf"
    _write_config(cfg, incr_cfg_path)
    runner = _runner(SAMPLE_DIR, os.path.join(work, "incr"), incr_cfg_path)
    report_final_incr = runner.run()
    _print_ledger(report_final_incr)

    print("== step 6: cold full rebuild with identical final config ==")
    full_cfg_path = os.path.join(work, "config_full.json")
    _write_config(load_config(incr_cfg_path), full_cfg_path)
    runner_full = _runner(SAMPLE_DIR, os.path.join(work, "full"), full_cfg_path)
    report_full = runner_full.run()

    if strip_ledger(report_final_incr) != strip_ledger(report_full):
        _diff(strip_ledger(report_final_incr), strip_ledger(report_full))
        raise AssertionError("incremental vs full rebuild mismatch")
    for incr_result, full_result in zip(
        report_final_incr["scale_results"], report_full["scale_results"]
    ):
        assert incr_result["cache_keys"] == full_result["cache_keys"], "cache keys must converge"
    print("OK: incremental final state == cold full rebuild (field-level)")

    print("== step 7: single-thread vs thread-pool ==")
    serial_cfg = os.path.join(work, "config_serial.json")
    serial_config = load_config(incr_cfg_path)
    serial_config["runtime"]["max_workers"] = 1
    _write_config(serial_config, serial_cfg)
    runner_serial = _runner(SAMPLE_DIR, os.path.join(work, "serial"), serial_cfg)
    report_serial = runner_serial.run()
    parallel_config = load_config(incr_cfg_path)
    parallel_config["runtime"]["max_workers"] = 4
    parallel_cfg = os.path.join(work, "config_parallel.json")
    _write_config(parallel_config, parallel_cfg)
    runner_parallel = _runner(SAMPLE_DIR, os.path.join(work, "parallel"), parallel_cfg)
    report_parallel = runner_parallel.run()
    if strip_ledger(report_serial) != strip_ledger(report_parallel):
        _diff(strip_ledger(report_serial), strip_ledger(report_parallel))
        raise AssertionError("serial vs parallel mismatch")
    print("OK: serial(max_workers=1) == parallel(max_workers=4) (field-level)")

    print("== step 8: business codes present in every scale ==")
    expected = {"E_LOG_INVALID", "E_LOG_DUPLICATE", "E_LOG_ORDER", "E_ITEM_MISSING"}
    for result in report_full["scale_results"]:
        codes = set(result["failures"]["code_counts"])
        assert expected.issubset(codes), f"{result['scale_id']} missing {expected - codes}"
    print("OK: all four parser business codes present per scale")

    summary_path = os.path.join(work, "demo_summary.json")
    atomic_write_json(summary_path, {
        "incremental_report": os.path.join(work, "incr", "reports", "comparison_report.json"),
        "full_report": os.path.join(work, "full", "reports", "comparison_report.json"),
        "serial_report": os.path.join(work, "serial", "reports", "comparison_report.json"),
        "parallel_report": os.path.join(work, "parallel", "reports", "comparison_report.json"),
        "status": "all_checks_passed",
    })
    print(f"summary written to {summary_path}")
    return 0


def _print_ledger(report: dict) -> None:
    inc = report["incremental"]
    print(f"  reused={inc['reused_count']} recomputed={inc['recomputed_count']}")


def _diff(left, right, path: str = "$") -> None:
    if type(left) is not type(right):
        print(f"TYPE DIFF {path}")
        return
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                print(f"KEY DIFF {path}.{key}")
            else:
                _diff(left[key], right[key], f"{path}.{key}")
    elif isinstance(left, list):
        if len(left) != len(right):
            print(f"LEN DIFF {path}: {len(left)} vs {len(right)}")
        for idx, (a, b) in enumerate(zip(left, right)):
            _diff(a, b, f"{path}[{idx}]")
    elif left != right:
        print(f"VALUE DIFF {path}: {left!r} vs {right!r}")


if __name__ == "__main__":
    sys.exit(main())
