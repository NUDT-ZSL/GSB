from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .arrayio import BlobStore
from .config import EffectiveConfig
from .db import LedgerDB
from .pipeline import compute_task
from .snapshots import SnapshotStore
from .task import TaskSpec


def _check(name: str, ok: bool, **details: Any) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), **details}


def run_selfcheck(
    *,
    db_path: str | Path,
    state_dir: str | Path,
    config: EffectiveConfig,
) -> dict[str, Any]:
    db = LedgerDB(db_path)
    snapshots = SnapshotStore(state_dir)
    blobs = BlobStore(Path(state_dir) / "cache")
    checks: list[dict[str, Any]] = []
    try:
        task_payloads = db.task_payloads()
        task_by_id = {
            task_id: TaskSpec.from_dict(payload)
            for task_id, payload in task_payloads.items()
        }
        accepted = db.accepted_task_ids()
        all_tasks = db.task_set()

        stage_sets = db.stage_task_sets()
        for stage in ("condition", "noise", "denoise", "rgb"):
            actual = stage_sets.get(stage, set())
            checks.append(
                _check(
                    f"stage_set:{stage}",
                    actual == accepted,
                    expected=sorted(accepted),
                    actual=sorted(actual),
                )
            )

        ledger_rows = db.ledger_rows()
        ledger_ids = {row["task_id"] for row in ledger_rows}
        checks.append(
            _check(
                "ledger_task_set",
                ledger_ids == accepted,
                expected=sorted(accepted),
                actual=sorted(ledger_ids),
            )
        )

        recompute_results: dict[str, str] = {}
        recompute_matches: dict[str, bool] = {}
        for row in ledger_rows:
            task = task_by_id[row["task_id"]]
            expected_input_hash = task.input_hash(config)
            independent = compute_task(task, config, blobs=None)
            recompute_results[row["task_id"]] = independent["ledger_hash"]
            matches = (
                independent["input_hash"] == row["input_hash"]
                and independent["ledger_hash"] == row["content_hash"]
            )
            recompute_matches[row["task_id"]] = matches
            checks.append(
                _check(
                    f"ledger_independent_recompute:{row['task_id']}",
                    matches and expected_input_hash == row["input_hash"],
                    expected_ledger_hash=row["content_hash"],
                    actual_ledger_hash=independent["ledger_hash"],
                    expected_input_hash=row["input_hash"],
                    actual_input_hash=independent["input_hash"],
                )
            )

        bad_artifacts: list[dict[str, Any]] = []
        for row in db.cache_rows():
            descriptor = {
                "artifact_path": row["artifact_path"],
                "content_hash": row["content_hash"],
                "storage": row["storage"],
            }
            if not blobs.validate_descriptor(descriptor):
                bad_artifacts.append(
                    {
                        "artifact_kind": row["artifact_kind"],
                        "cache_key": row["cache_key"],
                        "artifact_path": row["artifact_path"],
                        "content_hash": row["content_hash"],
                    }
                )
        checks.append(
            _check(
                "content_addressed_blobs",
                not bad_artifacts,
                invalid=bad_artifacts,
                checked=len(db.cache_rows()),
            )
        )

        latest_run = db.latest_run()
        if latest_run is None:
            checks.append(_check("latest_run_present", False))
        else:
            run_id = int(latest_run["run_id"])
            reuse_events = db.events(run_id, "reuse")
            recompute_events = db.events(run_id, "recompute")
            rejection_events = db.events(run_id, "rejected")
            failed_events = db.events(run_id, "recompute_failed")
            reused = {event["task_id"] for event in reuse_events}
            recomputed = {event["task_id"] for event in recompute_events}
            rejected = {event["task_id"] for event in rejection_events}
            failed = {event["task_id"] for event in failed_events}
            checks.append(
                _check(
                    "reuse_recompute_disjoint",
                    reused.isdisjoint(recomputed),
                    reused=sorted(reused),
                    recomputed=sorted(recomputed),
                )
            )
            summary = latest_run.get("summary", {})
            expected_union = set(summary.get("results", {}).get("reused", [])) | set(
                summary.get("results", {}).get("recomputed", [])
            )
            checks.append(
                _check(
                    "reuse_recompute_event_union",
                    reused | recomputed == expected_union,
                    from_events=sorted(reused | recomputed),
                    from_summary=sorted(expected_union),
                )
            )
            checks.append(
                _check(
                    "attempt_task_partition",
                    reused.isdisjoint(recomputed)
                    and reused.isdisjoint(rejected)
                    and reused.isdisjoint(failed)
                    and recomputed.isdisjoint(rejected)
                    and recomputed.isdisjoint(failed)
                    and rejected.isdisjoint(failed),
                    reused=sorted(reused),
                    recomputed=sorted(recomputed),
                    rejected=sorted(rejected),
                    failed=sorted(failed),
                )
            )
            reuse_ok = True
            for event in reuse_events:
                ledger = db.ledger_for(event["task_id"])
                if ledger is None or ledger["content_hash"] != event.get("ledger_hash"):
                    reuse_ok = False
                    break
            checks.append(_check("reuse_checkpoint_ledger_match", reuse_ok))
            recompute_verified = all(
                event.get("matches_previous", True) for event in recompute_events
            )
            checks.append(
                _check(
                    "recompute_previous_ledger_match",
                    recompute_verified,
                    events=[
                        {
                            "task_id": event["task_id"],
                            "previous_ledger_hash": event.get("previous_ledger_hash"),
                            "ledger_hash": event.get("ledger_hash"),
                            "matches_previous": event.get("matches_previous", True),
                        }
                        for event in recompute_events
                    ],
                )
            )

        snapshot_report = snapshots.verify()
        checks.append(
            _check(
                "snapshot_integrity",
                bool(snapshot_report.get("ok")),
                snapshot=snapshot_report,
            )
        )
        if snapshot_report.get("ok"):
            loaded = snapshots.load_all()
            manifest_tasks = {
                payload["task_id"] for payload in loaded["manifest"]["body"]["tasks"]
            }
            checks.append(
                _check(
                    "snapshot_manifest_task_set",
                    manifest_tasks == all_tasks,
                    expected=sorted(all_tasks),
                    actual=sorted(manifest_tasks),
                )
            )
            db_ledger = db.ledger_snapshot()
            checks.append(
                _check(
                    "snapshot_ledger_hash",
                    loaded["ledger"]["body"]["content_hash"]
                    == db_ledger["content_hash"],
                    snapshot_hash=loaded["ledger"]["body"]["content_hash"],
                    database_hash=db_ledger["content_hash"],
                )
            )
            db_cache = db.cache_snapshot()
            checks.append(
                _check(
                    "snapshot_cache_hash",
                    loaded["cache"]["body"]["content_hash"]
                    == db_cache["content_hash"],
                    snapshot_hash=loaded["cache"]["body"]["content_hash"],
                    database_hash=db_cache["content_hash"],
                )
            )
            db_versions = {
                row["domain"]: {
                    "version": row["version"],
                    "checksum": row["checksum"],
                }
                for row in db.snapshot_version_rows()
            }
            checks.append(
                _check(
                    "snapshot_version_ledger",
                    all(
                        db_versions.get(domain, {}).get("version")
                        == int(envelope["version"])
                        and db_versions.get(domain, {}).get("checksum")
                        == envelope["checksum"]
                        for domain, envelope in loaded.items()
                    ),
                    database=db_versions,
                )
            )

        return {
            "ok": all(check["ok"] for check in checks),
            "checks": checks,
            "summary": {
                "accepted_tasks": sorted(accepted),
                "all_tasks": sorted(all_tasks),
                "ledger_entries": len(ledger_rows),
                "independent_ledger_hashes": recompute_results,
                "snapshot": snapshot_report,
            },
        }
    finally:
        db.close()


def write_selfcheck_report(report: dict[str, Any], path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

