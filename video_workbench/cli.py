from __future__ import annotations

import argparse
import copy
import json
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from .config import command_merge, load_config_file, validate_config
from .errors import WorkbenchError
from .interactive import InteractiveController
from .pipeline import RenderResult, Renderer
from .util import json_safe, read_json


def _print_json(value: Any) -> None:
    print(json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True), flush=True)


def _result_payload(result: RenderResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "business_codes": result.business_codes,
        "primary_business_code": result.primary_code,
        "snapshot_version": result.snapshot_version,
        "report_path": result.report_path,
        "manifest_path": result.manifest_path,
        "frames": {
            "shape": list(result.frames.shape),
            "valid_frame_count": int(result.frame_valid_mask.sum()),
            "fingerprint": result.summary.get("frame_fingerprint"),
        },
        "preview": result.preview,
        "reused_stages": result.reused_stages,
        "invalidated_stages": result.invalidated_stages,
        "recomputed_stages": result.recomputed_stages,
        "recovery": result.recovery,
        "failure_manifest": result.failures,
        "summary": result.summary,
    }


def _install_signal_handlers(stop_event: threading.Event) -> dict[int, Any]:
    previous: dict[int, Any] = {}

    def handler(signum: int, _frame: Any) -> None:
        stop_event.set()
        print(json.dumps({"event": "graceful_exit_requested", "signal": signum}, sort_keys=True), file=sys.stderr, flush=True)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handler)
        except (ValueError, OSError):
            pass
    return previous


def _restore_signal_handlers(previous: Mapping[int, Any]) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):
            pass


def command_generate_samples(args: argparse.Namespace) -> int:
    from scripts.generate_samples import generate_all

    _print_json(generate_all())
    return 0


def command_render(args: argparse.Namespace) -> int:
    raw, base_dir = load_config_file(args.config, args.base_dir)
    raw = copy.deepcopy(raw)
    if args.output_dir:
        raw["output_dir"] = args.output_dir
    if args.set_json:
        updates = json.loads(args.set_json)
        if not isinstance(updates, dict):
            raise SystemExit("--set-json must decode to an object")
        raw = command_merge(raw, updates)
    stop_event = threading.Event()
    previous = _install_signal_handlers(stop_event)
    try:
        result = Renderer(base_dir=base_dir).render(
            raw,
            allow_reuse=not args.no_reuse,
            serial=args.serial,
            stop_event=stop_event,
        )
    finally:
        _restore_signal_handlers(previous)
    _print_json(_result_payload(result))
    if result.status in ("completed", "partial", "interrupted"):
        return 0
    return 2


def command_validate(args: argparse.Namespace) -> int:
    try:
        raw, base_dir = load_config_file(args.config, args.base_dir)
        if args.output_dir:
            raw["output_dir"] = args.output_dir
        config = validate_config(raw, base_dir)
    except WorkbenchError as exc:
        _print_json({"ok": False, "error": exc.as_dict()})
        return 2
    _print_json(
        {
            "ok": True,
            "mode": config.mode,
            "config_fingerprint": config.config_fingerprint,
            "stage_graph": config.stage_graph(),
            "noise_shape": list(config.noise_table.shape),
            "init_image_fingerprint": None if config.init_image is None else config.init_image.fingerprint,
            "patch_fingerprint": None if config.patch is None else config.patch.fingerprint,
        }
    )
    return 0


def command_interactive(args: argparse.Namespace) -> int:
    payload = read_json(args.commands)
    if isinstance(payload, list):
        commands = payload
        base_config_path = args.base_config
    elif isinstance(payload, dict):
        commands = payload.get("commands", [])
        base_config_path = args.base_config or payload.get("base_config")
    else:
        raise SystemExit("command stream must be a JSON object or list")
    if not isinstance(commands, list):
        raise SystemExit("command stream 'commands' must be a list")
    if not base_config_path:
        raise SystemExit("a base configuration is required")
    raw, base_dir = load_config_file(base_config_path, args.base_dir)
    raw = copy.deepcopy(raw)
    if args.output_dir:
        raw["output_dir"] = args.output_dir
    if args.budget_ms is not None:
        raw["preview_budget_ms"] = args.budget_ms
    stop_event = threading.Event()
    previous = _install_signal_handlers(stop_event)
    controller = InteractiveController(
        raw,
        base_dir=base_dir,
        on_response=_print_json,
        stop_event=stop_event,
    )
    try:
        for index, command in enumerate(commands):
            if stop_event.is_set():
                break
            if not isinstance(command, dict):
                raise SystemExit(f"command {index} must be an object")
            delay_ms = command.get("delay_before_ms", args.command_interval_ms)
            if isinstance(delay_ms, (int, float)) and not isinstance(delay_ms, bool) and delay_ms > 0:
                time.sleep(float(delay_ms) / 1000.0)
            controller.submit(command, preview_budget_ms=args.budget_ms)
        if not args.no_wait:
            results = controller.wait_all()
            _print_json(
                {
                    "event": "interactive_finished",
                    "statuses": [
                        {
                            "status": result.status,
                            "business_codes": result.business_codes,
                            "snapshot_version": result.snapshot_version,
                            "report_path": result.report_path,
                        }
                        for result in results
                    ],
                }
            )
    finally:
        controller.close()
        _restore_signal_handlers(previous)
    return 0


