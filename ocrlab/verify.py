"""In-process acceptance verification with a machine-readable result artifact."""

from __future__ import annotations

import json
import platform
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import PIL

from .errors import SimulatedCrash
from .pipeline import PipelineRunner
from .samples import generate_workspace, load_manifest
from .serialization import atomic_write_bytes
from .snapshots import SnapshotStore
from .templates import get_template_library


def _check(checks: list[dict[str, Any]], name: str, ok: bool, details: dict[str, Any]) -> None:
    checks.append({"name": name, "ok": bool(ok), "details": details})


def run_verification(output: str | Path) -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix="ocrlab-verify-"))
    checks: list[dict[str, Any]] = []
    observed: dict[str, Any] = {}
    try:
        data = root / "data"
        records = generate_workspace(data, page_count=6, base_seed=20261123, force=True)
        pages = load_manifest(data)
        baseline_a = PipelineRunner(data, root / "state-a").run([], reset=True)
        baseline_b = PipelineRunner(data, root / "state-b").run([], reset=True)
        _check(
            checks,
            "report_byte_reproducibility",
            baseline_a.report_bytes == baseline_b.report_bytes,
            {
                "report_checksum": baseline_a.report["report_checksum"],
                "bytes": len(baseline_a.report_bytes),
            },
        )
        _check(
            checks,
            "selfcheck",
            baseline_a.selfcheck["ok"],
            baseline_a.selfcheck,
        )
        _check(
            checks,
            "four_stage_page_sets",
            all(
                baseline_a.report["stage_page_sets"][str(stage)]
                == [record["page_id"] for record in records]
                for stage in range(1, 5)
            ),
            baseline_a.report["stage_page_sets"],
        )
        reference_ok = all(
            page["comparison"]["ok"]
            and page["comparison"]["max_coordinate_deviation"] == 0.0
            for page in baseline_a.report["pages"]
        )
        _check(
            checks,
            "chunked_vs_float64_reference",
            reference_ok,
            {
                "max_coordinate_deviation": max(
                    page["comparison"]["max_coordinate_deviation"]
                    for page in baseline_a.report["pages"]
                ),
                "max_score_delta": max(
                    page["comparison"]["max_score_delta"]
                    for page in baseline_a.report["pages"]
                ),
            },
        )
        recognized_exact = all(
            page["recognized_text"] == page["truth_text"]
            for page in baseline_a.report["pages"]
        )
        _check(
            checks,
            "recognized_text_matches_truth",
            recognized_exact,
            {"pages_checked": len(baseline_a.report["pages"])},
        )

        injections = [
            {
                "stage": "preprocess",
                "code": "E_PIXEL_OUT_OF_RANGE",
                "page_id": pages[0]["page_id"],
                "params": {"value": 300, "x": 5, "y": 6},
            },
            {
                "stage": "detect",
                "code": "E_INVALID_LINE_BOX",
                "page_id": pages[1]["page_id"],
                "line_no": 1,
                "params": {"box": [-8, -4, 99999, 12]},
            },
            {
                "stage": "recognize",
                "code": "E_TEMPLATE_SHAPE_MISMATCH",
                "page_id": pages[2]["page_id"],
                "line_no": 2,
                "params": {"shape": [3, 3], "expected": [21, 15]},
            },
            {
                "stage": "aggregate",
                "code": "E_INVALID_THRESHOLD",
                "page_id": pages[3]["page_id"],
                "params": {"threshold": 0},
            },
        ]
        injected_a = PipelineRunner(data, root / "state-inject-a").run(injections, reset=True)
        injected_b = PipelineRunner(data, root / "state-inject-b").run(injections, reset=True)
        expected_codes = {
            "E_PIXEL_OUT_OF_RANGE",
            "E_INVALID_LINE_BOX",
            "E_TEMPLATE_SHAPE_MISMATCH",
            "E_INVALID_THRESHOLD",
        }
        _check(
            checks,
            "injection_attribution_and_determinism",
            set(injected_a.report["business_codes"]) == expected_codes
            and injected_a.injection_log_bytes == injected_b.injection_log_bytes
            and injected_a.report_bytes == injected_b.report_bytes,
            {
                "business_codes": injected_a.report["business_codes"],
                "injection_log_checksum": injected_a.runtime["injection_log_checksum"],
                "injection_count": len(injected_a.injection_log),
            },
        )
        degradation_categories = sorted(
            {
                item["category"]
                for page in injected_a.report["pages"]
                for item in page["degradations"]
            }
        )
        _check(
            checks,
            "explicit_degradation_chain",
            {
                "truth_box_layout",
                "low_resolution_templates",
                "default_aggregation_threshold",
            }.issubset(set(degradation_categories)),
            {"categories": degradation_categories},
        )

        recovery_state = root / "state-recovery"
        recovery_baseline = PipelineRunner(data, recovery_state).run([], reset=True)
        crashed = False
        try:
            PipelineRunner(data, recovery_state).run([], reset=True, crash_after_stage=2)
        except SimulatedCrash:
            crashed = True
        recovered = PipelineRunner(data, recovery_state).run([])
        _check(
            checks,
            "forced_termination_recovery",
            crashed
            and recovered.report_bytes == recovery_baseline.report_bytes
            and recovered.runtime["resumed_from_stage"] == 3,
            {
                "crash_observed": crashed,
                "resumed_from_stage": recovered.runtime["resumed_from_stage"],
                "byte_identical": recovered.report_bytes == recovery_baseline.report_bytes,
                "reused_pages_by_stage": recovered.runtime["reused_pages_by_stage"],
                "recomputed_pages_by_stage": recovered.runtime["recomputed_pages_by_stage"],
            },
        )

        snapshot_identity_ok = all(
            metadata.get("config_checksum") == baseline_a.report["config"]["checksum"]
            and metadata.get("template_checksum") == baseline_a.report["template_library"]["checksum"]
            and metadata.get("dataset_checksum") == baseline_a.report["dataset_checksum"]
            for metadata in baseline_a.snapshot_metadata["stages"].values()
        )
        _check(
            checks,
            "snapshot_identity_and_versions",
            snapshot_identity_ok
            and [baseline_a.snapshot_metadata["stages"][str(stage)]["version"] for stage in range(1, 5)]
            == [1, 2, 3, 4],
            {
                "versions": {
                    stage: metadata["version"]
                    for stage, metadata in baseline_a.snapshot_metadata["stages"].items()
                },
                "checksums": {
                    stage: metadata["content_checksum"]
                    for stage, metadata in baseline_a.snapshot_metadata["stages"].items()
                },
            },
        )

        upgrade = PipelineRunner(
            data,
            root / "state-upgrade",
            library=get_template_library("glyph-v1"),
        )
        upgrade.run([], reset=True)
        upgraded = upgrade.upgrade_template_library(get_template_library("glyph-v2"))
        upgrade_context = upgraded.runtime["upgrade"]
        _check(
            checks,
            "selective_template_upgrade",
            upgraded.selfcheck["ok"]
            and not (
                set(upgrade_context["recomputed_pages"])
                & set(upgrade_context["reused_pages"])
            )
            and set(upgrade_context["affected_pages"]).issubset(
                set(upgrade_context["recomputed_pages"])
                | set(upgrade_context["reused_pages"])
            ),
            {
                "changed_characters": upgrade_context["changed_characters"],
                "affected_pages": upgrade_context["affected_pages"],
                "recomputed_pages": upgrade_context["recomputed_pages"],
                "reused_pages": upgrade_context["reused_pages"],
            },
        )

        store = SnapshotStore(root / "state-a")
        expected = {record["page_id"] for record in records}
        verified = store.verify_all(expected)
        _check(
            checks,
            "snapshot_checksum_verification",
            len(verified) == 4,
            {"verified": [item["stage_name"] for item in verified]},
        )

        observed = {
            "report_checksum": baseline_a.report["report_checksum"],
            "report_bytes": len(baseline_a.report_bytes),
            "injection_log_checksum": injected_a.runtime["injection_log_checksum"],
            "injection_log_bytes": len(injected_a.injection_log_bytes),
            "dataset_checksum": baseline_a.report["dataset_checksum"],
            "config_checksum": baseline_a.report["config"]["checksum"],
            "template_checksum": baseline_a.report["template_library"]["checksum"],
            "page_count": len(records),
            "line_count": sum(record["line_count"] for record in records),
        }
        result = {
            "schema_version": 1,
            "ok": all(check["ok"] for check in checks),
            "checks": checks,
            "observed": observed,
            "environment": {
                "python": platform.python_version(),
                "python_executable": sys.executable,
                "numpy": np.__version__,
                "pillow": PIL.__version__,
                "network_required": False,
            },
        }
    except Exception as exc:
        result = {
            "schema_version": 1,
            "ok": False,
            "checks": checks,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(
        output_path,
        (json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return result

