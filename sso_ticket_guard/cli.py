"""Interactive command-line interface for the SSO ticket guard."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .checkpoint import CheckpointManager, CheckpointView
from .constants import (
    EXIT_ARGUMENT,
    EXIT_OK,
    EXIT_PARTIAL,
    QUOTA_PROFILE_VERSION,
    SCHEMA_VERSION,
)
from .engine import RenewalEngine, build_summary
from .errors import BusinessError
from .io_utils import read_json, write_json_atomic
from .normalize import NormalizedDataset, load_dataset
from .revocation import revoke_all


def _dataset_report(dataset: NormalizedDataset) -> dict[str, Any]:
    return {
        "counts": {
            "platforms": len(dataset.platforms),
            "fingerprints": len(dataset.fingerprints),
            "tickets": len(dataset.tickets),
            "renewals": len(dataset.renewals),
            "revocations": len(dataset.revocations),
            "issues": len(dataset.issues),
        },
        "quota_profile_version": dataset.quota_profile_version,
        "rejections": [issue.as_dict() for issue in dataset.issues],
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    write_json_atomic(path, report)


def _load_manifest(samples_dir: Path) -> dict[str, Any] | None:
    manifest_path = samples_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    return read_json(manifest_path)


def _decision_counter(reports: list[dict[str, Any]]) -> dict[str, int]:
    counter = Counter(str(report["decision"]) for report in reports)
    return {
        "accept": counter.get("accept", 0),
        "reject": counter.get("reject", 0),
        "ambiguous": counter.get("ambiguous", 0),
    }


def _code_counter(reports: list[dict[str, Any]]) -> dict[str, int]:
    counter = Counter(
        str(report["code"]) for report in reports if report.get("code") is not None
    )
    return dict(sorted(counter.items()))


def command_import(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    dataset = load_dataset(args.samples)
    manifest = _load_manifest(args.samples)
    report = {
        "command": "import",
        "status": "partial" if dataset.issues else "ok",
        **_dataset_report(dataset),
        "sample_manifest": manifest,
        "canonical_fingerprints": [
            declaration.fingerprint.as_dict()
            for _, declaration in sorted(dataset.fingerprints.items())
        ],
    }
    _write_report(args.output, report)
    return report, EXIT_PARTIAL if dataset.issues else EXIT_OK


def _group_by_batch(dataset: NormalizedDataset) -> dict[int, list[Any]]:
    grouped: dict[int, list[Any]] = {}
    for request in dataset.renewal_order():
        grouped.setdefault(request.batch_id, []).append(request)
    return dict(sorted(grouped.items()))


def _renew_with_checkpoint(
    dataset: NormalizedDataset,
    engine: RenewalEngine,
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    manager = CheckpointManager(
        args.state_dir,
        schema_version=SCHEMA_VERSION,
        quota_version=dataset.quota_profile_version,
    )
    reports: list[dict[str, Any]] = []
    processed_batches: list[int] = []
    stopped_early = False
    with manager.session() as session:
        if args.fresh:
            session.reset()
        initial_view = session.load()
        view = initial_view
        events = list(view.events)
        state = view.state
        watermark = view.watermark
        resume_watermark = watermark
        batches = _group_by_batch(dataset)
        for batch_id, requests in batches.items():
            if batch_id <= watermark:
                continue
            batch_reports, batch_events, state = engine.adjudicate_all(requests, state)
            reports.extend(batch_reports)
            events.extend(batch_events)
            watermark = batch_id
            view = session.save(watermark=watermark, events=events, state=state)
            processed_batches.append(batch_id)
            if args.max_batches is not None and len(processed_batches) >= args.max_batches:
                remaining = any(
                    candidate > batch_id for candidate in batches
                )
                stopped_early = remaining
                break
        checkpoint = view.public_dict()
    processing = {
        "resume_watermark": resume_watermark,
        "processed_batches": processed_batches,
        "stopped_early": stopped_early,
        "load_status": initial_view.status,
        "load_diagnostics": initial_view.diagnostics,
        "checkpoint": checkpoint,
    }
    public_events = [
        {
            "request_id": event["request_id"],
            "decision": event["decision"],
            "code": event["code"],
        }
        for event in events
    ]
    checkpoint_summary = build_summary(
        watermark=watermark,
        events=events,
        state=state,
    )
    return reports, public_events, {**processing, "cumulative": checkpoint_summary}


def command_renew(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    dataset = load_dataset(args.samples)
    engine = RenewalEngine(dataset)
    checkpoint_info = None
    if args.state_dir is not None:
        reports, checkpoint_events, checkpoint_info = _renew_with_checkpoint(
            dataset,
            engine,
            args,
        )
    else:
        requests = dataset.renewal_order()
        reports, _, _ = engine.adjudicate_all(requests)
        checkpoint_events = []

    any_failure = bool(dataset.issues) or any(
        report["decision"] != "accept" for report in reports
    )
    report = {
        "command": "renew",
        "status": "partial" if any_failure else "ok",
        "quota_profile_version": dataset.quota_profile_version,
        "normalization_rejections": [issue.as_dict() for issue in dataset.issues],
        "summary": {
            "decisions": _decision_counter(reports),
            "codes": _code_counter(reports),
            "requests_processed": len(reports),
        },
        "adjudications": reports,
        "checkpoint": checkpoint_info,
        "checkpoint_events": checkpoint_events,
    }
    _write_report(args.output, report)
    return report, EXIT_PARTIAL if any_failure else EXIT_OK


def command_revoke(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    dataset = load_dataset(args.samples)
    renewal_events: list[dict[str, Any]] = []
    checkpoint_info = None
    if args.state_dir is not None:
        manager = CheckpointManager(
            args.state_dir,
            schema_version=SCHEMA_VERSION,
            quota_version=dataset.quota_profile_version,
        )
        view = manager.load_for_resume()
        renewal_events = view.events
        checkpoint_info = view.public_dict()
    revocations = revoke_all(dataset, renewal_events=renewal_events)
    any_failure = bool(dataset.issues) or any(
        item["decision"] != "accept" for item in revocations
    )
    report = {
        "command": "revoke",
        "status": "partial" if any_failure else "ok",
        "normalization_rejections": [issue.as_dict() for issue in dataset.issues],
        "revocations": revocations,
        "summary": {
            "requests": len(revocations),
            "accepted": sum(
                1 for item in revocations if item["decision"] == "accept"
            ),
            "rejected": sum(
                1 for item in revocations if item["decision"] == "reject"
            ),
        },
        "checkpoint": checkpoint_info,
    }
    _write_report(args.output, report)
    return report, EXIT_PARTIAL if any_failure else EXIT_OK


def _checkpoint_view_report(view: CheckpointView) -> dict[str, Any]:
    return {
        "command": "checkpoint",
        "action": "verify",
        "status": view.status,
        "checkpoint": view.public_dict(),
    }


def command_checkpoint(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.action == "reset":
        manager = CheckpointManager(
            args.state_dir,
            schema_version=SCHEMA_VERSION,
            quota_version=QUOTA_PROFILE_VERSION,
        )
        manager.reset()
        report = {
            "command": "checkpoint",
            "action": "reset",
            "status": "ok",
            "state_dir": str(args.state_dir),
        }
        _write_report(args.output, report)
        return report, EXIT_OK

    dataset = load_dataset(args.samples)
    manager = CheckpointManager(
        args.state_dir,
        schema_version=SCHEMA_VERSION,
        quota_version=dataset.quota_profile_version,
    )
    view = manager.load_for_resume()
    report = _checkpoint_view_report(view)
    diagnostics = view.diagnostics
    unhealthy = bool(
        diagnostics.get("damaged_checkpoints")
        or diagnostics.get("orphan_snapshots")
        or view.status in {"empty", "recovered_empty"}
    )
    if unhealthy:
        report["status"] = "partial"
        report["checkpoint"]["diagnostics"]["error_code"] = (
            "E_NO_CHECKPOINT"
            if view.status in {"empty", "recovered_empty"}
            and not diagnostics.get("damaged_checkpoints")
            and not diagnostics.get("orphan_snapshots")
            else "E_CHECKPOINT_CORRUPT"
        )
    _write_report(args.output, report)
    return report, EXIT_PARTIAL if unhealthy else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sso-ticket-guard",
        description="Deterministic offline SSO device-bound ticket validator",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import", help="parse and normalize declarations")
    import_parser.add_argument("--samples", type=Path, default=Path("samples"))
    import_parser.add_argument("--output", type=Path, default=Path("reports/import.json"))
    import_parser.set_defaults(handler=command_import)

    renew_parser = subparsers.add_parser("renew", help="adjudicate renewal requests")
    renew_parser.add_argument("--samples", type=Path, default=Path("samples"))
    renew_parser.add_argument("--output", type=Path, default=Path("reports/renew.json"))
    renew_parser.add_argument("--state-dir", type=Path, default=None)
    renew_parser.add_argument("--fresh", action="store_true")
    renew_parser.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help="stop after N newly processed batches (for interruption drills)",
    )
    renew_parser.set_defaults(handler=command_renew)

    revoke_parser = subparsers.add_parser("revoke", help="compute revocation closures")
    revoke_parser.add_argument("--samples", type=Path, default=Path("samples"))
    revoke_parser.add_argument("--output", type=Path, default=Path("reports/revoke.json"))
    revoke_parser.add_argument("--state-dir", type=Path, default=None)
    revoke_parser.set_defaults(handler=command_revoke)

    checkpoint_parser = subparsers.add_parser(
        "checkpoint",
        help="verify or reset durable checkpoints",
    )
    checkpoint_parser.add_argument("action", choices=["verify", "reset"])
    checkpoint_parser.add_argument("--samples", type=Path, default=Path("samples"))
    checkpoint_parser.add_argument("--state-dir", type=Path, default=Path(".sso-state"))
    checkpoint_parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/checkpoint.json"),
    )
    checkpoint_parser.set_defaults(handler=command_checkpoint)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "renew" and args.max_batches is not None and args.max_batches <= 0:
        parser.error("--max-batches must be positive")
    if (
        args.command == "renew"
        and args.max_batches is not None
        and args.state_dir is None
    ):
        parser.error("--max-batches requires --state-dir")
    try:
        report, exit_code = args.handler(args)
    except BusinessError as exc:
        error_report = {
            "command": getattr(args, "command", None),
            "status": "error",
            "error": exc.as_dict(),
        }
        if getattr(args, "output", None) is not None:
            _write_report(args.output, error_report)
        print(json.dumps(error_report, ensure_ascii=False, sort_keys=True))
        return EXIT_PARTIAL
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        error_report = {
            "command": getattr(args, "command", None),
            "status": "argument_error",
            "error": {"code": "E_INVALID_RECORD", "message": str(exc)},
        }
        print(json.dumps(error_report, ensure_ascii=False, sort_keys=True))
        return EXIT_ARGUMENT
    print(
        json.dumps(
            {
                "command": report.get("command"),
                "status": report.get("status"),
                "output": str(args.output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

