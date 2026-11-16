"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import infer_diagnostic_paths, load_config, read_raw_config
from .errors import (
    E_CONFIG_INVALID,
    E_SNAPSHOT_VERSION_CONFLICT,
    E_STAGE_FAILURE,
)
from .pipeline import build_stage_specs
from .report import build_report, render_dependency_graph, write_report
from .workbench import Workbench


REPORT_ERROR_CODES = {
    E_CONFIG_INVALID,
    E_SNAPSHOT_VERSION_CONFLICT,
    E_STAGE_FAILURE,
}


def _error_entry(
    *,
    config_path: Path,
    index: int,
    error: Any,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = raw or {}
    specs = []
    try:
        config = load_config(config_path)
        specs = build_stage_specs(config)
    except Exception:
        config = None
    pipeline = [spec.stage_id for spec in specs]
    details = error.details if isinstance(error.details, dict) else {}
    reused_stages = list(details.get("reused_stages", []))
    invalidated_stages = list(details.get("invalidated_stages", pipeline))
    completed_stages = list(details.get("completed_stages", []))
    if not reused_stages and not invalidated_stages:
        invalidated_stages = list(pipeline)
    statuses = {stage_id: "invalidated" for stage_id in invalidated_stages}
    for stage_id in reused_stages:
        statuses[stage_id] = "reused"
    for stage_id in completed_stages:
        if stage_id in statuses and statuses[stage_id] != "reused":
            statuses[stage_id] = "recomputed"
    if error.stage in statuses:
        statuses[error.stage] = "failed"
    elif error.stage is not None:
        statuses[error.stage] = "failed"
    return {
        "edit_index": index,
        "config_path": str(config_path),
        "snapshot_version": (
            config.snapshot_version if config is not None else raw.get("snapshot_version")
        ),
        "config_hash": config.config_hash if config is not None else None,
        "pipeline": pipeline,
        "dependencies": (
            {spec.stage_id: list(spec.dependencies) for spec in specs} if specs else {}
        ),
        "invalidated_stages": invalidated_stages,
        "reused_stages": reused_stages,
        "recompute_stages": invalidated_stages,
        "hit_stages": reused_stages,
        "stage_status": statuses,
        "stage_timings_ms": {stage_id: 0.0 for stage_id in pipeline},
        "stage_compute_times_ms": {stage_id: 0.0 for stage_id in pipeline},
        "total_elapsed_ms": 0.0,
        "completed_stages": completed_stages,
        "snapshot_published": False,
        "snapshot_path": None,
        "final_pixel_diff": {
            "available": False,
            "identical": None,
            "reason": "run_failed",
        },
        "final_image": None,
        "final_image_pixel_fingerprint": None,
        "status": "error",
        "business_code": error.code,
        "error": error.as_dict(),
    }


def _write_error_artifacts(
    *,
    config_path: Path,
    error: Any,
    entries: list[dict[str, Any]],
    report_override: str | None,
    graph_override: str | None,
    index: int,
) -> None:
    raw: dict[str, Any] = {}
    try:
        raw = read_raw_config(config_path)
    except Exception:
        pass
    report_path, graph_path = infer_diagnostic_paths(
        config_path,
        report_override=report_override,
        graph_override=graph_override,
    )
    error_entry = _error_entry(
        config_path=config_path, index=index, error=error, raw=raw
    )
    all_entries = [*entries, error_entry]
    last = all_entries[-1]
    report = build_report(
        status="error",
        business_code=error.code,
        edits=all_entries,
        errors=[error.as_dict()],
        snapshot_version=last.get("snapshot_version"),
        config_hash=last.get("config_hash"),
        pipeline=last.get("pipeline", []),
        dependencies=last.get("dependencies", {}),
        snapshot_path=last.get("snapshot_path"),
        final_image=None,
        global_content_fingerprint=last.get("global_content_fingerprint"),
    )
    try:
        write_report(report_path, report)
    except OSError as exc:
        print(
            f"E_REPORT_WRITE_FAILED: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
    if graph_path is not None and last.get("pipeline"):
        try:
            config = load_config(config_path)
            specs = build_stage_specs(config)
            render_dependency_graph(
                specs,
                {
                    stage_id: str(last.get("stage_status", {}).get(stage_id, "invalidated"))
                    for stage_id in last["pipeline"]
                },
                {
                    stage_id: float(last.get("stage_timings_ms", {}).get(stage_id, 0.0))
                    for stage_id in last["pipeline"]
                },
                graph_path,
                snapshot_version=last.get("snapshot_version"),
                title_suffix="error",
            )
        except Exception:
            pass


def _aggregate_report(
    entries: list[dict[str, Any]],
    *,
    status: str,
) -> dict[str, Any]:
    last = entries[-1] if entries else {}
    return build_report(
        status=status,
        business_code=None,
        edits=entries,
        errors=[],
        snapshot_version=last.get("snapshot_version"),
        config_hash=last.get("config_hash"),
        pipeline=last.get("pipeline", []),
        dependencies=last.get("dependencies", {}),
        snapshot_path=last.get("snapshot_path"),
        final_image=last.get("final_image"),
        global_content_fingerprint=last.get("global_content_fingerprint"),
    )


def _render_command(args: argparse.Namespace) -> int:
    entries: list[dict[str, Any]] = []
    report_paths: list[Path] = []
    graph_targets: list[Path] = []
    overall_status = "ok"

    for index, config_value in enumerate(args.config):
        config_path = Path(config_value).resolve()
        try:
            config = load_config(config_path)
        except Exception as exc:
            error = exc if hasattr(exc, "code") else None
            if error is None:
                from .errors import WorkbenchError

                error = WorkbenchError(E_CONFIG_INVALID, f"{type(exc).__name__}: {exc}")
            print(f"{error.code}: {error.message}", file=sys.stderr)
            _write_error_artifacts(
                config_path=config_path,
                error=error,
                entries=entries,
                report_override=args.report,
                graph_override=args.graph,
                index=index,
            )
            return 2

        workspace = Path(args.workspace).resolve() if args.workspace else config.workspace
        wb = Workbench(workspace)
        try:
            outcome = wb.run(config_path, write_diagnostics=False)
        except Exception as exc:
            error = exc if hasattr(exc, "code") else None
            if error is None:
                from .errors import WorkbenchError

                error = WorkbenchError(E_STAGE_FAILURE, f"{type(exc).__name__}: {exc}")
            print(f"{error.code}: {error.message}", file=sys.stderr)
            _write_error_artifacts(
                config_path=config_path,
                error=error,
                entries=entries,
                report_override=args.report,
                graph_override=args.graph,
                index=index,
            )
            return 3 if error.code == E_STAGE_FAILURE else 2

        outcome.entry["edit_index"] = index
        entries.append(outcome.entry)
        report_paths.append(config.report_json)
        graph_targets.append(config.graph_png)
        try:
            render_dependency_graph(
                build_stage_specs(config),
                outcome.entry["stage_status"],
                outcome.entry["stage_timings_ms"],
                config.graph_png if not args.graph else Path(args.graph).resolve(),
                snapshot_version=config.snapshot_version,
                title_suffix=outcome.status,
            )
        except OSError as exc:
            print(f"E_REPORT_WRITE_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

        if outcome.status == "interrupted":
            overall_status = "interrupted"
            break

    final_report = _aggregate_report(entries, status=overall_status)
    targets = [Path(args.report).resolve()] if args.report else report_paths
    if not targets:
        targets = [(Path.cwd() / "diagnostic.json").resolve()]
    try:
        for target in dict.fromkeys(targets):
            write_report(target, final_report)
        if args.graph and graph_targets:
            # A graph was already rendered to the override after the last edit.
            pass
    except OSError as exc:
        print(f"E_REPORT_WRITE_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    summary = {
        "status": overall_status,
        "business_code": None,
        "edits": len(entries),
        "snapshot_version": final_report.get("snapshot_version"),
        "report_paths": [str(path) for path in dict.fromkeys(targets)],
        "graph_paths": [str(path) for path in dict.fromkeys(graph_targets)],
        "final_image": final_report.get("final_image"),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def _query_command(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    try:
        config = load_config(config_path)
        workspace = Path(args.workspace).resolve() if args.workspace else config.workspace
        result = Workbench(workspace).query(config_path)
    except Exception as exc:
        error = exc if hasattr(exc, "code") else None
        if error is None:
            from .errors import WorkbenchError

            error = WorkbenchError(E_CONFIG_INVALID, f"{type(exc).__name__}: {exc}")
        print(f"{error.code}: {error.message}", file=sys.stderr)
        result = {
            "status": "error",
            "business_code": error.code,
            "error": error.as_dict(),
            "recompute_stages": [],
            "reuse_stages": [],
            "coverage_ok": False,
            "no_duplicates": True,
        }
        try:
            report_path, _ = infer_diagnostic_paths(config_path, report_override=args.report)
            report = build_report(
                status="error",
                business_code=error.code,
                edits=[],
                errors=[error.as_dict()],
            )
            write_report(report_path, report)
        except Exception:
            pass
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _snapshot_command(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    store_workspace = workspace
    try:
        from .snapshots import SnapshotStore

        store = SnapshotStore(store_workspace)
        current = store.load_current()
        snapshot_dirs = []
        if store.snapshots_dir.is_dir():
            snapshot_dirs = sorted(path.name for path in store.snapshots_dir.iterdir() if path.is_dir())
        payload = {
            "workspace": str(workspace),
            "current": None if current is None else {
                "snapshot_version": current.version,
                "config_hash": current.config_hash,
                "path": str(current.path),
                "complete": current.complete,
                "global_content_fingerprint": current.manifest.get(
                    "global_content_fingerprint"
                ),
                "pipeline": current.pipeline,
            },
            "snapshots": snapshot_dirs,
        }
    except Exception as exc:
        error = exc if hasattr(exc, "code") else None
        if error is None:
            from .errors import WorkbenchError

            error = WorkbenchError(E_SNAPSHOT_VERSION_CONFLICT, f"{type(exc).__name__}: {exc}")
        print(f"{error.code}: {error.message}", file=sys.stderr)
        print(json.dumps({"status": "error", "business_code": error.code, "error": error.as_dict()}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="imgdiff",
        description="Deterministic incremental diffusion workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    render = subparsers.add_parser("render", help="render one or more configs in sequence")
    render.add_argument("--config", action="append", required=True, help="config JSON path")
    render.add_argument("--workspace", help="override workspace snapshot root")
    render.add_argument("--report", help="override aggregate diagnostic JSON path")
    render.add_argument("--graph", help="override dependency graph PNG path")
    render.set_defaults(func=_render_command)

    query = subparsers.add_parser("query", help="show reuse/invalidation plan without rendering")
    query.add_argument("--config", required=True, help="config JSON path")
    query.add_argument("--workspace", help="override workspace snapshot root")
    query.add_argument("--report", help="diagnostic report path on query error")
    query.set_defaults(func=_query_command)

    snapshot = subparsers.add_parser("snapshot", help="show current snapshot and versions")
    snapshot.add_argument("--workspace", required=True, help="workspace snapshot root")
    snapshot.set_defaults(func=_snapshot_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
