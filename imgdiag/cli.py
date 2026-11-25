from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from .cache import atomic_write_json
from .reports import (
    baseline_report,
    budget_probe_report,
    invalid_code_report,
    parallel_report,
    revision_report,
)
from .revision import detect_stale_result
from .selfcheck import query_edit, self_check_workbench
from .tasks import load_jsonl, load_params
from .workbench import CheckpointedWorkbench
from .pipeline import prepare_bundle
from .hashing import canonical_hash


def write_json(path: str | None, payload: Any) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    if path:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
    else:
        print(text)


def _load_inputs(args: argparse.Namespace):
    return load_jsonl(args.samples), load_params(args.params)


def cmd_baseline(args: argparse.Namespace) -> int:
    tasks, params = _load_inputs(args)
    report = baseline_report(tasks, params)
    write_json(args.out, report)
    print(f"baseline: {report['byte_identical_block_full_count']}/{report['task_count']} byte-identical", file=sys.stderr)
    return 0 if report["all_blocks_match_float64"] else 2


def cmd_parallel(args: argparse.Namespace) -> int:
    tasks, params = _load_inputs(args)
    report = parallel_report(tasks, params, workers=args.workers, batch_size=args.batch_size)
    write_json(args.out, report)
    print(f"parallel: {report['result_count']} results, all_ok={report['all_ok']}", file=sys.stderr)
    return 0 if report["all_ok"] and report["scheduling_reproducible"] else 2


def cmd_report(args: argparse.Namespace) -> int:
    tasks, params = _load_inputs(args)
    report: dict[str, Any] = {
        "baseline": baseline_report(tasks, params),
        "parallel": parallel_report(tasks, params),
        "budget_adjudication": budget_probe_report(tasks, params),
    }
    if args.invalid_samples and os.path.exists(args.invalid_samples):
        report["business_code_probes"] = invalid_code_report(load_jsonl(args.invalid_samples), params)
    if args.old_revision and args.new_revision and os.path.exists(args.new_revision):
        report["revision_plan"] = revision_report(
            load_jsonl(args.old_revision), load_jsonl(args.new_revision)
        )
    report["business_codes"] = report["baseline"]["business_code_counts"]
    report["snapshot_version"] = report["baseline"]["snapshot_version"]
    write_json(args.out, report)
    return 0 if report["baseline"]["all_blocks_match_float64"] and report["parallel"]["all_ok"] else 2


def cmd_run(args: argparse.Namespace) -> int:
    tasks, params = _load_inputs(args)
    wb = CheckpointedWorkbench(args.workdir, params.get("snapshot_version", "snapshot-v1"))
    marker_fired = {"value": False}

    def checkpoint_hook(checkpoint: dict[str, Any]) -> None:
        target = args.ready_after_version
        if target and checkpoint["version"] >= target and not marker_fired["value"]:
            marker_fired["value"] = True
            if args.ready_marker:
                atomic_write_json(args.ready_marker, {
                    "pid": os.getpid(),
                    "workdir": os.path.abspath(args.workdir),
                    "checkpoint_version": checkpoint["version"],
                })
            time.sleep(max(0.0, float(args.pause_seconds)))

    result = wb.run_all_batches(
        tasks, params,
        batch_size=args.batch_size,
        worker_override=args.workers,
        hook=checkpoint_hook,
    )
    verification = self_check_workbench(args.workdir, tasks, params, replay_all_edits=False)
    write_json(args.report, {"run": result, "self_check": verification, "ledger_path": wb.ledger_path})
    print(f"checkpointed run complete: {len(result['completed_edit_ids'])} edits", file=sys.stderr)
    return 0 if verification["ok"] else 2


def cmd_verify(args: argparse.Namespace) -> int:
    tasks, params = _load_inputs(args)
    report = self_check_workbench(args.workdir, tasks, params, replay_all_edits=args.replay_all)
    write_json(args.out, report)
    print(f"self-check ok={report['ok']}", file=sys.stderr)
    return 0 if report["ok"] else 2


def cmd_query(args: argparse.Namespace) -> int:
    tasks, params = _load_inputs(args)
    tasks_by_id = {str(task["edit_id"]): task for task in tasks}
    if args.edit_id not in tasks_by_id:
        raise SystemExit(f"unknown edit_id {args.edit_id}")
    report = query_edit(args.edit_id, tasks_by_id, params, args.workdir)
    write_json(args.out, report)
    return 0 if report.get("replay_match") else 2


