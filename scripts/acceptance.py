#!/usr/bin/env python3
from __future__ import annotations

import copy
import json
import platform
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from layout_engine import LayoutEngine
from layout_engine.config import LayoutConfig
from layout_engine.errors import ConfigError
from layout_engine.interactive import InteractiveSession
from tests.helpers import canonical_page_core, canonical_pages, read_json, write_json
from tools.generate_samples import generate


def _copy_workspace(source: Path, destination: Path) -> dict[str, Path]:
    shutil.copytree(source / "images", destination / "images")
    for name in ("config.json", "metadata.json", "layout_truth.json"):
        shutil.copy2(source / name, destination / name)
    return {
        "root": destination,
        "images": destination / "images",
        "config": destination / "config.json",
        "metadata": destination / "metadata.json",
        "truth": destination / "layout_truth.json",
    }


def _run(
    workspace: dict[str, Path],
    *,
    snapshot: Path,
    allow_config_change: bool = False,
    full_rebuild: bool = False,
    truth: bool = False,
    stop_event: threading.Event | None = None,
    callback=None,
):
    return LayoutEngine(
        workspace["images"],
        workspace["config"],
        workspace["metadata"],
        snapshot,
        truth_path=workspace["truth"] if truth else None,
        allow_config_change=allow_config_change,
        full_rebuild=full_rebuild,
        stop_event=stop_event,
        page_commit_callback=callback,
    ).run()


