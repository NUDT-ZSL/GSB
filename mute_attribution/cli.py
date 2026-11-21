from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

from .config import STAGE_ORDER, load_config
from .consistency import run_consistency
from .errors import BusinessError
from .features import load_manifest
from .lineage import replay_event
from .pipeline import Pipeline
from .store import SnapshotStore
from .synth import edit_truth, generate_dataset, replace_pcm_gain
from .util import atomic_write_json


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _store(args: argparse.Namespace) -> SnapshotStore:
    return SnapshotStore(Path(args.store))


def _config(args: argparse.Namespace) -> dict[str, Any]:
    return load_config(Path(args.config))


def command_generate(args: argparse.Namespace) -> dict[str, Any]:
    return generate_dataset(
        Path(args.data_dir), seed=int(args.seed), calls=int(args.calls)
    )


def command_preflight(args: argparse.Namespace) -> dict[str, Any]:
    import numpy as np

    if sys.version_info < (3, 10):
        raise BusinessError(
            code="E_CONFIG_INVALID",
            context={
                "detail": "Python 3.10 or newer is required",
                "python": sys.version.split()[0],
            },
        )
    config = _config(args)
    store = _store(args)
    data_dir = Path(args.data_dir).resolve()
    if not data_dir.exists():
        raise BusinessError(
            code="E_INPUT_INVALID",
            context={"detail": "data directory is missing", "path": str(data_dir)},
        )
    descriptor, probe = tempfile.mkstemp(
        prefix=".preflight-", dir=str(store.work_root)
    )
    try:
        os.write(descriptor, b"preflight")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
        Path(probe).unlink(missing_ok=True)
    return {
        "code": "OK",
        "checks": {
            "python_ge_3_10": True,
            "numpy_available": True,
            "sample_rate_16000": config["sample_rate"] == 16000,
            "frame_ms_20": config["frame_ms"] == 20,
            "data_dir_readable": data_dir.is_dir(),
            "store_atomic_probe_writable": not Path(probe).exists(),
        },
        "environment": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "python_executable": sys.executable,
            "numpy": np.__version__,
            "data_dir": str(data_dir),
            "store": str(store.root),
        },
    }


def command_run(args: argparse.Namespace) -> dict[str, Any]:
    pipeline = Pipeline(
        Path(args.data_dir),
        _store(args),
        _config(args),
        crash_at=args.crash_at,
    )
    report = pipeline.run()
    if args.output:
        atomic_write_json(Path(args.output), report)
    return report


def command_consistency(args: argparse.Namespace) -> dict[str, Any]:
    calls = load_manifest(Path(args.data_dir))
    return run_consistency(_store(args), calls)


def command_replay(args: argparse.Namespace) -> dict[str, Any]:
    store = _store(args)
    calls = load_manifest(Path(args.data_dir))
    call_map = {call.call_id: call for call in calls}
    profile = store.get_current("profile")
    if profile is None:
        raise BusinessError(
            code="E_INCONSISTENT",
            context={"stage": "profile", "detail": "profile snapshot is missing"},
        )
    for record in profile.records:
        for event in record.get("events", []):
            if event.get("event_id") == args.event_id:
                call = call_map.get(str(event.get("call_id")))
                if call is None:
                    raise BusinessError(
                        code="E_LINEAGE_BROKEN",
                        context={
                            "event_id": args.event_id,
                            "detail": "event call is not in manifest",
                        },
                    )
                return replay_event(store, event, call)
    raise BusinessError(
        code="E_LINEAGE_BROKEN",
        context={"event_id": args.event_id, "detail": "event was not found"},
    )


def command_rollback(args: argparse.Namespace) -> dict[str, Any]:
    _store(args).attempt_rollback(args.stage, int(args.version))
    return {"code": "OK"}


def command_truth_edit(args: argparse.Namespace) -> dict[str, Any]:
    return edit_truth(
        Path(args.data_dir),
        args.call_id,
        int(args.start_frame),
        int(args.end_frame),
        args.kind,
    )


def command_replace_pcm(args: argparse.Namespace) -> dict[str, Any]:
    return replace_pcm_gain(Path(args.data_dir), args.call_id, float(args.gain))


def command_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    from .acceptance import run_acceptance

    return run_acceptance(Path(args.work_dir), Path(args.artifact_dir))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mute-attribution",
        description="Deterministic call-silence energy event attribution",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate")
    generate.add_argument("--data-dir", default="data")
    generate.add_argument("--seed", type=int, default=20261122)
    generate.add_argument("--calls", type=int, default=3)
    generate.set_defaults(handler=command_generate)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--data-dir", default="data")
    preflight.add_argument("--store", default="runtime/store")
    preflight.add_argument("--config", default="configs/default.json")
    preflight.set_defaults(handler=command_preflight)

    run = subparsers.add_parser("run")
    run.add_argument("--data-dir", default="data")
    run.add_argument("--store", default="runtime/store")
    run.add_argument("--config", default="configs/default.json")
    run.add_argument("--crash-at", default=None)
    run.add_argument("--output", default=None)
    run.set_defaults(handler=command_run)

    consistency = subparsers.add_parser("consistency")
    consistency.add_argument("--data-dir", default="data")
    consistency.add_argument("--store", default="runtime/store")
    consistency.set_defaults(handler=command_consistency)

    replay = subparsers.add_parser("replay")
    replay.add_argument("--data-dir", default="data")
    replay.add_argument("--store", default="runtime/store")
    replay.add_argument("--event-id", required=True)
    replay.set_defaults(handler=command_replay)

    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--store", default="runtime/store")
    rollback.add_argument("--stage", choices=STAGE_ORDER, required=True)
    rollback.add_argument("--version", type=int, required=True)
    rollback.set_defaults(handler=command_rollback)

    truth_edit = subparsers.add_parser("truth-edit")
    truth_edit.add_argument("--data-dir", default="data")
    truth_edit.add_argument("--call-id", required=True)
    truth_edit.add_argument("--start-frame", type=int, required=True)
    truth_edit.add_argument("--end-frame", type=int, required=True)
    truth_edit.add_argument("--kind", choices=["speech", "silence"], required=True)
    truth_edit.set_defaults(handler=command_truth_edit)

    replace_pcm = subparsers.add_parser("replace-pcm")
    replace_pcm.add_argument("--data-dir", default="data")
    replace_pcm.add_argument("--call-id", required=True)
    replace_pcm.add_argument("--gain", type=float, required=True)
    replace_pcm.set_defaults(handler=command_replace_pcm)

    acceptance = subparsers.add_parser("acceptance")
    acceptance.add_argument("--work-dir", default="runtime/acceptance")
    acceptance.add_argument("--artifact-dir", default="artifacts")
    acceptance.set_defaults(handler=command_acceptance)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
        _print(result)
        return 0
    except BusinessError as exc:
        _print(exc.to_dict())
        return 1
    except Exception as exc:
        if "--debug" in (argv if argv is not None else sys.argv[1:]):
            traceback.print_exc()
        _print(
            {
                "ok": False,
                "code": "E_INTERNAL",
                "message": "unexpected internal error",
                "context": {"detail": str(exc)},
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