def cmd_inspect(args: argparse.Namespace) -> int:
    tasks, params = _load_inputs(args)
    task = next((item for item in tasks if str(item["edit_id"]) == args.edit_id), None)
    if task is None:
        raise SystemExit(f"unknown edit_id {args.edit_id}")
    bundle = prepare_bundle(task, params)
    regions = []
    for region in bundle["mask_model"]["regions"]:
        boundary = region["boundary_coords"]
        context = region["context_coords"]
        regions.append({
            "region_id": region["region_id"],
            "polygon": region["polygon"],
            "pixel_count": region["pixel_count"],
            "boundary_yx": boundary.tolist(),
            "boundary_xy": [[int(x), int(y)] for y, x in boundary],
            "context_yx": context.tolist(),
            "context_xy": [[int(x), int(y)] for y, x in context],
            "context_rgb_float64": region["context_rgb"].tolist(),
        })
    report = {
        "edit_id": args.edit_id,
        "image_hash": bundle["image_hash"],
        "mask_digest": bundle["mask_model"]["mask_digest"],
        "regions": regions,
    }
    write_json(args.out, report)
    return 0


def cmd_stale(args: argparse.Namespace) -> int:
    revised_tasks = load_jsonl(args.revised_samples)
    revised = next((item for item in revised_tasks if str(item["edit_id"]) == args.edit_id), None)
    if revised is None:
        raise SystemExit(f"unknown edit_id {args.edit_id} in revised samples")
    wb = CheckpointedWorkbench(args.workdir)
    entry = wb.ledger["tasks"].get(args.edit_id)
    if not entry:
        write_json(args.out, {"ok": False, "code": "E_STALE_RESULT", "edit_id": args.edit_id,
                              "stale_fields": ["missing_ledger_entry"]})
        return 0
    old_key = entry.get("current_cache_key")
    old_record = wb.cache.get_key(old_key) if old_key else None
    if old_record is None:
        old_record = {"cache_key": old_key, "key_inputs": {}, "artifact_hash": None}
    stale = detect_stale_result(
        args.edit_id,
        old_record,
        revised,
        "pending-current-key:" + canonical_hash(revised),
    )
    if stale is None:
        report = {"ok": True, "code": "OK", "edit_id": args.edit_id}
    else:
        report = stale
    write_json(args.out, report)
    return 0 if report.get("code") == "E_STALE_RESULT" else 2


def cmd_revision_plan(args: argparse.Namespace) -> int:
    report = revision_report(load_jsonl(args.old), load_jsonl(args.new))
    write_json(args.out, report)
    ok = report["sets_disjoint"] and report["union_covers_all_tasks"]
    return 0 if ok else 2


def cmd_revise(args: argparse.Namespace) -> int:
    params = load_params(args.params)
    old_tasks = load_jsonl(args.old)
    new_tasks = load_jsonl(args.new)
    plan = revision_report(old_tasks, new_tasks)
    wb = CheckpointedWorkbench(args.workdir, params.get("snapshot_version", "snapshot-v1"))
    prepared = wb.prepare_revision_reuse(new_tasks, params, plan)
    run_result = wb.run_all_batches(
        new_tasks, params, batch_size=args.batch_size, worker_override=args.workers
    )
    verification = self_check_workbench(args.workdir, new_tasks, params, replay_all_edits=False)
    report = {
        "revision_plan": plan,
        "prepared_links": prepared["linked"],
        "run": run_result,
        "self_check": verification,
        "revised_edit_sets": {
            edit_id: {
                "recomputed": wb.ledger["tasks"][edit_id]["recomputed_stages"],
                "reused": wb.ledger["tasks"][edit_id]["reused_stages"],
            }
            for edit_id in plan["recomputed_edit_ids"]
            if edit_id in wb.ledger["tasks"]
        },
    }
    write_json(args.out, report)
    return 0 if verification["ok"] else 2


