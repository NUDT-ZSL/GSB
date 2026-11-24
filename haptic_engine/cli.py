from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from .engine import HapticEngine, load_bundle_from_workspace
from .errors import HapticError
from .samples import DEFAULT_SEED, generate_workspace


class _SignalState:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.info: dict[str, Any] = {}
        self._previous: dict[int, Any] = {}

    def __enter__(self):
        if threading.current_thread() is not threading.main_thread():
            return self

        def handler(signum: int, _frame: Any) -> None:
            if "signal" not in self.info:
                self.info["signal"] = signum
            self.stop_event.set()

        for signal_name in ("SIGINT", "SIGTERM"):
            candidate = getattr(signal, signal_name, None)
            if candidate is None:
                continue
            try:
                self._previous[candidate] = signal.getsignal(candidate)
                signal.signal(candidate, handler)
            except (OSError, ValueError):
                pass
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for signum, previous in self._previous.items():
            try:
                signal.signal(signum, previous)
            except (OSError, ValueError):
                pass


def _path_from_workspace(workspace: Path, value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    return path if path.is_absolute() else workspace / path


def _ensure_samples(
    workspace: Path,
    seed: int,
    regenerate: bool,
    devices_path: Path | None,
    events_path: Path | None,
    config_path: Path | None,
) -> None:
    default_paths = {
        workspace / "devices.json",
        workspace / "events.json",
        workspace / "config.json",
    }
    if regenerate:
        if devices_path or events_path or config_path:
            raise HapticError(
                "E_CONFIG",
                "--regenerate-samples cannot be combined with custom input paths",
                {},
            )
        generate_workspace(workspace, seed=seed, force=True)
        return
    if devices_path or events_path or config_path:
        for label, path in (
            ("devices", devices_path),
            ("events", events_path),
            ("config", config_path),
        ):
            if path is not None and not path.exists():
                raise HapticError(
                    "E_INPUT",
                    f"specified {label} file does not exist",
                    {"path": str(path)},
                )
        return
    existing = {path for path in default_paths if path.exists()}
    if not existing:
        generate_workspace(workspace, seed=seed, force=False)
    elif existing != default_paths:
        missing = sorted(str(path) for path in default_paths - existing)
        raise HapticError(
            "E_INPUT",
            "input sample set is incomplete",
            {"missing": missing},
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synthesize deterministic haptic command streams"
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="workspace containing inputs, state, and export directories",
    )
    parser.add_argument("--devices", help="custom device manifest path")
    parser.add_argument("--events", help="custom event script path")
    parser.add_argument("--config", help="custom engine config path")
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="seed used when local samples must be generated",
    )
    parser.add_argument(
        "--regenerate-samples",
        action="store_true",
        help="overwrite devices.json, events.json, and config.json from the seed",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit compact JSON on stdout",
    )
    return parser


def _print_json(value: dict[str, Any], compact: bool) -> None:
    if compact:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    else:
        text = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
    print(text)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).resolve()
    devices_path = _path_from_workspace(workspace, args.devices)
    events_path = _path_from_workspace(workspace, args.events)
    config_path = _path_from_workspace(workspace, args.config)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        _ensure_samples(
            workspace,
            args.seed,
            args.regenerate_samples,
            devices_path,
            events_path,
            config_path,
        )
        bundle = load_bundle_from_workspace(
            workspace,
            devices_path=devices_path,
            events_path=events_path,
            config_path=config_path,
        )
    except HapticError as error:
        _print_json(error.to_dict(), args.compact)
        return 2
    except KeyboardInterrupt:
        interrupted = HapticError(
            "E_INTERRUPTED",
            "process was interrupted during input loading",
            {"exit_code": 130},
        ).to_dict()
        interrupted["status"] = "interrupted"
        _print_json(interrupted, args.compact)
        return 130
    except OSError as error:
        io_error = HapticError(
            "E_IO",
            "failed to prepare workspace or sample files",
            {"message": str(error)},
        )
        _print_json(io_error.to_dict(), args.compact)
        return 2

    with _SignalState() as signal_state:
        engine = HapticEngine(bundle)
        try:
            result = engine.run(
                stop_event=signal_state.stop_event,
                signal_info=signal_state.info,
            )
        except HapticError as error:
            _print_json(error.to_dict(), args.compact)
            return 2
        except KeyboardInterrupt:
            interrupted = HapticError(
                "E_INTERRUPTED",
                "process was interrupted before a block-boundary result was available",
                {"exit_code": 130},
            ).to_dict()
            interrupted["status"] = "interrupted"
            _print_json(interrupted, args.compact)
            return 130
        except OSError as error:
            io_error = HapticError(
                "E_IO", "unexpected I/O failure", {"message": str(error)}
            )
            _print_json(io_error.to_dict(), args.compact)
            return 2

    if result.status == "interrupted":
        payload = {
            "ok": False,
            "status": "interrupted",
            "code": "E_INTERRUPTED",
            "message": "graceful stop accepted at a frame-block boundary",
            "business_codes": ["E_INTERRUPTED"],
            "exit_code": result.exit_code,
            "interrupt_path": str(result.interrupt_path),
        }
        _print_json(payload, args.compact)
        return result.exit_code

    report = result.report or {}
    payload = {
        "ok": True,
        "status": "completed",
        "exit_code": 0,
        "version_id": report.get("version_id"),
        "commands_path": str(result.commands_path),
        "report_path": str(result.report_path),
        "business_codes": report.get("business_codes", []),
        "totals": report.get("totals", {}),
        "scheduling": report.get("scheduling", {}),
    }
    _print_json(payload, args.compact)
    return 0


if __name__ == "__main__":
    sys.exit(main())