def command_status(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    manifest_path = workspace / "manifest.json"
    report_path = workspace / "diagnostic_report.json"
    payload: dict[str, Any] = {
        "workspace": str(workspace),
        "manifest_exists": manifest_path.exists(),
        "report_exists": report_path.exists(),
    }
    if manifest_path.exists():
        payload["manifest"] = read_json(manifest_path)
    if report_path.exists() and args.include_report:
        payload["report"] = read_json(report_path)
    _print_json(payload)
    return 0


def command_check_injections(args: argparse.Namespace) -> int:
    payload = read_json(args.cases)
    base_config_path = payload.get("base_config")
    cases = payload.get("cases")
    if not isinstance(base_config_path, str) or not isinstance(cases, list):
        raise SystemExit("injection file must contain base_config and cases")
    raw, base_dir = load_config_file(base_config_path, args.base_dir)
    results = []
    failures = 0
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise SystemExit("each injection case must be an object")
        candidate = command_merge(raw, case.get("set", {}))
        candidate["output_dir"] = str(Path(args.output_root).resolve() / f"case_{index:02d}_{case.get('id', 'case')}")
        result = Renderer(base_dir=base_dir).render(candidate, allow_reuse=False)
        expected = case.get("expected_business_code")
        passed = expected == result.primary_code or (isinstance(expected, str) and expected in result.business_codes)
        if not passed:
            failures += 1
        results.append(
            {
                "id": case.get("id"),
                "expected_business_code": expected,
                "actual_business_codes": result.business_codes,
                "status": result.status,
                "passed": passed,
                "report_path": result.report_path,
                "failure_count": len(result.failures),
            }
        )
    _print_json({"total": len(results), "failed": failures, "results": results})
    return 0 if failures == 0 else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="video-workbench", description="Local deterministic T2V/I2V rendering workbench")
    parser.add_argument("--base-dir", default=None, help="base directory for relative paths (defaults to CWD)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    samples = subparsers.add_parser("generate-samples", help="generate deterministic local sample inputs")
    samples.set_defaults(func=command_generate_samples)

    render = subparsers.add_parser("render", help="run one full or incremental render")
    render.add_argument("--config", required=True)
    render.add_argument("--output-dir", default=None)
    render.add_argument("--set-json", default=None, help="command-style JSON object of top-level config edits")
    render.add_argument("--no-reuse", action="store_true", help="ignore all prior stage artifacts")
    render.add_argument("--serial", action="store_true", help="force max_workers=1")
    render.set_defaults(func=command_render)

    validate = subparsers.add_parser("validate", help="validate a config and print its stage graph")
    validate.add_argument("--config", required=True)
    validate.add_argument("--output-dir", default=None)
    validate.set_defaults(func=command_validate)

    interactive = subparsers.add_parser("interactive", help="replay a JSON command stream with preview-budget responses")
    interactive.add_argument("--commands", required=True)
    interactive.add_argument("--base-config", default=None)
    interactive.add_argument("--output-dir", default=None)
    interactive.add_argument("--budget-ms", type=int, default=None)
    interactive.add_argument("--command-interval-ms", type=int, default=0)
    interactive.add_argument("--no-wait", action="store_true", help="return after command responses without waiting for background renders")
    interactive.set_defaults(func=command_interactive)

    status = subparsers.add_parser("status", help="print manifest and optionally diagnostic report")
    status.add_argument("--workspace", required=True)
    status.add_argument("--include-report", action="store_true")
    status.set_defaults(func=command_status)

    injections = subparsers.add_parser("check-injections", help="run sample validation/failure cases")
    injections.add_argument("--cases", default="samples/injection_cases.json")
    injections.add_argument("--output-root", default="out/injection-check")
    injections.set_defaults(func=command_check_injections)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except WorkbenchError as exc:
        _print_json({"ok": False, "error": exc.as_dict()})
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        _print_json({"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
