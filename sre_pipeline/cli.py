"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from .engine import replay_all
from .errors import (
    E_BAD_ARGUMENT,
    E_NOT_LOADED,
    E_SAMPLE_CONFLICT,
    E_STORAGE_STATE,
    PipelineError,
)
from .model import SampleBundle, load_bundle
from .storage import (
    Store,
    get_status,
    initialize_store,
    load_bundle_from_store,
    reset_database_files,
    run_pending,
    verify_sample_matches,
    verify_store,
)


DEFAULT_DB = "artifacts/pipeline.sqlite"
DEFAULT_CONFIG = "samples/config.json"
DEFAULT_EVENTS = "samples/events.json"
DEFAULT_ANOMALIES = "samples/anomalies.json"


def _json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )


def _emit(value: Any, output: Optional[str] = None) -> None:
    text = _json_text(value) + "\n"
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        summary = {
            "status": "written",
            "output": str(path),
        }
        if isinstance(value, dict):
            for key in (
                "ok",
                "processed",
                "committed",
                "errors",
                "state_version",
                "records_checked",
            ):
                if key in value:
                    summary[key] = value[key]
        print(_json_text(summary))
    else:
        print(text, end="")


def _load_bundle_from_args(
    config: Optional[str],
    events: Optional[str],
    anomalies: Optional[str],
) -> SampleBundle:
    if config is None or events is None:
        raise PipelineError(
            E_SAMPLE_CONFLICT,
            "both --config and --events are required",
        )
    return load_bundle(config, events, anomalies)


def _default_bundle_if_present() -> Optional[SampleBundle]:
    if Path(DEFAULT_CONFIG).is_file() and Path(DEFAULT_EVENTS).is_file():
        anomalies = DEFAULT_ANOMALIES if Path(DEFAULT_ANOMALIES).is_file() else None
        return load_bundle(DEFAULT_CONFIG, DEFAULT_EVENTS, anomalies)
    return None


def _open_existing_store(db_path: str) -> Store:
    if not Path(db_path).is_file():
        raise PipelineError(
            E_NOT_LOADED,
            f"database not found: {db_path}",
            details={"db": db_path},
        )
    store = Store(db_path)
    if not store.is_initialized():
        store.close()
        raise PipelineError(
            E_NOT_LOADED,
            f"database is not initialized: {db_path}",
            details={"db": db_path},
        )
    return store


def _resolve_existing_bundle(
    store: Store,
    config: Optional[str],
    events: Optional[str],
    anomalies: Optional[str],
) -> SampleBundle:
    if config is not None or events is not None or anomalies is not None:
        bundle = _load_bundle_from_args(config, events, anomalies)
        verify_sample_matches(store, bundle)
        return bundle
    return load_bundle_from_store(store)


def _parse_crash_points(values: Sequence[str]) -> Dict[Tuple[str, int], None]:
    result: Dict[Tuple[str, int], None] = {}
    valid_stages = {"parse", "rate_limit", "window_commit", "degradation"}
    for value in values:
        try:
            stage, seq_text = value.rsplit(":", 1)
            seq = int(seq_text)
        except (ValueError, TypeError):
            raise PipelineError(
                E_BAD_ARGUMENT,
                f"invalid --crash-at value: {value!r}; expected STAGE:SEQ",
                details={"value": value},
            )
        if stage not in valid_stages:
            raise PipelineError(
                E_BAD_ARGUMENT,
                f"invalid crash stage: {stage}",
                details={"stage": stage, "valid": sorted(valid_stages)},
            )
        result[(stage, seq)] = None
    return result


def _crash_hook(points: Dict[Tuple[str, int], None]):
    if not points:
        return None

    def hook(stage: str, event_seq: int) -> None:
        if (stage, event_seq) in points:
            os._exit(86)

    return hook


def command_load(args: argparse.Namespace) -> int:
    anomalies = args.anomalies
    if (
        anomalies is None
        and args.config == DEFAULT_CONFIG
        and args.events == DEFAULT_EVENTS
        and Path(DEFAULT_ANOMALIES).is_file()
    ):
        anomalies = DEFAULT_ANOMALIES
    bundle = load_bundle(args.config, args.events, anomalies)
    if args.force:
        reset_database_files(args.db)
    with Store(args.db) as store:
        result = initialize_store(store, bundle)
    _emit({"ok": True, **result}, args.output)
    return 0


def command_run(args: argparse.Namespace, *, resume_only: bool = False) -> int:
    crash_hook = _crash_hook(_parse_crash_points(args.crash_at))
    db_exists = Path(args.db).is_file()
    supplied_bundle: Optional[SampleBundle] = None
    if args.config is not None or args.events is not None or args.anomalies is not None:
        supplied_bundle = _load_bundle_from_args(
            args.config,
            args.events,
            args.anomalies,
        )
    elif not db_exists and not resume_only:
        supplied_bundle = _default_bundle_if_present()

    if db_exists:
        store = _open_existing_store(args.db)
    else:
        if resume_only:
            raise PipelineError(
                E_NOT_LOADED,
                f"database not found: {args.db}",
                details={"db": args.db},
            )
        if supplied_bundle is None:
            raise PipelineError(
                E_NOT_LOADED,
                "no database and no sample supplied",
                details={
                    "hint": "use load first or pass --config and --events",
                },
            )
        store = Store(args.db)
        initialize_store(store, supplied_bundle)

    with store:
        bundle = supplied_bundle
        if bundle is not None:
            verify_sample_matches(store, bundle)
        else:
            bundle = load_bundle_from_store(store)
        result = run_pending(
            store,
            bundle,
            max_events=args.max_events,
            crash_hook=crash_hook,
        )

    payload = {"ok": True, **result}
    _emit(payload, args.output)
    if args.fail_on_anomaly and int(result["errors"]) > 0:
        return 3
    return 0


