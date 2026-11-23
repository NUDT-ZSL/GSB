from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from .engine import Engine, record_external_error
from .errors import BusinessError
from .input_store import InputStore
from .models import InjectionSpec, JobSpec
from .report import render_workbench
from .sample import write_sample_files
from .selfcheck import run_selfcheck
from .snapshots import SnapshotStore
from .util import atomic_write_json, canonical_json_text, read_json, tail_lines
from .workbench import run_interactive
from .workspace import Workspace


def _emit(payload: Any, *, pretty: bool = True) -> None:
    if pretty:
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    else:
        text = canonical_json_text(payload)
    print(text)


def _root_from_args(args: argparse.Namespace) -> Path:
    value = (
        getattr(args, "workspace_sub", None)
        or getattr(args, "workspace", None)
        or os.environ.get("TD_WORKSPACE")
        or "."
    )
    return Path(value).resolve()


def _add_workspace(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace",
        dest="workspace_sub",
        default=None,
        help="workspace root (default: current directory or TD_WORKSPACE)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m transformer_diag",
        description="Deterministic Softmax/LayerNorm incremental execution and diagnostics workbench",
    )
    parser.add_argument("--workspace", default=None, help="workspace root")
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="initialize workspace directories and default config")
    _add_workspace(init)
    init.add_argument("--reset", action="store_true", help="clear managed state and recreate it")

    sample = sub.add_parser("sample", help="write deterministic sample JSONL batches")
    _add_workspace(sample)
    sample.add_argument("--out-dir", default=None, help="output directory for sample JSONL")

    run = sub.add_parser("run", help="merge a JSONL batch and execute incremental stages")
    _add_workspace(run)
    run.add_argument("--jobs", "--batch", dest="jobs", required=True, help="JSONL job batch")
    run.add_argument("--config", default=None, help="JSON config override")
    run.add_argument(
        "--inject",
        action="append",
        default=[],
        help="runtime injection STAGE[:CODE[:JOB_ID]]; repeatable",
    )
    run.add_argument("--degrade-policy", choices=("fail", "skip"), default=None)
    run.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    run.add_argument("--no-verify", action="store_true", help="skip independent full recompute")
    run.add_argument(
        "--hold-lock-seconds",
        type=float,
        default=0.0,
        help="hold the non-blocking commit lock for a bounded diagnostic window",
    )

    mutate = sub.add_parser("mutate-input", help="deterministically modify one input tensor")
    _add_workspace(mutate)
    mutate.add_argument("--job-id", required=True)
    mutate.add_argument("--flat-index", type=int, default=None)
    mutate.add_argument("--value", type=float, default=None)
    mutate.add_argument("--scale", type=float, default=None)

    inspect = sub.add_parser("inspect", help="inspect a job/stage cache key and product hash")
    _add_workspace(inspect)
    inspect.add_argument("--job-id", required=True)
    inspect.add_argument("--stage", required=True, choices=("validate_plan", "block_stats", "normalize", "output"))
    inspect.add_argument("--cache-key", default=None, help="inspect a raw cache key instead")

    snapshot = sub.add_parser("snapshot", help="inspect, verify, or atomically replace snapshots")
    _add_workspace(snapshot)
    snap_sub = snapshot.add_subparsers(dest="snapshot_command", required=True)
    snap_list = snap_sub.add_parser("list")
    _add_workspace(snap_list)
    snap_get = snap_sub.add_parser("get")
    _add_workspace(snap_get)
    snap_get.add_argument("--kind", required=True, choices=("plan", "tolerance", "stats"))
    snap_get.add_argument("--key", required=True)
    snap_put = snap_sub.add_parser("put")
    _add_workspace(snap_put)
    snap_put.add_argument("--kind", required=True, choices=("plan", "tolerance", "stats"))
    snap_put.add_argument("--key", required=True)
    snap_put.add_argument("--version", required=True, type=int)
    snap_put.add_argument("--content-file", required=True)
    snap_verify = snap_sub.add_parser("verify")
    _add_workspace(snap_verify)

    selfcheck = sub.add_parser("selfcheck", help="run cross-module consistency checks")
    _add_workspace(selfcheck)

    verify = sub.add_parser("verify", help="verify incremental/full consistency and all invariants")
    _add_workspace(verify)

    errors = sub.add_parser("errors", help="show recent business error codes")
    _add_workspace(errors)
    errors.add_argument("--limit", type=int, default=25)

    workbench = sub.add_parser("workbench", help="open the local terminal/HTML diagnostics workbench")
    _add_workspace(workbench)
    workbench.add_argument("--interactive", action="store_true", help="force the interactive prompt")
    workbench.add_argument("--no-render", action="store_true", help="do not regenerate HTML")

    reset = sub.add_parser("reset", help="clear cache, snapshots, outputs, and state")
    _add_workspace(reset)

    demo = sub.add_parser("demo", help="run a deterministic end-to-end acceptance scenario")
    _add_workspace(demo)
    return parser


