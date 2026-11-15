#!/usr/bin/env python3
"""Run the local deterministic acceptance scenario and write a JSON report."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from level_solver.engine import LevelSolver, SolverInterrupted  # noqa: E402
from level_solver.errors import SolverError  # noqa: E402
from level_solver.model import canonical_json, digest_value, parse_spec  # noqa: E402
from level_solver.samples import (  # noqa: E402
    conflict_spec_payload,
    demo_spec_payload,
)
from level_solver.snapshots import SnapshotStore  # noqa: E402


def _solve(payload: dict[str, Any], snapshot_dir: Path) -> dict[str, Any]:
    return LevelSolver(parse_spec(payload), snapshot_dir).solve().as_dict()


def _compare(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    return {
        "grid_equal": first["grid"] == second["grid"],
        "rooms_equal": first["rooms"] == second["rooms"],
        "final_version_equal": first["version_id"] == second["version_id"],
        "candidate_attempts_equal": (
            first["candidate_attempts"] == second["candidate_attempts"]
        ),
        "constraint_application_count_equal": (
            first["constraint_application_count"]
            == second["constraint_application_count"]
        ),
    }


def run() -> dict[str, Any]:
    demo_payload = demo_spec_payload()
    demo_spec = parse_spec(demo_payload)
    with tempfile.TemporaryDirectory(prefix="level-solver-acceptance-") as temp:
        root = Path(temp)
        baseline = _solve(demo_payload, root / "baseline")

        interrupted: dict[str, Any]
        try:
            LevelSolver(
                demo_spec, root / "resume", interrupt_after=2
            ).solve()
            raise AssertionError("interrupt hook did not fire")
        except SolverInterrupted as exc:
            interrupted = {
                "exit_code": exc.exit_code,
                "applied_count": exc.applied_count,
                "marker_exists": exc.marker_path.exists(),
                "version_id": exc.version_id,
            }
        resumed = LevelSolver(demo_spec, root / "resume").solve().as_dict()

        damaged = _solve(demo_payload, root / "damaged")
        manifest = json.loads(
            (root / "damaged" / "manifest.json").read_text(encoding="utf-8")
        )
        latest_path = (
            root / "damaged" / "snapshots" / manifest["latest_snapshot"]
        )
        tampered_payload = json.loads(latest_path.read_text(encoding="utf-8"))
        tampered_payload["grid"][2][2] = 2
        latest_path.write_text(
            json.dumps(
                tampered_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        verify_code = None
        try:
            SnapshotStore(root / "damaged", demo_spec).verify()
        except SolverError as exc:
            verify_code = exc.code
        strict_code = None
        try:
            LevelSolver(
                demo_spec, root / "damaged", strict_digest=True
            ).solve()
        except SolverError as exc:
            strict_code = exc.code
        recovered = LevelSolver(demo_spec, root / "damaged").solve().as_dict()

        conflict = conflict_spec_payload()
        conflict_code = None
        conflict_details: dict[str, Any] = {}
        conflict_output_created = False
        conflict_output = root / "conflict-result.json"
        try:
            result = LevelSolver(
                parse_spec(conflict), root / "conflict"
            ).solve().as_dict()
            conflict_output.write_text(canonical_json(result), encoding="utf-8")
            conflict_output_created = True
        except SolverError as exc:
            conflict_code = exc.code
            conflict_details = exc.details

        checks = {
            "resume_matches_baseline": _compare(resumed, baseline),
            "tamper_fallback_matches_baseline": _compare(recovered, baseline),
            "tamper_verify_code": verify_code,
            "tamper_strict_code": strict_code,
            "tamper_warning_codes": sorted(
                {warning["code"] for warning in recovered["warnings"]}
            ),
            "conflict_code": conflict_code,
            "conflict_details": conflict_details,
            "conflict_output_created": conflict_output_created,
        }
        ok = (
            bool(interrupted["marker_exists"])
            and interrupted["exit_code"] == 75
            and all(
                value
                for value in checks["resume_matches_baseline"].values()
            )
            and all(
                value
                for value in checks["tamper_fallback_matches_baseline"].values()
            )
            and verify_code == "E_DIGEST"
            and strict_code == "E_DIGEST"
            and conflict_code == "E_CONFLICT"
            and not conflict_output_created
        )
        return {
            "ok": ok,
            "sample": {
                "spec_digest": demo_spec.spec_digest,
                "seed": demo_spec.seed,
                "constraint_ids": demo_spec.constraint_ids(),
                "baseline_version_id": baseline["version_id"],
                "baseline_grid_sha256": digest_value(baseline["grid"]),
                "baseline_rooms": baseline["rooms"],
                "baseline_snapshot_count": baseline["snapshot_count"],
                "baseline_candidate_attempts": baseline["candidate_attempts"],
                "baseline_rollback_count": baseline["rollback_count"],
            },
            "interrupt": interrupted,
            "resume": {
                "source": resumed["resume"]["source"],
                "start_index": resumed["resume"]["start_index"],
                "comparison": checks["resume_matches_baseline"],
                "snapshot_count": resumed["snapshot_count"],
            },
            "tamper": {
                "comparison": checks["tamper_fallback_matches_baseline"],
                "verify_code": verify_code,
                "strict_digest_code": strict_code,
                "warning_codes": checks["tamper_warning_codes"],
                "fallback_start_index": recovered["resume"]["start_index"],
            },
            "conflict": {
                "code": conflict_code,
                "unresolved_constraints": conflict_details.get(
                    "unresolved_constraints", []
                ),
                "conflict_pairs": conflict_details.get("conflict_pairs", []),
                "output_created": conflict_output_created,
            },
            "damaged_snapshot_example": {
                "latest_snapshot": manifest["latest_snapshot"],
                "original_version_id": damaged["version_id"],
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "docs" / "verification_report.json"),
        help="acceptance report path",
    )
    args = parser.parse_args()
    report = run()
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