def command_resume(args: argparse.Namespace) -> int:
    return command_run(args, resume_only=True)


def command_replay(args: argparse.Namespace) -> int:
    db_exists = Path(args.db).is_file()
    if args.config is not None or args.events is not None or args.anomalies is not None:
        bundle = _load_bundle_from_args(args.config, args.events, args.anomalies)
    elif db_exists:
        with _open_existing_store(args.db) as store:
            bundle = load_bundle_from_store(store)
    else:
        bundle = _default_bundle_if_present()
    if bundle is None:
        raise PipelineError(
            E_NOT_LOADED,
            "no replay source supplied",
            details={"hint": "pass --config/--events or load a database"},
        )
    result = replay_all(bundle.policy, bundle.events, bundle.anomalies)
    payload = {
        "ok": True,
        "policy_version": bundle.policy.policy_version,
        "events": len(bundle.events),
        "anomalies": len(bundle.anomalies),
        **result,
    }
    _emit(payload, args.output)
    return 0


def command_status(args: argparse.Namespace) -> int:
    with _open_existing_store(args.db) as store:
        result = get_status(store)
    _emit({"ok": True, **result}, args.output)
    return 0


def command_verify(args: argparse.Namespace) -> int:
    with _open_existing_store(args.db) as store:
        if args.config is not None or args.events is not None or args.anomalies is not None:
            bundle = _load_bundle_from_args(
                args.config,
                args.events,
                args.anomalies,
            )
            verify_sample_matches(store, bundle)
        result = verify_store(store)
    _emit(result, args.output)
    return 0 if result["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sre-pipeline",
        description="Deterministic SRE limit/circuit/degradation pipeline",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite state database")
    parser.add_argument(
        "--output",
        default=None,
        help="write the full JSON result to this path",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_io_options(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--db",
            default=argparse.SUPPRESS,
            help="SQLite state database (may also be placed before the subcommand)",
        )
        subparser.add_argument(
            "--output",
            default=argparse.SUPPRESS,
            help="write full JSON to this path (may also be placed before the subcommand)",
        )

    load_parser = subparsers.add_parser("load", help="validate and load local samples")
    add_io_options(load_parser)
    load_parser.add_argument("--config", default=DEFAULT_CONFIG)
    load_parser.add_argument("--events", default=DEFAULT_EVENTS)
    load_parser.add_argument("--anomalies", default=None)
    load_parser.add_argument("--force", action="store_true")
    load_parser.set_defaults(handler=command_load)

    run_parser = subparsers.add_parser("run", help="process all pending events")
    add_io_options(run_parser)
    run_parser.add_argument("--config", default=None)
    run_parser.add_argument("--events", default=None)
    run_parser.add_argument("--anomalies", default=None)
    run_parser.add_argument("--max-events", type=int, default=None)
    run_parser.add_argument(
        "--crash-at",
        action="append",
        default=[],
        metavar="STAGE:SEQ",
        help="force-exit at a deterministic crash point",
    )
    run_parser.add_argument("--fail-on-anomaly", action="store_true")
    run_parser.set_defaults(handler=command_run)

    resume_parser = subparsers.add_parser(
        "resume",
        help="resume after a committed watermark without repeating events",
    )
    add_io_options(resume_parser)
    resume_parser.add_argument("--config", default=None)
    resume_parser.add_argument("--events", default=None)
    resume_parser.add_argument("--anomalies", default=None)
    resume_parser.add_argument("--max-events", type=int, default=None)
    resume_parser.add_argument(
        "--crash-at",
        action="append",
        default=[],
        metavar="STAGE:SEQ",
    )
    resume_parser.add_argument("--fail-on-anomaly", action="store_true")
    resume_parser.set_defaults(handler=command_resume)

    replay_parser = subparsers.add_parser(
        "replay",
        help="run a full deterministic in-memory replay",
    )
    add_io_options(replay_parser)
    replay_parser.add_argument("--config", default=None)
    replay_parser.add_argument("--events", default=None)
    replay_parser.add_argument("--anomalies", default=None)
    replay_parser.set_defaults(handler=command_replay)

    status_parser = subparsers.add_parser(
        "status",
        help="print stable JSON status from the committed pointer",
    )
    add_io_options(status_parser)
    status_parser.set_defaults(handler=command_status)

    verify_parser = subparsers.add_parser(
        "verify",
        help="compare persisted state and traces with a full replay",
    )
    add_io_options(verify_parser)
    verify_parser.add_argument("--config", default=None)
    verify_parser.add_argument("--events", default=None)
    verify_parser.add_argument("--anomalies", default=None)
    verify_parser.set_defaults(handler=command_verify)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "max_events", None) is not None and args.max_events <= 0:
        parser.error("--max-events must be positive")
    try:
        return int(args.handler(args))
    except PipelineError as exc:
        payload = {"ok": False, **exc.to_dict()}
        print(_json_text(payload), file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        payload = {
            "ok": False,
            "error_code": E_STORAGE_STATE,
            "message": str(exc),
        }
        print(_json_text(payload), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

