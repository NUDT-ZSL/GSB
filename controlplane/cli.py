"""Command-line interface for the deterministic control plane."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any, Sequence

from .canonical import pretty_json
from .engine import (
    compute_cluster_base,
    finalize_cluster_results,
    scheduler_inputs,
)
from .errors import ControlPlaneError
from .scheduler import ACTIVE_ABANDONED, NOT_STARTED, STARTED_FAILED, SUCCEEDED, run_scheduler
from .store import Store
from .validation import Sample, load_sample

DEFAULT_DB = "state/controlplane.sqlite3"
DEFAULT_SAMPLE_DIR = "samples"


def _add_common_options(parser: argparse.ArgumentParser, *, sub: bool = False) -> None:
    default = argparse.SUPPRESS if sub else None
    parser.add_argument(
        "--db",
        default=default if sub else DEFAULT_DB,
        help=f"SQLite state database (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--sample-dir",
        default=default if sub else DEFAULT_SAMPLE_DIR,
        help=f"deterministic sample directory (default: {DEFAULT_SAMPLE_DIR})",
    )
    parser.add_argument(
        "--output",
        default=default,
        help="also write the stable JSON report to this path",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="controlplane",
        description=(
            "Deterministic local multi-cluster configuration distribution "
            "control plane"
        ),
    )
    _add_common_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate and load a sample")
    _add_common_options(load_parser, sub=True)

    distribute_parser = subparsers.add_parser(
        "distribute", help="compute and commit a distribution round"
    )
    _add_common_options(distribute_parser, sub=True)
    distribute_parser.add_argument(
        "--cluster",
        action="append",
        default=[],
        help="only claim this cluster (repeatable); round scope remains complete",
    )
    distribute_parser.add_argument(
        "--new-round",
        action="store_true",
        help="start a new round even if this version already has a committed round",
    )
    distribute_parser.add_argument(
        "--interrupt-after",
        type=int,
        default=None,
        metavar="N",
        help="test hook: hard-exit after N newly completed cluster work items",
    )
    distribute_parser.add_argument(
        "--report-work",
        action="store_true",
        help="include per-invocation claimed/reused work counters",
    )

    explain_parser = subparsers.add_parser(
        "explain", help="explain the current effective configuration"
    )
    _add_common_options(explain_parser, sub=True)
    explain_parser.add_argument(
        "--cluster",
        action="append",
        default=[],
        help="only explain this cluster (repeatable)",
    )

    resume_parser = subparsers.add_parser(
        "resume", help="resume an interrupted pending round"
    )
    _add_common_options(resume_parser, sub=True)
    resume_parser.add_argument(
        "--cluster",
        action="append",
        default=[],
        help="only claim this cluster (repeatable)",
    )
    resume_parser.add_argument(
        "--interrupt-after",
        type=int,
        default=None,
        metavar="N",
        help="test hook: hard-exit after N newly completed cluster work items",
    )
    resume_parser.add_argument(
        "--report-work",
        action="store_true",
        help="include per-invocation claimed/reused work counters",
    )

    status_parser = subparsers.add_parser(
        "status", help="show stable current state"
    )
    _add_common_options(status_parser, sub=True)

    verify_parser = subparsers.add_parser(
        "verify", help="verify transactional and hash invariants"
    )
    _add_common_options(verify_parser, sub=True)
    verify_parser.add_argument(
        "--skip-sample-check",
        action="store_true",
        help="do not compare the database against the current sample version",
    )
    return parser


def _resolve_common(args: argparse.Namespace) -> tuple[str, str, str | None]:
    db_path = getattr(args, "db", None) or DEFAULT_DB
    sample_dir = getattr(args, "sample_dir", None) or DEFAULT_SAMPLE_DIR
    output = getattr(args, "output", None)
    return db_path, sample_dir, output


def _validate_selected_clusters(sample: Sample, selected: Sequence[str]) -> None:
    names = {cluster.name for cluster in sample.clusters}
    unknown = sorted(set(selected) - names)
    if unknown:
        raise ControlPlaneError(
            "E_UNKNOWN_CLUSTER",
            f"unknown cluster(s): {', '.join(unknown)}",
        )


def _dispatch_counts(results: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts = {
        SUCCEEDED: 0,
        STARTED_FAILED: 0,
        NOT_STARTED: 0,
        ACTIVE_ABANDONED: 0,
    }
    for result in results:
        status = result["dispatch"]["status"]
        counts[status] += 1
    return counts


def _round_output(
    command: str,
    store: Store,
    round_id: str,
    *,
    computed_this_run: int = 0,
    reused_this_run: int = 0,
    include_work_counters: bool = False,
) -> dict[str, Any]:
    round_info = store.get_round(round_id)
    if round_info is None:
        raise ControlPlaneError("E_ROUND_MISSING", f"round not found: {round_id}")
    progress = store.work_progress(round_id)
    payload: dict[str, Any] = {
        "ok": True,
        "command": command,
        "status": round_info.status,
        "round_id": round_info.round_id,
        "round_number": round_info.round_no,
        "version_id": round_info.version_id,
        "logical_clock": round_info.logical_clock,
        "work": {
            "expected": progress.expected,
            "completed": progress.complete,
            "pending": progress.pending,
            "claimed": progress.claimed,
        },
    }
    if include_work_counters:
        payload["computed_this_run"] = computed_this_run
        payload["reused_this_run"] = reused_this_run
    if round_info.status == "COMMITTED":
        results = store.round_results(round_id)
        payload["dispatch_counts"] = _dispatch_counts(results)
        payload["precision_sensitive_total"] = sum(
            int(result["precision_sensitive_count"]) for result in results
        )
    else:
        payload["missing_clusters"] = list(progress.missing_clusters)
    return payload


def _run_round(
    *,
    command: str,
    sample: Sample,
    store: Store,
    selected: Sequence[str],
    new_round: bool,
    allow_create: bool,
    interrupt_after: int | None,
    report_work: bool,
) -> dict[str, Any]:
    _validate_selected_clusters(sample, selected)
    if not allow_create:
        pending = store.pending_round_for_version(sample.version_id)
        if pending is None:
            current = store.current_round()
            if current is not None and current.version_id == sample.version_id:
                return _round_output(
                    command,
                    store,
                    current.round_id,
                    reused_this_run=store.work_progress(
                        current.round_id
                    ).complete,
                    include_work_counters=report_work,
                )
            raise ControlPlaneError(
                "E_NO_PENDING_ROUND",
                "no pending distribution round exists for this sample version",
            )
    round_info, _ = store.ensure_round(sample, new_round=new_round)
    if round_info.status == "COMMITTED":
        progress = store.work_progress(round_info.round_id)
        return _round_output(
            command,
            store,
            round_info.round_id,
            reused_this_run=progress.complete,
            include_work_counters=report_work,
        )

    if interrupt_after is not None and interrupt_after < 0:
        raise ControlPlaneError(
            "E_CLI", "--interrupt-after must be an integer >= 0"
        )
    if interrupt_after == 0:
        os._exit(75)

    initial_progress = store.work_progress(round_info.round_id)
    computed = 0
    while True:
        cluster = store.claim_next_work(
            round_info.round_id,
            only_clusters=selected or None,
        )
        if cluster is None:
            break
        result = compute_cluster_base(sample, cluster)
        store.complete_work(round_info.round_id, cluster, result)
        computed += 1
        if interrupt_after is not None and computed >= interrupt_after:
            os._exit(75)

    progress = store.work_progress(round_info.round_id)
    if progress.all_complete:
        def final_factory(
            base_results: dict[str, dict[str, Any]]
        ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
            final_results = finalize_cluster_results(sample, base_results)
            schedule = run_scheduler(sample, scheduler_inputs(final_results))
            return final_results, schedule

        store.adjudicate_and_commit(sample, round_info, final_factory)
    return _round_output(
        command,
        store,
        round_info.round_id,
        computed_this_run=computed,
        reused_this_run=initial_progress.complete,
        include_work_counters=report_work,
    )


def command_load(
    sample: Sample, store: Store
) -> dict[str, Any]:
    inserted = store.store_version(sample)
    return {
        "ok": True,
        "command": "load",
        "version_id": sample.version_id,
        "source_hash": sample.source_hash,
        "cluster_count": len(sample.clusters),
        "region_count": len(sample.regions),
        "region_waves": [list(wave) for wave in sample.region_waves],
        "inserted": inserted,
    }


def command_status(store: Store) -> dict[str, Any]:
    return store.status_report()


def command_explain(store: Store, clusters: Sequence[str]) -> dict[str, Any]:
    return store.explain_report(clusters or None)


def command_verify(
    store: Store, sample: Sample | None
) -> tuple[dict[str, Any], int]:
    report = store.verify_report(sample)
    if sample is not None and report.get("round_id"):
        report["checks"].append(_recomputed_check(sample, store, report["round_id"]))
        report["ok"] = all(check["ok"] for check in report["checks"])
    return report, 0 if report["ok"] else 3


def _recomputed_check(
    sample: Sample, store: Store, round_id: str
) -> dict[str, Any]:
    try:
        bases = {
            cluster.name: compute_cluster_base(sample, cluster.name)
            for cluster in sample.clusters
        }
        recomputed = finalize_cluster_results(sample, bases)
        schedule = run_scheduler(sample, scheduler_inputs(recomputed))
        staged = store.load_complete_base_results(round_id)
        actual_results = {
            result["cluster"]: result
            for result in store.round_results(round_id)
        }
        mismatched: list[str] = []
        if set(staged) != set(bases):
            mismatched.extend(sorted(set(bases) ^ set(staged)))
        for cluster in sorted(bases):
            if cluster in staged and staged[cluster] != bases[cluster]:
                mismatched.append(f"{cluster}:work")
            expected = recomputed.get(cluster)
            actual = actual_results.get(cluster)
            if expected is None or actual is None:
                mismatched.append(f"{cluster}:missing")
                continue
            actual_core = dict(actual)
            for runtime_field in (
                "dispatch",
                "round_id",
                "round_number",
                "logical_clock_watermark",
                "summary_hash",
            ):
                actual_core.pop(runtime_field, None)
            if actual_core != expected:
                mismatched.append(f"{cluster}:result")
            if actual.get("dispatch") != schedule["outcomes"].get(cluster):
                mismatched.append(f"{cluster}:dispatch")
        return {
            "name": "recomputed_results_match",
            "ok": not mismatched,
            **({"detail": ", ".join(sorted(set(mismatched)))} if mismatched else {}),
        }
    except Exception as exc:
        return {
            "name": "recomputed_results_match",
            "ok": False,
            "detail": str(exc),
        }


def _parse_interrupt_after(value: int | None) -> int | None:
    env_value = os.environ.get("CONTROLPLANE_INTERRUPT_AFTER")
    if value is not None:
        return value
    if env_value is None or env_value == "":
        return None
    try:
        parsed = int(env_value)
    except ValueError as exc:
        raise ControlPlaneError(
            "E_CLI", "CONTROLPLANE_INTERRUPT_AFTER must be an integer"
        ) from exc
    if parsed < 0:
        raise ControlPlaneError(
            "E_CLI", "CONTROLPLANE_INTERRUPT_AFTER must be >= 0"
        )
    return parsed


def run_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    db_path, sample_dir, _ = _resolve_common(args)
    command = args.command

    if command in {"load", "distribute", "resume"}:
        sample = load_sample(sample_dir)
        create_database = command != "resume"
        with Store(db_path, create=create_database) as store:
            if command == "load":
                return command_load(sample, store), 0
            if command == "distribute":
                report = _run_round(
                    command="distribute",
                    sample=sample,
                    store=store,
                    selected=args.cluster,
                    new_round=args.new_round,
                    allow_create=True,
                    interrupt_after=_parse_interrupt_after(
                        args.interrupt_after
                    ),
                    report_work=args.report_work,
                )
                return report, 0
            report = _run_round(
                command="resume",
                sample=sample,
                store=store,
                selected=args.cluster,
                new_round=False,
                allow_create=False,
                interrupt_after=_parse_interrupt_after(args.interrupt_after),
                report_work=args.report_work,
            )
            return report, 0

    if command == "status":
        with Store(db_path, create=False) as store:
            return command_status(store), 0

    if command == "explain":
        with Store(db_path, create=False) as store:
            return command_explain(store, args.cluster), 0

    if command == "verify":
        sample = None
        if not args.skip_sample_check:
            sample = load_sample(sample_dir)
        with Store(db_path, create=False) as store:
            return command_verify(store, sample)

    raise ControlPlaneError("E_CLI", f"unsupported command: {command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload, exit_code = run_command(args)
    except ControlPlaneError as exc:
        payload = {
            "ok": False,
            "command": args.command,
            "error": exc.as_dict(),
        }
        exit_code = 2
    except (sqlite3.Error, OSError) as exc:
        payload = {
            "ok": False,
            "command": args.command,
            "error": {
                "code": "E_IO",
                "message": str(exc),
            },
        }
        exit_code = 2
    except Exception as exc:  # pragma: no cover - last-resort CLI boundary
        if os.environ.get("CONTROLPLANE_DEBUG") == "1":
            traceback.print_exc()
        payload = {
            "ok": False,
            "command": args.command,
            "error": {
                "code": "E_INTERNAL",
                "message": str(exc),
            },
        }
        exit_code = 2

    text = pretty_json(payload) + "\n"
    output = getattr(args, "output", None)
    if output:
        try:
            path = Path(output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            payload = {
                "ok": False,
                "command": args.command,
                "error": {
                    "code": "E_OUTPUT_IO",
                    "message": str(exc),
                },
            }
            text = pretty_json(payload) + "\n"
            exit_code = 2
    sys.stdout.write(text)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
