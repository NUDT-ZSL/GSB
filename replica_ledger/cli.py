"""Command-line interface for the replica ledger component."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Any, Sequence

from .analysis import analyze_local
from .anomalies import corrupt_shard_digest, parse_target, truncate_block_file
from .demo import run_demo, write_demo_outputs
from .errors import (
    E_BLOCK,
    E_GRID,
    E_INTEGRITY,
    E_LEDGER,
    E_PROTOCOL,
    E_SPEC,
    GridError,
    LedgerDomainError,
    ProtocolError,
)
from .fixed import validate_scale
from .grid import diagnose_grid_local
from .ledger import LedgerStore
from .protocol import NodeServer
from .samples import generate_samples
from .spec import load_replica_group
from .util import JsonlEventLog, atomic_write_json, safe_id_component


EXIT_CODES = {
    E_SPEC: 2,
    E_BLOCK: 3,
    E_INTEGRITY: 4,
    E_GRID: 5,
    E_PROTOCOL: 6,
    E_LEDGER: 7,
}


def _parse_scale(text: str | int) -> int:
    try:
        value = int(text)
    except (TypeError, ValueError) as exc:
        raise GridError(
            "定点缩放因子必须是正整数",
            "session.scale",
            details={"actual": repr(text)},
        ) from exc
    return validate_scale(value)


def _parse_scales(text: str) -> list[int]:
    values = []
    for part in text.split(","):
        part = part.strip()
        if part:
            values.append(_parse_scale(part))
    if not values:
        raise GridError("至少需要一个定点格度", "session.scales")
    return values


def _logger(args: argparse.Namespace) -> JsonlEventLog:
    path = getattr(args, "log", None) or "logs/structured.jsonl"
    return JsonlEventLog(path)


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _summary(result: Any) -> dict[str, Any]:
    return {
        "group_id": result.spec.group_id,
        "scale": result.scale,
        "spec_digest": result.spec.spec_digest,
        "group_state_digest": result.group_state_digest,
        "blocks": [
            {
                "block_id": block_id,
                "replica_count": decision.replica_count,
                "truth_nodes": list(decision.truth_nodes),
                "mismatch_nodes": list(decision.mismatch_nodes),
                "truth_digest": decision.truth_digest,
                "strict_majority": decision.strict_majority,
                "ratio_fixed": decision.ratio.ratio.value,
                "threshold_fixed": decision.ratio.threshold.value,
                "scale": decision.ratio.scale,
                "ratio_gap_units": decision.ratio.gap_units,
                "ratio_error_bound_units": decision.ratio.gap_error_bound_units,
                "alert_certainty": decision.ratio.certainty,
            }
            for block_id, decision in sorted(result.consensus.decisions.items())
        ],
        "repair_tasks": [task.to_dict() for task in result.plan.tasks],
        "cost_summary": result.plan.summary,
    }


def _apply_injections(
    args: argparse.Namespace, logger: JsonlEventLog
) -> tuple[Any, list[dict[str, object]]]:
    spec = load_replica_group(args.spec, data_root=args.data_root)
    injected: list[dict[str, object]] = []
    if args.inject_block_truncate:
        injected.append(
            truncate_block_file(
                spec, parse_target(args.inject_block_truncate), logger=logger
            )
        )
    if args.inject_digest_corrupt:
        injected.append(
            corrupt_shard_digest(
                spec.source_path,
                spec,
                parse_target(args.inject_digest_corrupt),
                logger=logger,
            )
        )
        spec = load_replica_group(args.spec, data_root=args.data_root)
    return spec, injected


def command_generate(args: argparse.Namespace) -> int:
    index = generate_samples(args.output, force=args.force)
    _print(index)
    return 0


def command_validate(args: argparse.Namespace) -> int:
    logger = _logger(args)
    spec = load_replica_group(args.spec, data_root=args.data_root)
    result = {
        "ok": True,
        "group_id": spec.group_id,
        "spec_digest": spec.spec_digest,
        "node_count": len(spec.nodes),
        "block_ids": list(spec.block_ids),
        "checks": [check.to_dict() for check in spec.checks],
    }
    logger.emit(
        "SPEC_VALIDATED",
        node_id="*",
        block_id="*",
        field_path="spec",
        context={"group_id": spec.group_id, "spec_digest": spec.spec_digest},
    )
    _print(result)
    return 0


def command_check(args: argparse.Namespace) -> int:
    logger = _logger(args)
    if args.inject_grid:
        _parse_scale("0")
    scale = _parse_scale(args.scale)
    spec, injected = _apply_injections(args, logger)
    result = analyze_local(spec, scale, logger=logger)
    output: dict[str, Any] = {
        "ok": True,
        "injected": injected,
        **_summary(result),
    }
    if args.grid_scales:
        output["grid_diagnostics"] = diagnose_grid_local(
            spec, _parse_scales(args.grid_scales), logger=logger
        )
    _print(output)
    return 0


def command_grid(args: argparse.Namespace) -> int:
    logger = _logger(args)
    spec = load_replica_group(args.spec, data_root=args.data_root)
    result = diagnose_grid_local(
        spec,
        _parse_scales(args.scales),
        logger=logger,
    )
    _print(result)
    return 0


def command_serve(args: argparse.Namespace) -> int:
    logger = _logger(args)
    spec = load_replica_group(args.spec, data_root=args.data_root)
    server = NodeServer(
        spec,
        args.node_id,
        host=args.host,
        port=_parse_port(args.port),
        log_path=args.log,
    )
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    pid_path = run_dir / f"node-{safe_id_component(args.node_id)}.json"
    server.start()
    atomic_write_json(
        pid_path,
        {
            "pid": os.getpid(),
            "node_id": args.node_id,
            "host": server.host,
            "port": server.port,
            "spec": str(Path(args.spec).resolve()),
            "pid_file": str(pid_path.resolve()),
        },
    )
    stop_event = threading.Event()

    def request_stop(signum: int, frame: Any) -> None:
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
    except (ValueError, OSError):
        pass
    _print(
        {
            "ok": True,
            "node_id": args.node_id,
            "host": server.host,
            "port": server.port,
            "pid": os.getpid(),
            "pid_file": str(pid_path),
            "recovery_mode": server.recovery["mode"],
            "exit_report": False,
        }
    )
    try:
        while not stop_event.wait(timeout=0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        try:
            pid_path.unlink()
        except OSError:
            pass
    return 0


def _parse_port(value: str | int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolError("端口必须是整数", "protocol.port") from exc
    if port < 0 or port > 65535:
        raise ProtocolError("端口必须在 0..65535", "protocol.port")
    return port


def command_port_check(args: argparse.Namespace) -> int:
    requested = [int(part) for part in args.ports.split(",") if part.strip()]
    results = []
    for port in requested:
        if port <= 0 or port > 65535:
            results.append({"port": port, "bindable": False, "error": "invalid"})
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            results.append({"port": port, "bindable": True})
        except OSError as exc:
            results.append(
                {"port": port, "bindable": False, "error": str(exc)}
            )
        finally:
            sock.close()
    output = {
        "ok": all(item["bindable"] for item in results),
        "host": "127.0.0.1",
        "ports": results,
    }
    _print(output)
    return 0 if output["ok"] else EXIT_CODES[E_PROTOCOL]


def command_demo(args: argparse.Namespace) -> int:
    report = run_demo(args.workdir)
    write_demo_outputs(report, json_path=args.report_json, text_path=args.report_text)
    _print(
        {
            "ok": True,
            "report_digest": report["report_digest"],
            "experiments": report["experiment_order"],
            "invariants": report["invariants"],
            "report_json": str(Path(args.report_json)),
            "report_text": str(Path(args.report_text)),
            "workdir": str(Path(args.workdir)),
        }
    )
    return 0


def command_gui(args: argparse.Namespace) -> int:
    from .gui import launch_gui

    return launch_gui(spec_path=args.spec, scale=_parse_scale(args.scale), log_path=args.log)


def command_ledger_open(args: argparse.Namespace) -> int:
    store = LedgerStore(args.json, args.sqlite)
    view = store.open(
        expected_group_state_digest=args.expected_state_digest,
        expected_format_version=args.expected_format_version,
    )
    _print(view.to_dict())
    return 0


def _add_common_spec(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--spec", required=True, help="replica group JSON path")
    parser.add_argument("--data-root", default=None, help="override relative data root")


def _add_log(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--log",
        default="logs/structured.jsonl",
        help="JSONL structured log path",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="replica-ledger",
        description="Distributed replica repair ledger over loopback JSONL TCP",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic samples")
    generate.add_argument("--output", default="samples")
    generate.add_argument("--force", action="store_true")
    generate.set_defaults(func=command_generate)

    validate = subparsers.add_parser("validate", help="strictly validate spec JSON")
    _add_common_spec(validate)
    _add_log(validate)
    validate.set_defaults(func=command_validate)

    check = subparsers.add_parser("check", help="analyse blocks and optional anomalies")
    _add_common_spec(check)
    check.add_argument("--scale", default="8", help="positive integer fixed-point scale")
    check.add_argument("--grid-scales", default=None, help="comma-separated scales")
    check.add_argument(
        "--inject-block-truncate",
        metavar="NODE:BLOCK",
        default=None,
        help="mutate the input block file to trigger E_BLOCK",
    )
    check.add_argument(
        "--inject-digest-corrupt",
        metavar="NODE:BLOCK",
        default=None,
        help="mutate the input spec shard digest to trigger E_INTEGRITY",
    )
    check.add_argument(
        "--inject-grid",
        action="store_true",
        help="use an invalid grid parameter to trigger E_GRID",
    )
    _add_log(check)
    check.set_defaults(func=command_check)

    grid = subparsers.add_parser("grid", help="diagnose fixed-point grid flips")
    _add_common_spec(grid)
    grid.add_argument("--scales", default="3,4,6,8,16")
    _add_log(grid)
    grid.set_defaults(func=command_grid)

    serve = subparsers.add_parser("serve", help="run one loopback node server")
    _add_common_spec(serve)
    serve.add_argument("--node-id", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default="19101")
    serve.add_argument("--run-dir", default="run")
    _add_log(serve)
    serve.set_defaults(func=command_serve)

    port_check = subparsers.add_parser("port-check", help="verify loopback ports")
    port_check.add_argument("--ports", default="19101,19102,19103")
    port_check.set_defaults(func=command_port_check)

    demo = subparsers.add_parser("demo", help="run the three end-to-end demos")
    demo.add_argument("--workdir", default="run/demo")
    demo.add_argument("--report-json", default="reports/demo_report.json")
    demo.add_argument("--report-text", default="reports/demo_report.txt")
    demo.set_defaults(func=command_demo)

    gui = subparsers.add_parser("gui", help="launch the Tkinter diagnostic UI")
    gui.add_argument("--spec", default="samples/scenarios/grid/spec.json")
    gui.add_argument("--scale", default="8")
    _add_log(gui)
    gui.set_defaults(func=command_gui)

    ledger_open = subparsers.add_parser("ledger-open", help="open and verify a ledger")
    ledger_open.add_argument("--json", required=True)
    ledger_open.add_argument("--sqlite", required=True)
    ledger_open.add_argument("--expected-state-digest", default=None)
    ledger_open.add_argument("--expected-format-version", type=int, default=1)
    ledger_open.set_defaults(func=command_ledger_open)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = _logger(args) if hasattr(args, "log") else None
    try:
        return int(args.func(args))
    except LedgerDomainError as exc:
        if logger is not None:
            logger.error(exc, event=f"ANOMALY_{exc.code}")
        payload = {"ok": False, "error": exc.to_dict()}
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)
        return EXIT_CODES.get(exc.code, 1)
    except Exception as exc:
        code = "E_INTERNAL"
        error = {
            "code": code,
            "message": str(exc),
            "field_path": "cli",
            "node_id": "*",
            "block_id": "*",
            "details": {"error_type": type(exc).__name__},
        }
        if logger is not None:
            logger.emit(
                "INTERNAL_ERROR",
                level="ERROR",
                code=code,
                message=str(exc),
                field_path="cli",
                node_id="*",
                block_id="*",
                context={"error_type": type(exc).__name__},
            )
        print(json.dumps({"ok": False, "error": error}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
