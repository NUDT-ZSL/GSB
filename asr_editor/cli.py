"""Command-line interface for ASR transcript interoperability workflows."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import types
from pathlib import Path
from typing import Any

from .codes import (
    E_EDIT_INVALID,
    E_FORMAT_INVALID,
    E_INTERNAL,
    E_IO,
    E_PLAN_MISMATCH,
    E_SNAPSHOT_INVALID,
)
from .diagnostics import build_cross_format_report, write_report
from .formats import (
    atomic_write_json,
    build_sidecar,
    content_hash,
    write_sidecar,
    write_v1,
    write_v2,
)
from .interop import upgrade_v2
from .operations import OperationError
from .parsers import FailureRecord, parse_v1_file, parse_v2_file
from .samples import generate_samples
from .snapshots import SnapshotError, SnapshotStore, plan_digest
from .timeutil import PrecisionStats
from .workspace import Workspace, default_sidecar_path, default_state_dir


def configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _failure_dicts(failures: list[FailureRecord]) -> list[dict[str, Any]]:
    return [failure.to_dict() for failure in failures]


def _write_optional_report(path: str | None, payload: dict[str, Any]) -> None:
    if path:
        atomic_write_json(path, payload)


def _business_codes(
    failures: list[FailureRecord], diagnostics: list[str] | None = None
) -> list[str]:
    codes = [failure.code for failure in failures]
    codes.extend(diagnostics or [])
    return list(dict.fromkeys(codes))


class GracefulSignalHandler:
    """Turn SIGINT/SIGTERM/SIGBREAK into a safe shutdown request."""

    def __init__(self) -> None:
        self.requested = False
        self.signal_name: str | None = None
        self._previous: dict[int, Any] = {}

    def _handle(self, signum: int, frame: Any) -> None:
        self.requested = True
        self.signal_name = signal.Signals(signum).name

    def __enter__(self) -> GracefulSignalHandler:  # noqa: PYI034
        names = ["SIGINT", "SIGTERM", "SIGBREAK"]
        for name in names:
            if not hasattr(signal, name):
                continue
            signum = getattr(signal, name)
            try:
                self._previous[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle)
            except (ValueError, OSError):
                continue
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: types.TracebackType | None,
    ) -> None:
        for signum, previous in self._previous.items():
            try:
                signal.signal(signum, previous)
            except (ValueError, OSError):
                pass


def _test_delay() -> None:
    """Test-only inter-operation pause used to exercise signal handling."""

    raw = os.environ.get("ASR_EDITOR_TEST_OP_DELAY_MS")
    if not raw:
        return
    try:
        delay = max(0, int(raw)) / 1000
    except ValueError:
        return
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


def _load_operations(path: str | Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read operation plan: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"operation plan is not UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid operation plan JSON at line {exc.lineno}: {exc.msg}"
        ) from exc
    operations = data.get("operations") if isinstance(data, dict) else data
    if not isinstance(operations, list) or not all(
        isinstance(item, dict) for item in operations
    ):
        raise ValueError(
            "operation plan must be a list of objects or {'operations': [...]}"
        )
    return operations


def _common_workspace_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "v1_path": args.v1,
        "v2_path": args.v2,
        "sidecar_path": args.sidecar,
        "state_dir": args.state_dir,
    }


def command_generate_samples(args: argparse.Namespace) -> int:
    try:
        manifest = generate_samples(args.out)
    except Exception as exc:
        failures = [
            FailureRecord(
                "generate",
                str(args.out),
                None,
                E_INTERNAL,
                type(exc).__name__,
                str(exc),
            )
        ]
        payload = {
            "command": "generate-samples",
            "failure_list": _failure_dicts(failures),
        }
        _print_json(payload)
        _write_optional_report(args.report, payload)
        return 1
    payload = {
        "command": "generate-samples",
        "output_dir": str(args.out),
        "manifest": manifest,
    }
    _print_json(payload)
    _write_optional_report(args.report, payload)
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    failures: list[FailureRecord] = []
    payload: dict[str, Any] = {"command": "inspect"}
    if args.v1:
        try:
            result = parse_v1_file(args.v1)
            failures.extend(result.failures)
            payload["v1"] = {
                "source_file": str(args.v1),
                "utterances": len(result.transcript.utterances),
                "tokens": result.transcript.token_count,
                "precision": result.precision.to_dict(),
                "transcript": result.transcript.to_dict(),
            }
        except Exception as exc:
            failures.append(
                FailureRecord(
                    "parse",
                    str(args.v1),
                    None,
                    E_INTERNAL,
                    type(exc).__name__,
                    str(exc),
                )
            )
    if args.v2:
        try:
            result = parse_v2_file(args.v2)
            failures.extend(result.failures)
            payload["v2"] = {
                "source_file": str(args.v2),
                "utterances": len(result.transcript.utterances),
                "tokens": result.transcript.token_count,
                "precision": result.precision.to_dict(),
                "transcript": result.transcript.to_dict(),
            }
        except Exception as exc:
            failures.append(
                FailureRecord(
                    "parse",
                    str(args.v2),
                    None,
                    E_INTERNAL,
                    type(exc).__name__,
                    str(exc),
                )
            )
    payload["failure_list"] = _failure_dicts(failures)
    payload["business_codes"] = _business_codes(failures)
    _print_json(payload)
    _write_optional_report(args.report, payload)
    return 1 if failures else 0


def command_init(args: argparse.Namespace) -> int:
    workspace = Workspace.open(
        **_common_workspace_kwargs(args), recover=False, create_initial_snapshot=False
    )
    snapshot = workspace.initialize()
    payload = {
        "command": "init",
        "workspace_version": workspace.version,
        "snapshot_version": snapshot.version,
        "ir_hash": snapshot.ir_hash,
        "sidecar_hash": snapshot.sidecar_hash,
        "state_dir": str(workspace.paths.state_dir),
        "sidecar": str(workspace.paths.sidecar),
        "failure_list": _failure_dicts(workspace.failures),
        "business_codes": _business_codes(workspace.failures, workspace.diagnostics),
    }
    _print_json(payload)
    _write_optional_report(args.report, payload)
    return 1 if workspace.failures else 0


def command_recover(args: argparse.Namespace) -> int:
    workspace = Workspace.open(
        **_common_workspace_kwargs(args), recover=True, create_initial_snapshot=False
    )
    try:
        latest = workspace.store.load_latest()
    except SnapshotError:
        latest = None
    payload = {
        "command": "recover",
        "recovery": workspace.recovery.to_dict(),
        "workspace_version": workspace.version,
        "snapshot_version": workspace.version,
        "resume_after_operation": workspace.resume_after_operation,
        "ir_hash": latest.ir_hash if latest else None,
        "failure_list": _failure_dicts(workspace.failures),
        "business_codes": _business_codes(workspace.failures, workspace.diagnostics),
    }
    _print_json(payload)
    _write_optional_report(args.report, payload)
    return 1 if workspace.failures else 0


def command_edit(args: argparse.Namespace) -> int:
    with GracefulSignalHandler() as handler:
        return _command_edit_impl(args, handler)


def _command_edit_impl(args: argparse.Namespace, handler: GracefulSignalHandler) -> int:
    failure_list: list[FailureRecord] = []
    try:
        operations = _load_operations(args.ops)
    except ValueError as exc:
        failure = FailureRecord(
            "edit", str(args.ops), None, E_EDIT_INVALID, type(exc).__name__, str(exc)
        )
        payload = {
            "command": "edit",
            "failure_list": _failure_dicts([failure]),
            "business_codes": [failure.code],
        }
        _print_json(payload)
        _write_optional_report(args.report, payload)
        return 1

    digest = plan_digest(operations)
    workspace = Workspace.open(
        **_common_workspace_kwargs(args), recover=True, create_initial_snapshot=True
    )
    failure_list.extend(workspace.failures)
    if (
        workspace.version == 0
        and not workspace.operation_log
        and workspace.plan_digest != digest
    ):
        workspace.initialize(digest)

    start_after = 0
    plan_mismatch = False
    if workspace.plan_digest is not None and workspace.plan_digest != digest:
        failure_list.append(
            FailureRecord(
                "edit",
                str(args.ops),
                None,
                E_PLAN_MISMATCH,
                "PlanDigestMismatch",
                "operation plan differs from the persisted resume plan",
            )
        )
        plan_mismatch = True
    else:
        start_after = workspace.resume_after_operation
        if start_after > len(operations):
            failure_list.append(
                FailureRecord(
                    "edit",
                    str(args.ops),
                    start_after,
                    E_EDIT_INVALID,
                    "ResumePositionError",
                    f"resume position {start_after} exceeds {len(operations)} operations",
                )
            )
            start_after = len(operations)

    applied: list[dict[str, Any]] = []
    graceful_signal: str | None = None
    for operation_index, operation in enumerate(operations, start=1):
        if plan_mismatch:
            break
        if operation_index <= start_after:
            continue
        if handler.requested:
            graceful_signal = handler.signal_name
            break
        try:
            result = workspace.apply_operation(operation, operation_index, digest)
            applied.append(result)
        except OperationError as exc:
            failure_list.append(
                FailureRecord(
                    "edit",
                    str(workspace.paths.v1),
                    operation_index,
                    exc.code,
                    type(exc).__name__,
                    str(exc),
                )
            )
        except Exception as exc:
            failure_list.append(
                FailureRecord(
                    "edit",
                    str(workspace.paths.v1),
                    operation_index,
                    E_INTERNAL,
                    type(exc).__name__,
                    str(exc),
                )
            )
        _test_delay()
        if handler.requested:
            graceful_signal = handler.signal_name
            break
    if graceful_signal is None and handler.requested:
        graceful_signal = handler.signal_name

    try:
        latest = workspace.store.load_latest()
    except SnapshotError:
        latest = None
    payload = {
        "command": "edit",
        "files": {
            "v1": str(workspace.paths.v1),
            "v2": str(workspace.paths.v2),
            "sidecar": str(workspace.paths.sidecar),
        },
        "operations_total": len(operations),
        "resumed_after_operation": start_after,
        "operations_applied_this_run": len(applied),
        "last_completed_operation": workspace.resume_after_operation,
        "workspace_version": workspace.version,
        "snapshot_version": latest.version if latest else workspace.version,
        "operation_plan_digest": digest,
        "operation_log": [
            {
                "seq": item.get("seq"),
                "operation_index": item.get("operation_index"),
                "op": item.get("op"),
                "summary": item.get("summary"),
                "snapshot_version": item.get("snapshot_version"),
            }
            for item in workspace.operation_log
        ],
        "applied": applied,
        "recovery": workspace.recovery.to_dict(),
        "graceful_signal": graceful_signal,
        "signal_exit_code": 0 if graceful_signal else None,
        "failure_list": _failure_dicts(failure_list),
        "business_codes": _business_codes(failure_list, workspace.diagnostics),
    }
    _print_json(payload)
    _write_optional_report(args.report, payload)
    if graceful_signal:
        return 0
    return 1 if failure_list else 0


def _snapshot_info(
    state_dir: str | Path,
) -> tuple[dict[str, Any] | None, list[FailureRecord]]:
    failures: list[FailureRecord] = []
    store = SnapshotStore(state_dir)
    try:
        snapshot = store.load_latest()
    except SnapshotError as exc:
        failures.append(
            FailureRecord(
                "recover",
                str(state_dir),
                None,
                E_SNAPSHOT_INVALID,
                type(exc).__name__,
                str(exc),
            )
        )
        return None, failures
    if snapshot is None:
        return None, failures
    return {
        "version": snapshot.version,
        "ir_hash": snapshot.ir_hash,
        "sidecar_hash": snapshot.sidecar_hash,
        "operation_log_digest": snapshot.operation_log_digest,
        "last_completed_operation": snapshot.last_completed_operation,
        "precision": snapshot.precision.to_dict(),
    }, failures


def command_diagnose(args: argparse.Namespace) -> int:
    failures: list[FailureRecord] = []
    diagnostics: list[str] = []
    v1_result = None
    v2_result = None
    try:
        v1_result = parse_v1_file(args.v1)
        failures.extend(v1_result.failures)
    except Exception as exc:
        failures.append(
            FailureRecord(
                "parse", str(args.v1), None, E_INTERNAL, type(exc).__name__, str(exc)
            )
        )
    try:
        v2_result = parse_v2_file(args.v2)
        failures.extend(v2_result.failures)
    except Exception as exc:
        failures.append(
            FailureRecord(
                "parse", str(args.v2), None, E_INTERNAL, type(exc).__name__, str(exc)
            )
        )
    state_dir = args.state_dir or default_state_dir(args.v1)
    snapshot_info, snapshot_failures = _snapshot_info(state_dir)
    failures.extend(snapshot_failures)
    snapshot_version = int(snapshot_info["version"]) if snapshot_info else 0
    from .models import Transcript

    report = build_cross_format_report(
        v1_result.transcript
        if v1_result
        else Transcript(source_version=1, utterances=[]),
        v2_result.transcript
        if v2_result
        else Transcript(source_version=2, utterances=[]),
        snapshot_version=snapshot_version,
        failures=failures,
        diagnostic_codes=diagnostics,
        precision_v1=v1_result.precision if v1_result else PrecisionStats(),
        precision_v2=v2_result.precision if v2_result else PrecisionStats(),
        source_v1=str(args.v1),
        source_v2=str(args.v2),
        snapshot=snapshot_info,
    )
    if args.out:
        write_report(args.out, report)
    _print_json(report)
    return 1 if failures else 0


def command_upgrade(args: argparse.Namespace) -> int:
    parsed = parse_v2_file(args.input)
    sidecar_path = args.sidecar
    if sidecar_path is None:
        input_path = Path(args.input)
        name = input_path.name
        base = (
            name[: -len(".v2.srt")]
            if name.lower().endswith(".v2.srt")
            else input_path.stem
        )
        candidate = input_path.with_name(f"{base}.tokens.meta.json")
        sidecar_path = str(candidate) if candidate.exists() else None
    upgraded = upgrade_v2(parsed.transcript, sidecar_path)
    failures = parsed.failures + upgraded.failures
    payload = {
        "command": "upgrade",
        "input": str(args.input),
        "output": str(args.out),
        "sidecar_path": sidecar_path,
        "sidecar_used": upgraded.sidecar_used,
        "inferred_time_count": upgraded.inferred_time_count,
        "reason": upgraded.reason,
        "diagnostic_codes": upgraded.diagnostic_codes,
        "utterances": len(upgraded.transcript.utterances),
        "tokens": upgraded.transcript.token_count,
        "failure_list": _failure_dicts(failures),
        "business_codes": _business_codes(failures, upgraded.diagnostic_codes),
    }
    if not failures:
        try:
            write_v1(args.out, upgraded.transcript)
        except Exception as exc:
            failures.append(
                FailureRecord(
                    "upgrade", str(args.out), None, E_IO, type(exc).__name__, str(exc)
                )
            )
            payload["failure_list"] = _failure_dicts(failures)
            payload["business_codes"] = _business_codes(
                failures, upgraded.diagnostic_codes
            )
    _print_json(payload)
    _write_optional_report(args.report, payload)
    return 1 if failures else 0


def command_downgrade(args: argparse.Namespace) -> int:
    parsed = parse_v1_file(args.input)
    sidecar_path = (
        Path(args.sidecar) if args.sidecar else default_sidecar_path(args.input)
    )
    payload: dict[str, Any] = {
        "command": "downgrade",
        "input": str(args.input),
        "output": str(args.out),
        "sidecar_path": str(sidecar_path),
        "utterances": len(parsed.transcript.utterances),
        "tokens": parsed.transcript.token_count,
        "failure_list": _failure_dicts(parsed.failures),
        "business_codes": _business_codes(parsed.failures),
    }
    if not parsed.failures:
        try:
            sidecar = build_sidecar(parsed.transcript)
            write_v2(args.out, parsed.transcript)
            write_sidecar(sidecar_path, sidecar)
            payload["sidecar_hash"] = content_hash(sidecar)
        except Exception as exc:
            failure = FailureRecord(
                "downgrade", str(args.out), None, E_IO, type(exc).__name__, str(exc)
            )
            parsed.failures.append(failure)
            payload["failure_list"] = _failure_dicts(parsed.failures)
            payload["business_codes"] = _business_codes(parsed.failures)
    _print_json(payload)
    _write_optional_report(args.report, payload)
    return 1 if parsed.failures else 0


def command_verify_wav(args: argparse.Namespace) -> int:
    failures: list[FailureRecord] = []
    try:
        import soundfile as sf

        info = sf.info(args.wav)
        ok = (
            info.samplerate == 16000 and info.channels == 1 and info.subtype == "PCM_16"
        )
        payload = {
            "command": "verify-wav",
            "wav": str(args.wav),
            "samplerate": info.samplerate,
            "channels": info.channels,
            "subtype": info.subtype,
            "frames": info.frames,
            "valid_16k_mono_pcm": ok,
            "failure_list": [],
            "business_codes": [],
        }
        if not ok:
            failure = FailureRecord(
                "parse",
                str(args.wav),
                None,
                E_FORMAT_INVALID,
                "AudioFormatError",
                "expected 16 kHz mono PCM_16 WAV",
            )
            failures.append(failure)
            payload["failure_list"] = _failure_dicts(failures)
            payload["business_codes"] = _business_codes(failures)
    except Exception as exc:
        failures.append(
            FailureRecord(
                "parse", str(args.wav), None, E_INTERNAL, type(exc).__name__, str(exc)
            )
        )
        payload = {
            "command": "verify-wav",
            "wav": str(args.wav),
            "valid_16k_mono_pcm": False,
            "failure_list": _failure_dicts(failures),
            "business_codes": _business_codes(failures),
        }
    _print_json(payload)
    _write_optional_report(args.report, payload)
    return 0 if not failures else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asr-editor", description="ASR v1/v2 interoperability editor"
    )
    parser.add_argument("--version", action="version", version="asr-editor 1.0.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate-samples", help="generate deterministic local samples"
    )
    generate.add_argument("--out", default="samples")
    generate.add_argument("--report")
    generate.set_defaults(func=command_generate_samples)

    inspect = subparsers.add_parser("inspect", help="parse and print both formats")
    inspect.add_argument("--v1")
    inspect.add_argument("--v2")
    inspect.add_argument("--report")
    inspect.set_defaults(func=command_inspect)

    for name, function in (("init", command_init), ("recover", command_recover)):
        sub = subparsers.add_parser(name)
        sub.add_argument("--v1", required=True)
        sub.add_argument("--v2", required=True)
        sub.add_argument("--sidecar")
        sub.add_argument("--state-dir")
        sub.add_argument("--report")
        sub.set_defaults(func=function)

    edit = subparsers.add_parser("edit", help="apply an operation plan with snapshots")
    edit.add_argument("--v1", required=True)
    edit.add_argument("--v2", required=True)
    edit.add_argument("--sidecar")
    edit.add_argument("--state-dir")
    edit.add_argument("--ops", required=True)
    edit.add_argument("--report")
    edit.set_defaults(func=command_edit)

    diagnose = subparsers.add_parser(
        "diagnose", help="write a cross-format consistency report"
    )
    diagnose.add_argument("--v1", required=True)
    diagnose.add_argument("--v2", required=True)
    diagnose.add_argument("--state-dir")
    diagnose.add_argument("--out")
    diagnose.set_defaults(func=command_diagnose)

    upgrade = subparsers.add_parser("upgrade", help="upgrade v2 SRT to v1 JSON")
    upgrade.add_argument("--input", required=True)
    upgrade.add_argument("--out", required=True)
    upgrade.add_argument("--sidecar")
    upgrade.add_argument("--report")
    upgrade.set_defaults(func=command_upgrade)

    downgrade = subparsers.add_parser(
        "downgrade", help="downgrade v1 JSON to v2 SRT plus sidecar"
    )
    downgrade.add_argument("--input", required=True)
    downgrade.add_argument("--out", required=True)
    downgrade.add_argument("--sidecar")
    downgrade.add_argument("--report")
    downgrade.set_defaults(func=command_downgrade)

    verify = subparsers.add_parser("verify-wav", help="verify 16 kHz mono PCM_16 WAV")
    verify.add_argument("--wav", required=True)
    verify.add_argument("--report")
    verify.set_defaults(func=command_verify_wav)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        _print_json(
            {
                "command": getattr(args, "command", None),
                "failure_list": [],
                "interrupted": True,
            }
        )
        return 130
    except Exception as exc:
        failure = FailureRecord(
            getattr(args, "command", "application"),
            str(getattr(args, "v1", getattr(args, "input", ""))),
            None,
            E_INTERNAL,
            type(exc).__name__,
            str(exc),
        )
        _print_json(
            {
                "command": getattr(args, "command", None),
                "failure_list": [failure.to_dict()],
                "business_codes": [E_INTERNAL],
            }
        )
        return 1
