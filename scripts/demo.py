#!/usr/bin/env python3
"""Run the three deterministic acceptance scenarios and print a stable summary."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model_service_workbench.models import canonical_json  # noqa: E402
from model_service_workbench.workbench import Workbench  # noqa: E402


class Tee:
    def __init__(self, *streams: TextIO) -> None:
        self.streams = streams

    def write(self, value: str) -> int:
        for stream in self.streams:
            stream.write(value)
        return len(value)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def _normalized_decisions(minutes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for minute in sorted(minutes, key=lambda item: item["minute"]):
        normalized.append(
            {
                key: value
                for key, value in minute.items()
                if key not in ("committed_at", "is_stale")
            }
        )
    return normalized


def _summary(workbench: Workbench, run_id: str) -> list[str]:
    view = workbench.get_current_view()
    run = view["run"]
    lines = [
        f"run_id={run_id}",
        f"status={run['status']}",
        f"declaration_version={run['declaration_version']}",
        f"minutes={run['completed_minutes']}/{run['total_minutes']}",
    ]
    for minute in sorted(view["minutes"], key=lambda item: item["minute"]):
        service_parts = []
        for service in sorted(minute["services"], key=lambda item: item["service"]):
            reasons = ",".join(
                f"{reason['dimension']}:{reason['before_replicas']}->{reason['after_replicas']}"
                for reason in service["clip_reasons"]
            ) or "-"
            service_parts.append(
                f"{service['service']}[req={service['request_count']},"
                f"raw={service['unclamped_demand']},demand={service['demand_replicas']},"
                f"final={service['final_replicas']},reject={service['rejected_requests']},"
                f"clip={reasons}]"
            )
        lines.append(
            f"m={minute['minute']:02d} budget={minute['budget_used']}/{minute['budget_limit']} "
            f"requests={minute['requests_total']} accepted={minute['accepted_total']} "
            f"rejected={minute['rejected_total']} " + " ".join(service_parts)
        )
    return lines


def run_demo(state_dir: Path, output_path: Path) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path = state_dir / "ledger.sqlite"
    snapshot_dir = state_dir / "snapshots"
    baseline_path = ROOT / "samples" / "cluster.json"
    modified_path = ROOT / "samples" / "cluster_modified.json"

    with output_path.open("w", encoding="utf-8", newline="\n") as output_file:
        out = Tee(sys.stdout, output_file)
        print("scenario=1 name=baseline_full", file=out)
        workbench = Workbench(baseline_path, ledger_path, snapshot_dir)
        try:
            registration = workbench.load_declaration()
            print(
                "registration="
                + canonical_json(
                    {
                        "declaration_version": registration.declaration.declaration_version,
                        "fingerprint": registration.declaration.fingerprint,
                        "version_changed": registration.version_changed,
                    }
                ),
                file=out,
            )
            baseline = workbench.run_all()
            baseline_run = baseline["run_id"]
            print(
                "replay_baseline="
                + canonical_json(workbench.replay(baseline_run)),
                file=out,
            )
            for line in _summary(workbench, baseline_run):
                print(line, file=out)

            print("scenario=2 name=concurrency_change_rerun", file=out)
            registration2 = workbench.load_declaration(modified_path)
            print(
                "registration="
                + canonical_json(
                    {
                        "declaration_version": registration2.declaration.declaration_version,
                        "authored_version": registration2.declaration.authored_version,
                        "version_changed": registration2.version_changed,
                        "previous_version": registration2.previous_version,
                        "differences": list(registration2.differences),
                        "affected_services": list(registration2.affected_services),
                        "affected_minute_range": list(registration2.affected_minute_range)
                        if registration2.affected_minute_range is not None
                        else None,
                        "stale_run_ids": list(registration2.stale_run_ids),
                    }
                ),
                file=out,
            )
            old_run = workbench.ledger.get_run(baseline_run)
            print(
                "old_run_state="
                + canonical_json(
                    {
                        "run_id": baseline_run,
                        "is_current": bool(old_run["is_current"]),
                        "is_stale": bool(old_run["is_stale"]),
                    }
                ),
                file=out,
            )
            modified = workbench.run_all()
            modified_run = modified["run_id"]
            modified_minutes = workbench.ledger.get_decisions(modified_run)
            baseline_minutes = workbench.ledger.get_decisions(baseline_run)
            changed_decisions = (
                _normalized_decisions(baseline_minutes)
                != _normalized_decisions(modified_minutes)
            )
            print(f"decisions_changed_after_version={str(changed_decisions).lower()}", file=out)
            replay_modified = workbench.replay(modified_run)
            print("replay_modified=" + canonical_json(replay_modified), file=out)
            snapshot = workbench.save_snapshot(modified_run)
            print(
                "snapshot="
                + canonical_json(
                    {
                        "snapshot_id": snapshot["snapshot_id"],
                        "declaration_version": snapshot["declaration_version"],
                        "completed_minutes": snapshot["completed_minutes"],
                    }
                ),
                file=out,
            )
            for line in _summary(workbench, modified_run):
                print(line, file=out)

            print("scenario=3 name=snapshot_restore_replay", file=out)
            restore = workbench.restore_snapshot(snapshot["snapshot_id"])
            restored_run = restore["restored_run_id"]
            restored_minutes = workbench.ledger.get_decisions(restored_run)
            exact_match = _normalized_decisions(modified_minutes) == _normalized_decisions(
                restored_minutes
            )
            print(
                "restore="
                + canonical_json(
                    {
                        "snapshot_id": snapshot["snapshot_id"],
                        "restored_run_id": restored_run,
                        "restored_minutes": restore["restored_minutes"],
                        "decision_fields_exact_match": exact_match,
                        "replay_ok": restore["replay"]["ok"],
                        "minutes_replayed": restore["replay"]["minutes_replayed"],
                    }
                ),
                file=out,
            )
            replay_restored = workbench.replay(restored_run)
            print("replay_restored=" + canonical_json(replay_restored), file=out)
            for line in _summary(workbench, restored_run):
                print(line, file=out)

            if not exact_match or not replay_restored["ok"]:
                print("result=FAILED", file=out)
                return 1
            print("result=OK", file=out)
            print(
                "verified=baseline_full,version_invalidation,version_rerun,"
                "budget_clipping,snapshot_restore,field_by_field_replay",
                file=out,
            )
            return 0
        finally:
            workbench.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deterministic workbench demo")
    parser.add_argument("--state-dir", default=str(ROOT / "artifacts" / "demo_state"))
    parser.add_argument(
        "--output",
        default=str(ROOT / "artifacts" / "demo_output" / "demo_report.txt"),
    )
    parser.add_argument(
        "--keep-state",
        action="store_true",
        help="do not remove the previous demo SQLite ledger and snapshots",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state_dir = Path(args.state_dir).resolve()
    if state_dir.exists() and not args.keep_state:
        shutil.rmtree(state_dir)
    return run_demo(state_dir, Path(args.output).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