def _run_summary(report: dict[str, Any]) -> dict[str, Any]:
    errors = report.get("errors", [])
    first_code = errors[0].get("code") if errors else None
    return {
        "ok": report.get("status") == "ok",
        "status": report.get("status"),
        "code": first_code,
        "run_id": report.get("run_id"),
        "report_path": report.get("report_path"),
        "workbench_html": report.get("workbench_html"),
        "mode": report.get("mode"),
        "affected_jobs": report.get("invalidation", {}).get("affected_jobs", []),
        "recomputed_jobs": report.get("invalidation", {}).get("recomputed_jobs", []),
        "reused_jobs": report.get("invalidation", {}).get("reused_jobs", []),
        "recomputed_nodes": report.get("invalidation", {}).get("recomputed_nodes", []),
        "reused_nodes": report.get("invalidation", {}).get("reused_nodes", []),
        "consistency": report.get("consistency", {}).get("all_byte_identical"),
        "errors": [
            {
                key: error.get(key)
                for key in (
                    "code",
                    "stage",
                    "job_id",
                    "field",
                    "message",
                    "upstream_code",
                    "category",
                )
                if error.get(key) is not None
            }
            for error in errors
        ],
        "degradation_chains": report.get("degradation_chains", []),
        "snapshots": report.get("snapshots", []),
        "recent_error_codes": report.get("recent_error_codes", [])[:10],
    }


def _cmd_run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    root = _root_from_args(args)
    injections = [InjectionSpec.parse(item) for item in args.inject]
    engine = Engine(
        root,
        config_path=args.config,
        injections=injections,
        degrade_policy=args.degrade_policy,
        mode=args.mode,
        verify_full=not args.no_verify,
        hold_lock_seconds=args.hold_lock_seconds,
    )
    report = engine.run(args.jobs)
    summary = _run_summary(report)
    if report.get("status") == "ok":
        return summary, 0
    errors = report.get("errors", [])
    if errors:
        code = errors[0].get("code", "E_UNATTRIBUTED")
        exit_code = {
            "E_STAGE_BUSY": 4,
            "E_INCONSISTENT": 5,
            "E_CACHE_CORRUPT": 6,
            "E_SNAPSHOT_ROLLBACK": 7,
        }.get(code, 2)
        return summary, exit_code
    return summary, 2


