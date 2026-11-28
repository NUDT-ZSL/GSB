"""Command line entry points for the rhythm judging kernel."""

from __future__ import annotations

import argparse
import json

from .engine import RhythmEngine, load_project
from .generator import generate_project
from .incremental import recompute_incremental
from .persistence import load_snapshot, save_snapshot
from .reference import compare_with_kernel
from .server import serve


def _write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.write("\n")


def cmd_generate(args):
    project = generate_project(args.seed)
    _write_json(args.project_out, project)
    print(f"wrote deterministic project: {args.project_out}")


def cmd_judge(args):
    project = load_project(args.project)
    engine = RhythmEngine(project)
    report = engine.run_full()
    _write_json(args.results_out, report)
    _write_json(
        args.stats_out,
        {
            "algorithm_version": report["algorithm_version"],
            "consistency_report": report["consistency_report"],
            "project_id": report["project_id"],
            "schema_version": report["schema_version"],
            "state_revision": report["state_revision"],
            "stats": report["stats"],
        },
    )
    snapshot = save_snapshot(args.snapshot_out, report, project)
    comparison = compare_with_kernel(report, project)
    print(
        "judged events:",
        report["stats"]["overall"]["event_count"],
        "reference check ok:",
        comparison["ok"],
        "mismatches:",
        comparison["mismatch_count"],
    )
    print("snapshot integrity sections:", len(snapshot["integrity"]))


def cmd_incremental(args):
    project = load_project(args.project)
    engine = RhythmEngine(project)
    baseline = engine.run_full()

    with open(args.changes, "r", encoding="utf-8") as handle:
        changes = json.load(handle)

    changed_event_ids = []
    for event in changes.get("upsert_events", []):
        engine.upsert_event(event)
        changed_event_ids.append(event["id"])

    changed_song_ids = []
    for song_change in changes.get("song_beats", []):
        engine.update_song_beats(
            song_change["song_id"],
            song_change["beats_ms"],
            song_change.get("bpm"),
        )
        changed_song_ids.append(song_change["song_id"])

    report, scope = recompute_incremental(
        engine,
        baseline,
        changed_event_ids=changed_event_ids,
        removed_event_ids=changes.get("remove_event_ids", []),
        changed_song_ids=changed_song_ids,
        reason=changes.get("reason", "incremental_recompute"),
    )
    full = RhythmEngine(engine.project).run_full()
    _write_json(args.results_out, report)
    _write_json(args.scope_out, scope)
    print(
        "incremental scope:",
        scope["recomputed_event_count"],
        "recomputed,",
        scope["unchanged_event_count"],
        "untouched",
    )
    print("matches full recompute:", _reports_equal(report, full))


def cmd_resume(args):
    snapshot = load_snapshot(args.snapshot)
    project = snapshot["project"]
    engine = RhythmEngine(project)
    previous = {
        "algorithm_version": snapshot["algorithm_version"],
        "consistency_report": snapshot["consistency_report"],
        "event_order": list(snapshot["results"].keys()),
        "incremental_history": snapshot["incremental_history"],
        "legal_time_range_ms": project["legal_time_range_ms"],
        "policy": project["policy"],
        "project_id": project["project_id"],
        "results": [
            snapshot["results"][event_id]
            for event_id in snapshot["results"]
        ],
        "schema_version": snapshot["snapshot_version"],
        "state_revision": snapshot["state_revision"],
        "stats": snapshot["stats"],
        "trigger_sequence": snapshot["trigger_sequence"],
        "validation": snapshot["validation"],
    }
    with open(args.changes, "r", encoding="utf-8") as handle:
        changes = json.load(handle)
    changed_ids = []
    for event in changes.get("upsert_events", []):
        engine.upsert_event(event)
        changed_ids.append(event["id"])
    report, scope = recompute_incremental(
        engine, previous, changed_event_ids=changed_ids, reason="resume"
    )
    _write_json(args.results_out, report)
    _write_json(args.scope_out, scope)
    save_snapshot(args.snapshot_out, report, engine.project)
    print("resumed from snapshot; revision:", report["state_revision"])


def cmd_serve(args):
    if args.snapshot:
        serve(snapshot_path=args.snapshot, port=args.port)
    else:
        project = load_project(args.project) if args.project else generate_project()
        report = RhythmEngine(project).run_full()
        serve(project=project, report=report, port=args.port)


def _reports_equal(left, right):
    bookkeeping = {"incremental_history", "state_revision"}
    left_core = {key: value for key, value in left.items() if key not in bookkeeping}
    right_core = {
        key: value for key, value in right.items() if key not in bookkeeping
    }
    return json.dumps(left_core, sort_keys=True) == json.dumps(
        right_core, sort_keys=True
    )


def build_parser():
    parser = argparse.ArgumentParser(description="Rhythm judging kernel CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate deterministic project")
    gen.add_argument("--seed", type=int, default=20261128)
    gen.add_argument("--project-out", required=True)
    gen.set_defaults(func=cmd_generate)

    judge = sub.add_parser("judge", help="full judging run + snapshot")
    judge.add_argument("--project", required=True)
    judge.add_argument("--results-out", required=True)
    judge.add_argument("--stats-out", required=True)
    judge.add_argument("--snapshot-out", required=True)
    judge.set_defaults(func=cmd_judge)

    inc = sub.add_parser("incremental", help="local change incremental run")
    inc.add_argument("--project", required=True)
    inc.add_argument("--changes", required=True)
    inc.add_argument("--results-out", required=True)
    inc.add_argument("--scope-out", required=True)
    inc.set_defaults(func=cmd_incremental)

    resume = sub.add_parser("resume", help="resume from a snapshot")
    resume.add_argument("--snapshot", required=True)
    resume.add_argument("--changes", required=True)
    resume.add_argument("--results-out", required=True)
    resume.add_argument("--scope-out", required=True)
    resume.add_argument("--snapshot-out", required=True)
    resume.set_defaults(func=cmd_resume)

    server = sub.add_parser("serve", help="loopback diagnostic HTTP view")
    server.add_argument("--project")
    server.add_argument("--snapshot")
    server.add_argument("--port", type=int, default=8080)
    server.set_defaults(func=cmd_serve)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