def main() -> int:
    started = time.perf_counter()
    checks: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    reports_dir = PROJECT_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    def check(name: str, passed: bool, **detail: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), **detail})

    with tempfile.TemporaryDirectory(prefix="layout-acceptance-") as temporary:
        root = Path(temporary)
        generated = root / "generated"
        generate(generated, pages=16, seed=20261117)
        working = _copy_workspace(generated, root / "working")
        incremental_snapshot = root / "incremental-snapshot"

        baseline = _run(working, snapshot=incremental_snapshot, truth=True)
        baseline_truth = baseline.report["summary"]["truth_validation"]
        check(
            "baseline_full_run_truth",
            baseline.exit_code == 0
            and baseline_truth["all_passed"]
            and baseline.report["summary"]["reading_order_entries"] == 208,
            pages=baseline.report["summary"]["pages_processed"],
            entries=baseline.report["summary"]["reading_order_entries"],
            role_counts=baseline.report["summary"]["role_counts"],
        )
        details["baseline"] = {
            "summary": baseline.report["summary"],
            "adaptive_selected": baseline.report["adaptive_decision"]["selected"],
            "config_hash": baseline.report["configuration"]["config_hash"],
        }
        _write_json_file(reports_dir / "experiment_report.json", baseline.report)

        reused = _run(working, snapshot=incremental_snapshot, truth=True)
        all_reused = all(
            set(item["reused_stages"]) == {"detect", "roles", "columns", "order"}
            and not item["detection_performed"]
            for item in reused.report["reuse_recompute"]
        )
        check("unchanged_resume_reuses_all_stages", reused.exit_code == 0 and all_reused)

        config_payload = read_json(working["config"])
        config_payload["analysis"]["binary_threshold"] = 120
        write_json(working["config"], config_payload)
        strict = _run(working, snapshot=incremental_snapshot, truth=True)
        check(
            "strict_config_hash_mismatch",
            strict.exit_code == 3
            and "E_CONFIG_MISMATCH" in strict.report["business_codes"],
            business_codes=strict.report["business_codes"],
        )

        threshold_run = _run(
            working, snapshot=incremental_snapshot, allow_config_change=True, truth=True
        )
        cascade = all(
            set(item["recomputed_stages"]) == {"detect", "roles", "columns", "order"}
            for item in threshold_run.report["reuse_recompute"]
        )
        check(
            "threshold_cascades_all_downstream_stages",
            threshold_run.exit_code == 0
            and cascade
            and threshold_run.report["summary"]["truth_validation"]["all_passed"],
            recomputed_pages=len(threshold_run.report["reuse_recompute"]),
        )

        config_payload["columns"]["gap_threshold"] = 30
        write_json(working["config"], config_payload)
        gap_run = _run(
            working, snapshot=incremental_snapshot, allow_config_change=True, truth=True
        )
        gap_selective = all(
            {"detect", "roles"}.issubset(item["reused_stages"])
            and set(item["recomputed_stages"]) == {"columns", "order"}
            and not item["detection_performed"]
            for item in gap_run.report["reuse_recompute"]
        )
        check("column_gap_recomputes_columns_and_order_only", gap_selective)

        config_payload["reading"]["rule"] = "region_first"
        write_json(working["config"], config_payload)
        rule_run = _run(
            working, snapshot=incremental_snapshot, allow_config_change=True, truth=True
        )
        rule_selective = all(
            {"detect", "roles", "columns"}.issubset(item["reused_stages"])
            and item["recomputed_stages"] == ["order"]
            and not item["detection_performed"]
            for item in rule_run.report["reuse_recompute"]
        )
        check(
            "rule_switch_recomputes_order_only",
            rule_selective
            and rule_run.report["summary"]["truth_validation"]["all_passed"],
            truth=rule_run.report["summary"]["truth_validation"],
        )

        replacement = PROJECT_ROOT / "samples" / "replacements" / "page_alt_mixed.png"
        (working["images"] / "page_0002.png").write_bytes(replacement.read_bytes())
        replace_run = _run(
            working, snapshot=incremental_snapshot, allow_config_change=True
        )
        pages_by_name = {
            item["page"]: item for item in replace_run.report["reuse_recompute"]
        }
        replacement_selective = (
            set(pages_by_name["page_0002.png"]["recomputed_stages"])
            == {"detect", "roles", "columns", "order"}
            and all(
                set(item["reused_stages"]) == {"detect", "roles", "columns", "order"}
                for name, item in pages_by_name.items()
                if name != "page_0002.png"
            )
        )
        check("single_page_replacement_invalidates_one_page", replacement_selective)

        add_source = PROJECT_ROOT / "samples" / "replacements" / "page_alt_double.png"
        (working["images"] / "page_9999.png").write_bytes(add_source.read_bytes())
        add_run = _run(working, snapshot=incremental_snapshot, allow_config_change=True)
        check(
            "page_add_processes_new_page",
            "page_9999.png" in add_run.report["concurrency"]["page_result_order"],
        )
        (working["images"] / "page_0001.png").unlink()
        removed_run = _run(
            working, snapshot=incremental_snapshot, allow_config_change=True
        )
        check(
            "page_remove_drops_page_from_results",
            "page_0001.png" not in removed_run.report["concurrency"]["page_result_order"],
        )
        final_metadata = read_json(working["metadata"])
        final_metadata["expected_reading_order_entries"] = removed_run.report["summary"][
            "reading_order_entries"
        ]
        write_json(working["metadata"], final_metadata)
        incremental_final = _run(
            working, snapshot=incremental_snapshot, allow_config_change=True
        )

        clean_root = root / "clean"
        clean_workspace = {
            "root": clean_root,
            "images": clean_root / "images",
            "config": clean_root / "config.json",
            "metadata": clean_root / "metadata.json",
            "truth": clean_root / "layout_truth.json",
        }
        clean_workspace["images"].mkdir(parents=True)
        for source in sorted(working["images"].glob("*.png")):
            (clean_workspace["images"] / source.name).write_bytes(source.read_bytes())
        write_json(clean_workspace["config"], read_json(working["config"]))
        write_json(clean_workspace["metadata"], read_json(working["metadata"]))
        full_final = _run(
            clean_workspace,
            snapshot=clean_root / "full-snapshot",
            full_rebuild=True,
        )
        equivalent = canonical_page_core(incremental_final.report) == canonical_page_core(
            full_final.report
        )
        check(
            "incremental_equals_clean_full_rebuild",
            equivalent
            and incremental_final.exit_code == 0
            and full_final.exit_code == 0
            and incremental_final.report["summary"]["reading_order_entries"]
            == incremental_final.report["summary"]["expected_reading_order_entries"],
            compared_pages=len(incremental_final.report["pages"]),
        )
        details["final_incremental"] = {
            "summary": incremental_final.report["summary"],
            "batch": incremental_final.report["adaptive_decision"]["budget_impact"],
        }
        _write_json_file(reports_dir / "incremental_final_report.json", incremental_final.report)
        _write_json_file(reports_dir / "full_rebuild_final_report.json", full_final.report)

        concurrency_root = root / "concurrency"
        concurrency_workspace = _copy_workspace(generated, concurrency_root / "working")
        (concurrency_workspace["images"] / "page_0000_bad.png").write_bytes(b"invalid png")
        (concurrency_workspace["images"] / "page_9999_bad.png").write_bytes(b"also invalid")
        base_config = read_json(concurrency_workspace["config"])
        serial_config = copy.deepcopy(base_config)
        serial_config["runtime"]["thread_pool_workers"] = 1
        parallel_config = copy.deepcopy(base_config)
        parallel_config["runtime"]["thread_pool_workers"] = 4
        write_json(concurrency_root / "serial.json", serial_config)
        write_json(concurrency_root / "parallel.json", parallel_config)
        serial_workspace = dict(concurrency_workspace)
        serial_workspace["config"] = concurrency_root / "serial.json"
        parallel_workspace = dict(concurrency_workspace)
        parallel_workspace["config"] = concurrency_root / "parallel.json"
        serial = _run(
            serial_workspace, snapshot=concurrency_root / "serial-snapshot", full_rebuild=True
        )
        parallel = _run(
            parallel_workspace,
            snapshot=concurrency_root / "parallel-snapshot",
            full_rebuild=True,
        )
        concurrency_equal = canonical_pages(serial.report) == canonical_pages(parallel.report)
        check(
            "serial_parallel_identical_with_failures",
            concurrency_equal
            and serial.report["failures"] == parallel.report["failures"]
            and serial.exit_code == parallel.exit_code == 4,
            failure_order=[item["page"] for item in serial.report["failures"]],
            business_codes=serial.report["business_codes"],
        )
        details["concurrency"] = {
            "failures": serial.report["failures"],
            "business_codes": serial.report["business_codes"],
            "page_order_serial": serial.report["concurrency"]["page_result_order"],
            "page_order_parallel": parallel.report["concurrency"]["page_result_order"],
        }

        invalid_config = read_json(working["config"])
        invalid_config["analysis"]["binary_threshold"] = 255
        config_error_raised = False
        try:
            LayoutConfig.from_dict(invalid_config)
        except ConfigError as exc:
            config_error_raised = exc.code == "E_CONFIG_INVALID"
        check("oversized_threshold_returns_config_invalid", config_error_raised)

        graceful_root = root / "graceful"
        graceful_workspace = _copy_workspace(generated, graceful_root / "working")
        graceful_config = read_json(graceful_workspace["config"])
        graceful_config["runtime"]["thread_pool_workers"] = 2
        graceful_config["runtime"]["snapshot_commit_interval"] = 1
        write_json(graceful_workspace["config"], graceful_config)
        stop_event = threading.Event()

        def stop_after_first_chunk(engine, processed_count) -> None:  # noqa: ANN001, ARG001
            if processed_count >= 2:
                stop_event.set()

        graceful_snapshot = graceful_root / "snapshot"
        interrupted = _run(
            graceful_workspace,
            snapshot=graceful_snapshot,
            stop_event=stop_event,
            callback=stop_after_first_chunk,
        )
        interrupted_ok = (
            interrupted.exit_code == 0
            and interrupted.report["run"]["status"] == "interrupted"
            and (graceful_snapshot / "manifest.json").exists()
        )
        resumed = _run(graceful_workspace, snapshot=graceful_snapshot)
        check(
            "graceful_stop_commits_and_resumes",
            interrupted_ok
            and resumed.exit_code == 0
            and resumed.report["summary"]["pages_processed"] == 16,
            interrupted_pages=interrupted.report["summary"]["pages_processed"],
        )

        interaction_root = root / "interaction"
        interaction_workspace = _copy_workspace(generated, interaction_root / "working")
        session = InteractiveSession(
            images_dir=interaction_workspace["images"],
            config_path=interaction_workspace["config"],
            metadata_path=interaction_workspace["metadata"],
            snapshot_dir=interaction_root / "snapshot",
            report_path=interaction_root / "report.json",
            truth_path=interaction_workspace["truth"],
        )
        session.execute("threshold 120")
        session.execute("rule region_first")
        interaction_report = session.last_report
        check(
            "interaction_latency_is_measured",
            isinstance(interaction_report, dict)
            and len(interaction_report["interaction"]["commands"]) == 2
            and all(
                "within_budget" in command
                for command in interaction_report["interaction"]["commands"]
            ),
            commands=interaction_report["interaction"]["commands"],
            all_within_budget=interaction_report["interaction"]["all_within_budget"],
        )

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    passed = all(item["passed"] for item in checks)
    acceptance = {
        "schema_version": 1,
        "all_passed": passed,
        "checks_passed": sum(1 for item in checks if item["passed"]),
        "checks_total": len(checks),
        "checks": checks,
        "details": details,
        "environment": {
            "python": sys.version.split()[0],
            "numpy": np.__version__,
            "platform": platform.platform(),
        },
        "elapsed_ms": round(elapsed_ms, 3),
    }
    _write_json_file(reports_dir / "acceptance_report.json", acceptance)
    print(
        json.dumps(
            {
                "all_passed": passed,
                "checks_passed": acceptance["checks_passed"],
                "checks_total": acceptance["checks_total"],
                "elapsed_ms": acceptance["elapsed_ms"],
                "report": str(reports_dir / "acceptance_report.json"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if passed else 1


def _write_json_file(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