def add_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--samples", default="samples/edit_tasks.jsonl")
    parser.add_argument("--params", default="samples/params.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="imgdiag", description="Deterministic image-editing diagnostics workbench")
    sub = parser.add_subparsers(dest="command", required=True)

    parser_baseline = sub.add_parser("baseline", help="run baseline diagnostics")
    add_inputs(parser_baseline)
    parser_baseline.add_argument("--out", default="artifacts/baseline_report.json")
    parser_baseline.set_defaults(func=cmd_baseline)

    parser_parallel = sub.add_parser("parallel", help="run process-pool scheduler")
    add_inputs(parser_parallel)
    parser_parallel.add_argument("--workers", type=int, default=None)
    parser_parallel.add_argument("--batch-size", type=int, default=None)
    parser_parallel.add_argument("--out", default="artifacts/parallel_report.json")
    parser_parallel.set_defaults(func=cmd_parallel)

    parser_report = sub.add_parser("report", help="generate comprehensive JSON report")
    add_inputs(parser_report)
    parser_report.add_argument("--invalid-samples", default="samples/invalid_cases.jsonl")
    parser_report.add_argument("--old-revision", default="samples/edit_tasks.jsonl")
    parser_report.add_argument("--new-revision", default="samples/edit_tasks_rev2_style.jsonl")
    parser_report.add_argument("--out", default="artifacts/diagnostic_report.json")
    parser_report.set_defaults(func=cmd_report)

    parser_run = sub.add_parser("run", help="checkpointed deterministic run")
    add_inputs(parser_run)
    parser_run.add_argument("--workdir", default="runtime/default")
    parser_run.add_argument("--batch-size", type=int, default=None)
    parser_run.add_argument("--workers", type=int, default=None)
    parser_run.add_argument("--report", default=None)
    parser_run.add_argument("--ready-after-version", type=int, default=None)
    parser_run.add_argument("--ready-marker", default=None)
    parser_run.add_argument("--pause-seconds", type=float, default=30.0)
    parser_run.set_defaults(func=cmd_run)

    parser_verify = sub.add_parser("verify", help="verify checkpoint ledger and replay")
    add_inputs(parser_verify)
    parser_verify.add_argument("--workdir", default="runtime/default")
    parser_verify.add_argument("--out", default="artifacts/self_check.json")
    parser_verify.add_argument("--replay-all", action="store_true")
    parser_verify.set_defaults(func=cmd_verify)

    parser_query = sub.add_parser("query", help="query and replay one edit")
    add_inputs(parser_query)
    parser_query.add_argument("edit_id")
    parser_query.add_argument("--workdir", default="runtime/default")
    parser_query.add_argument("--out", default=None)
    parser_query.set_defaults(func=cmd_query)

    parser_inspect = sub.add_parser("inspect", help="print mask boundary and context coordinates")
    add_inputs(parser_inspect)
    parser_inspect.add_argument("edit_id")
    parser_inspect.add_argument("--out", default=None)
    parser_inspect.set_defaults(func=cmd_inspect)

    parser_stale = sub.add_parser("stale", help="check an old ledger cache key against revised task input")
    parser_stale.add_argument("edit_id")
    parser_stale.add_argument("--workdir", default="runtime/default")
    parser_stale.add_argument("--revised-samples", default="samples/edit_tasks_rev2_style.jsonl")
    parser_stale.add_argument("--out", default=None)
    parser_stale.set_defaults(func=cmd_stale)

    parser_revision = sub.add_parser("revision-plan", help="compare revision JSONL files")
    parser_revision.add_argument("--old", required=True)
    parser_revision.add_argument("--new", required=True)
    parser_revision.add_argument("--out", default=None)
    parser_revision.set_defaults(func=cmd_revision_plan)

    parser_revise = sub.add_parser("revise", help="apply a revision with targeted cache invalidation")
    parser_revise.add_argument("--params", default="samples/params.json")
    parser_revise.add_argument("--old", default="samples/edit_tasks.jsonl")
    parser_revise.add_argument("--new", default="samples/edit_tasks_rev2_style.jsonl")
    parser_revise.add_argument("--workdir", default="runtime/revision-demo")
    parser_revise.add_argument("--batch-size", type=int, default=None)
    parser_revise.add_argument("--workers", type=int, default=None)
    parser_revise.add_argument("--out", default="artifacts/revision_report.json")
    parser_revise.set_defaults(func=cmd_revise)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
