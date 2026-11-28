"""End-to-end demonstration of the rhythm judging kernel.

Steps:
1. generate the deterministic demo project;
2. run the full judging pipeline and persist a snapshot;
3. apply a local event + beat-grid change incrementally;
4. restart from the snapshot and append a new event;
5. cross-check every judgement against the independent reference judge.

All artefacts are written under ``./work`` and only the Python standard
library is used.
"""

from __future__ import annotations

import json
import os

from rhythm_kernel.engine import RhythmEngine
from rhythm_kernel.generator import generate_project
from rhythm_kernel.incremental import recompute_incremental
from rhythm_kernel.persistence import load_snapshot
from rhythm_kernel.reference import compare_with_kernel

WORKDIR = os.path.join(os.path.dirname(__file__), "work")


def write_json(name, payload):
    path = os.path.join(WORKDIR, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    return path


def main():
    os.makedirs(WORKDIR, exist_ok=True)

    project = generate_project()
    project_path = write_json("demo_project.json", project)
    print(f"[1] deterministic project: {project_path} ({len(project['events'])} events)")

    engine = RhythmEngine(project)
    report = engine.run_full()
    comparison = compare_with_kernel(report, project)
    print(
        "[2] full judging: reference check",
        "OK" if comparison["ok"] else "FAILED",
        f"({comparison['mismatch_count']} mismatches)",
    )
    snapshot_path = write_json("snapshot.json", _snapshot_payload(report, project))

    changed_event = dict(project["events"][0])
    changed_event["click_ms"] += 42.5
    beta = engine.songs["song-beta"]
    engine.upsert_event(changed_event)
    engine.update_song_beats("song-beta", [beat + 10.0 for beat in beta["beats_ms"]])
    report, scope = recompute_incremental(
        engine,
        report,
        changed_event_ids=[changed_event["id"]],
        changed_song_ids=["song-beta"],
        reason="demo_local_tweak",
    )
    write_json("incremental_scope.json", scope)
    full = RhythmEngine(engine.project).run_full()
    core_equal = _core(report) == _core(full)
    print(
        f"[3] incremental: {scope['recomputed_event_count']} recomputed, "
        f"{scope['unchanged_event_count']} untouched; matches full run: {core_equal}"
    )

    snapshot = load_snapshot(snapshot_path)
    restarted_engine = RhythmEngine(snapshot["project"])
    previous = _report_from_snapshot(snapshot)
    new_event = dict(snapshot["project"]["events"][3])
    new_event["id"] = "evt-demo-new"
    new_event["click_ms"] += 300.0
    restarted_engine.upsert_event(new_event)
    resumed, resumed_scope = recompute_incremental(
        restarted_engine,
        previous,
        changed_event_ids=[new_event["id"]],
        reason="demo_resume",
    )
    write_json("resume_scope.json", resumed_scope)
    write_json("snapshot_v2.json", _snapshot_payload(resumed, restarted_engine.project))
    print(
        f"[4] resumed from snapshot; new event judged, revision "
        f"{resumed['state_revision']}, events {len(resumed['results'])}"
    )

    final_check = compare_with_kernel(resumed, restarted_engine.project)
    print(
        "[5] final reference cross-check",
        "OK" if final_check["ok"] else "FAILED",
        f"({final_check['mismatch_count']} mismatches)",
    )
    print("diagnostic view: python3 -m rhythm_kernel.cli serve --project", project_path)


def _snapshot_payload(report, project):
    from rhythm_kernel.persistence import build_snapshot

    return build_snapshot(report, project)


def _report_from_snapshot(snapshot):
    project = snapshot["project"]
    return {
        "algorithm_version": snapshot["algorithm_version"],
        "consistency_report": snapshot["consistency_report"],
        "event_order": list(snapshot["results"].keys()),
        "incremental_history": snapshot["incremental_history"],
        "legal_time_range_ms": [
            project["legal_time_range_ms"]["start_ms"],
            project["legal_time_range_ms"]["end_ms"],
        ],
        "policy": project["policy"],
        "project_id": project["project_id"],
        "results": [
            snapshot["results"][event_id] for event_id in snapshot["results"]
        ],
        "schema_version": snapshot["snapshot_version"],
        "state_revision": snapshot["state_revision"],
        "stats": snapshot["stats"],
        "trigger_sequence": snapshot["trigger_sequence"],
        "validation": snapshot["validation"],
    }


def _core(report):
    return {
        key: value
        for key, value in report.items()
        if key not in ("incremental_history", "state_revision")
    }


if __name__ == "__main__":
    main()