def _cmd_mutate(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.flat_index is None and args.scale is None:
        raise BusinessError(
            "E_INPUT_INVALID",
            "mutate-input requires --flat-index/--value or --scale",
            field="mutation",
            stage="validate_plan",
        )
    if args.flat_index is not None and args.value is None:
        raise BusinessError(
            "E_INPUT_INVALID",
            "--flat-index requires --value",
            field="value",
            stage="validate_plan",
        )
    root = _root_from_args(args)
    workspace = Workspace(root)
    workspace.initialize()
    raw = workspace.load_jobs_registry()
    if args.job_id not in raw:
        raise BusinessError(
            "E_JOB_INVALID",
            f"job {args.job_id} is not registered",
            job_id=args.job_id,
            field="job_id",
        )
    job = JobSpec.from_dict(raw[args.job_id])
    store = InputStore(workspace)
    if args.scale is not None:
        result = store.mutate_scale(job, args.scale)
    else:
        result = store.mutate_flat(job, args.flat_index, args.value)
    return {"ok": True, **result}, 0


def _cmd_inspect(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    root = _root_from_args(args)
    workspace = Workspace(root)
    workspace.initialize()
    if args.cache_key:
        from .cache import ContentAddressedCache

        cache = ContentAddressedCache(workspace)
        return {"ok": True, **cache.inspect(args.cache_key)}, 0
    nodes = read_json(workspace.paths.nodes, default={})
    node = nodes.get("nodes", {}).get(args.job_id, {}).get(args.stage)
    if not node:
        raise BusinessError(
            "E_JOB_INVALID",
            f"no node record for job={args.job_id}, stage={args.stage}",
            job_id=args.job_id,
            field="stage",
        )
    result = {
        "ok": True,
        "job_id": args.job_id,
        "stage": args.stage,
        "status": node.get("status"),
        "cache_key": node.get("cache_key"),
        "product_hash": node.get("content_hash"),
        "artifact_version": node.get("artifact_version"),
        "key_material": node.get("key_material"),
        "reason": node.get("reason"),
        "error_code": node.get("error_code"),
    }
    if node.get("cache_key"):
        from .cache import ContentAddressedCache

        cache = ContentAddressedCache(workspace)
        cached = cache.get(
            node["cache_key"],
            expected_job_id=args.job_id,
            expected_stage=args.stage,
        )
        if cached is not None:
            result["cache_entry"] = cached["entry"]
    return result, 0


def _cmd_snapshot(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    root = _root_from_args(args)
    workspace = Workspace(root)
    workspace.initialize()
    store = SnapshotStore(workspace)
    command = args.snapshot_command
    if command == "list":
        return {"ok": True, "snapshots": store.list()}, 0
    if command == "get":
        envelope = store.read(args.kind, args.key)
        if envelope is None:
            raise BusinessError(
                "E_JOB_INVALID",
                f"snapshot {args.kind}/{args.key} does not exist",
                field=f"snapshot.{args.kind}.{args.key}",
            )
        return {"ok": True, "snapshot": envelope}, 0
    if command == "put":
        content_path = Path(args.content_file)
        content = read_json(content_path, code="E_POLICY_INVALID")
        envelope = store.put(args.kind, args.key, args.version, content)
        return {
            "ok": True,
            "kind": envelope["kind"],
            "key": envelope["key"],
            "version": envelope["version"],
            "content_checksum": envelope["content_checksum"],
            "replacement": envelope["replacement"],
        }, 0
    if command == "verify":
        items = store.verify_all()
        return {"ok": True, "verified": len(items), "snapshots": items}, 0
    raise BusinessError("E_UNATTRIBUTED", f"unknown snapshot command {command}")


def _cmd_errors(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    workspace = Workspace(_root_from_args(args))
    workspace.initialize()
    records: list[dict[str, Any]] = []
    for line in tail_lines(workspace.paths.errors, max(1, args.limit)):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return {"ok": True, "errors": list(reversed(records))}, 0


def _cmd_workbench(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    root = _root_from_args(args)
    workspace = Workspace(root)
    workspace.initialize()
    report = read_json(workspace.paths.latest_report, default=None)
    if not isinstance(report, dict):
        raise BusinessError(
            "E_INCONSISTENT",
            "no report exists; run a job batch first",
            field=str(workspace.paths.latest_report),
            stage="workbench",
        )
    if not args.no_render:
        render_workbench(report, workspace.paths.html_report)
    if args.interactive or sys.stdin.isatty():
        run_interactive(workspace, force=args.interactive)
        return {
            "ok": True,
            "interactive": True,
            "html": str(workspace.paths.html_report),
        }, 0
    from .workbench import print_summary

    print_summary(workspace)
    return {
        "ok": True,
        "interactive": False,
        "html": str(workspace.paths.html_report),
        "report": str(workspace.paths.latest_report),
    }, 0


def _cmd_demo(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    root = _root_from_args(args)
    workspace = Workspace(root)
    workspace.initialize(reset=True)
    sample_dir = root / "samples"
    paths = write_sample_files(sample_dir)
    results: dict[str, Any] = {}

    first = Engine(root).run(paths["batch_1"])
    results["initial"] = _run_summary(first)
    second = Engine(root).run(paths["batch_1"])
    results["repeat"] = _run_summary(second)
    appended = Engine(root).run(paths["jobs_full"])
    results["append"] = _run_summary(appended)
    revised = Engine(root).run(paths["batch_2_revision"])
    results["revision"] = _run_summary(revised)

    workspace = Workspace(root)
    registry = workspace.load_jobs_registry()
    store = InputStore(workspace)
    target = JobSpec.from_dict(registry["layernorm_b2"])
    mutation = store.mutate_flat(target, 0, 0.125)
    mutated = Engine(root).run(paths["batch_2_revision"])
    results["input_mutation"] = {**_run_summary(mutated), "mutation": mutation}

    selfcheck = run_selfcheck(workspace)
    results["selfcheck"] = selfcheck
    results["acceptance"] = {
        "initial_all_recomputed": len(first["invalidation"]["recomputed_nodes"]) == 8,
        "repeat_all_reused": len(second["invalidation"]["recomputed_nodes"]) == 0,
        "append_only_new_jobs_affected": appended["invalidation"]["affected_jobs"]
        == ["layernorm_b2", "softmax_b2"],
        "revision_recomputed_only_revised_job": revised["invalidation"]["affected_jobs"]
        == ["softmax_b2"],
        "input_mutation_affected_only_target": mutated["invalidation"]["affected_jobs"]
        == ["layernorm_b2"],
        "incremental_full_byte_identical": all(
            result.get("consistency")
            for key, result in results.items()
            if key in {"initial", "repeat", "append", "revision", "input_mutation"}
        ),
        "selfcheck_ok": selfcheck["ok"],
    }
    atomic_write_json(root / "demo_result.json", results, pretty=True)
    ok = all(results["acceptance"].values())
    results["ok"] = ok
    results["demo_result"] = str(root / "demo_result.json")
    return results, 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    root = _root_from_args(args)
    try:
        if args.command == "init":
            workspace = Workspace(root)
            workspace.initialize(reset=args.reset)
            payload = {
                "ok": True,
                "workspace": str(root),
                "config": str(workspace.paths.config),
                "managed": str(workspace.paths.managed),
                "reset": bool(args.reset),
            }
            code = 0
        elif args.command == "sample":
            workspace = Workspace(root)
            workspace.initialize()
            out_dir = Path(args.out_dir) if args.out_dir else root / "samples"
            payload = {"ok": True, **write_sample_files(out_dir)}
            code = 0
        elif args.command == "run":
            payload, code = _cmd_run(args)
        elif args.command == "mutate-input":
            payload, code = _cmd_mutate(args)
        elif args.command == "inspect":
            payload, code = _cmd_inspect(args)
        elif args.command == "snapshot":
            payload, code = _cmd_snapshot(args)
        elif args.command in {"selfcheck", "verify"}:
            result = run_selfcheck(Workspace(root))
            payload = result
            code = 0 if result["ok"] else 5
        elif args.command == "errors":
            payload, code = _cmd_errors(args)
        elif args.command == "workbench":
            payload, code = _cmd_workbench(args)
        elif args.command == "reset":
            Workspace(root).initialize(reset=True)
            payload = {"ok": True, "workspace": str(root), "reset": True}
            code = 0
        elif args.command == "demo":
            payload, code = _cmd_demo(args)
        else:
            raise BusinessError("E_UNATTRIBUTED", f"unknown command {args.command}")
    except BusinessError as exc:
        record_external_error(root, exc)
        _emit(exc.to_dict(), pretty=not args.compact)
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001
        attributed = BusinessError(
            "E_UNATTRIBUTED",
            f"unattributed CLI failure: {type(exc).__name__}: {exc}",
            stage=getattr(args, "command", "cli"),
            field="cli",
            details={"traceback": traceback.format_exc(limit=12)},
        )
        record_external_error(root, attributed)
        _emit(attributed.to_dict(), pretty=not args.compact)
        return 2
    _emit(payload, pretty=not args.compact)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

