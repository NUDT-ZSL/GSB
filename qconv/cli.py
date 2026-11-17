"""Command line and interactive REPL for the quantized convolution workbench."""

from __future__ import annotations

import argparse
import json
import shlex
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from .engine import QConvEngine
from .errors import Failure, QConvError
from .sample import DEFAULT_SEED, generate_samples
from .util import atomic_write_json


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--samples", type=str, default=None, help="sample workspace directory")
    parser.add_argument("--graph", type=str, default=None, help="graph JSON path")
    parser.add_argument("--packs", type=str, default=None, help="pack manifest path")
    parser.add_argument("--schedule", type=str, default=None, help="schedule config path")
    parser.add_argument("--injection", type=str, default=None, help="injection config path")
    parser.add_argument("--session", type=str, default=".qconv-session", help="session directory")
    parser.add_argument(
        "--no-resume-working-graph",
        action="store_true",
        help="ignore a session's persisted edited graph",
    )


def _resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path | None]:
    if args.samples:
        base = Path(args.samples)
        graph = Path(args.graph) if args.graph else base / "graph.json"
        packs = Path(args.packs) if args.packs else base / "packs.json"
        schedule = Path(args.schedule) if args.schedule else base / "schedule.json"
        injection = Path(args.injection) if args.injection else None
    else:
        if not args.graph or not args.packs or not args.schedule:
            raise SystemExit("provide --samples or all of --graph/--packs/--schedule")
        graph = Path(args.graph)
        packs = Path(args.packs)
        schedule = Path(args.schedule)
        injection = Path(args.injection) if args.injection else None
    return graph, packs, schedule, injection


def _install_stop_handlers(stop_event: threading.Event) -> None:
    def handler(signum: int, frame: Any) -> None:
        stop_event.set()
        print(f"qconv: received signal {signum}; finishing current node(s)", file=sys.stderr)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        value = getattr(signal, name, None)
        if value is not None:
            try:
                signal.signal(value, handler)
            except (OSError, ValueError):
                pass


def _print_run_summary(report: dict[str, Any]) -> None:
    summary = {
        "status": report.get("status"),
        "graph_version": report.get("graph_version"),
        "config_hash": report.get("config_hash"),
        "recomputed_nodes": report.get("recomputed_nodes", []),
        "reused_nodes": report.get("reused_nodes", []),
        "failures": [item.get("code") for item in report.get("failures", [])],
        "snapshot_version": report.get("snapshot", {}).get("version"),
        "budget_remaining": report.get("budget", {}).get("remaining"),
        "interrupted": report.get("interrupted", False),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


def _write_fatal_report(path: Path | None, failure: Failure) -> None:
    if path is None:
        return
    atomic_write_json(
        path,
        {
            "schema_version": 1,
            "status": "failed",
            "failures": [failure.to_dict()],
            "nodes": [],
            "recomputed_nodes": [],
            "reused_nodes": [],
        },
    )


def command_run(args: argparse.Namespace) -> int:
    graph, packs, schedule, injection = _resolve_paths(args)
    report_path = Path(args.report) if args.report else Path(args.session) / "report.json"
    stop_event = threading.Event()
    _install_stop_handlers(stop_event)
    try:
        engine = QConvEngine(
            graph,
            packs,
            schedule,
            args.session,
            injection,
            resume_working_graph=not args.no_resume_working_graph,
        )
        report = engine.run(report_path=report_path, stop_event=stop_event)
        if args.export_outputs:
            engine.export_outputs(args.export_outputs)
    except QConvError as exc:
        failure = exc.to_failure()
        _write_fatal_report(report_path, failure)
        print(json.dumps({"status": "failed", "failures": [failure.to_dict()]}, ensure_ascii=False))
        return 2
    _print_run_summary(report)
    if report.get("interrupted"):
        return 0
    return 0 if report.get("status") == "success" else 2


def _repl_help() -> str:
    return """commands:
  run [REPORT_PATH]                         execute and write a JSON report
  set-bitwidth NODE weight|activation BITS  edit a quantization bit width
  set-scale NODE weight|activation VALUE    edit a finite positive scale
  set-quantization on|off                   toggle integer scaling mode
  replace-subgraph NODE PATCH_JSON          replace node and descendants
  save-graph PATH                           persist the current edited graph
  export-outputs DIRECTORY                  write each node output as .npy
  show                                      print the normalized graph
  help                                      show this help
  exit                                      leave the REPL
"""


def _run_repl(args: argparse.Namespace) -> int:
    graph, packs, schedule, injection = _resolve_paths(args)
    stop_event = threading.Event()
    _install_stop_handlers(stop_event)
    engine = QConvEngine(
        graph,
        packs,
        schedule,
        args.session,
        injection,
        resume_working_graph=not args.no_resume_working_graph,
    )
    default_report = str(Path(args.session) / "report.json")
    if args.script:
        lines = Path(args.script).read_text(encoding="utf-8").splitlines()
        interactive = False
    else:
        lines = []
        interactive = True
        print("qconv REPL; type `help` for commands")
    while True:
        if not interactive:
            if not lines:
                break
            line = lines.pop(0)
            print(f"qconv> {line}")
        else:
            if stop_event.is_set():
                break
            try:
                line = input("qconv> ")
            except (EOFError, KeyboardInterrupt):
                break
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            tokens = shlex.split(stripped)
            command = tokens[0].lower()
            if command in {"exit", "quit"}:
                break
            if command == "help":
                print(_repl_help(), end="")
            elif command == "run":
                report_path = tokens[1] if len(tokens) > 1 else default_report
                report = engine.run(report_path=report_path, stop_event=stop_event)
                _print_run_summary(report)
                if stop_event.is_set():
                    break
            elif command in {"set-bitwidth", "set-width"}:
                if len(tokens) != 4:
                    raise ValueError("usage: set-bitwidth NODE weight|activation BITS")
                affected = engine.set_bit_width(tokens[1], tokens[2], int(tokens[3]))
                print(json.dumps({"invalidated_nodes": affected}, ensure_ascii=False))
            elif command == "set-scale":
                if len(tokens) != 4:
                    raise ValueError("usage: set-scale NODE weight|activation VALUE")
                affected = engine.set_scale(tokens[1], tokens[2], float(tokens[3]))
                print(json.dumps({"invalidated_nodes": affected}, ensure_ascii=False))
            elif command == "set-quantization":
                if len(tokens) != 2 or tokens[1] not in {"on", "off"}:
                    raise ValueError("usage: set-quantization on|off")
                affected = engine.set_quantization_enabled(tokens[1] == "on")
                print(json.dumps({"invalidated_nodes": affected}, ensure_ascii=False))
            elif command in {"replace-subgraph", "replace"}:
                if len(tokens) != 3:
                    raise ValueError("usage: replace-subgraph NODE PATCH_JSON")
                patch = json.loads(Path(tokens[2]).read_text(encoding="utf-8"))
                affected = engine.replace_subgraph(tokens[1], patch)
                print(json.dumps({"invalidated_nodes": affected}, ensure_ascii=False))
            elif command in {"save-graph", "save"}:
                if len(tokens) != 2:
                    raise ValueError("usage: save-graph PATH")
                engine.save_graph(tokens[1])
                print(json.dumps({"saved": tokens[1]}))
            elif command in {"export-outputs", "export"}:
                if len(tokens) != 2:
                    raise ValueError("usage: export-outputs DIRECTORY")
                manifest = engine.export_outputs(tokens[1])
                print(json.dumps({"exported_nodes": sorted(manifest["nodes"])}))
            elif command == "show":
                graph_obj = engine.graph
                print(json.dumps(graph_obj.to_dict() if graph_obj else None, indent=2, ensure_ascii=False))
            else:
                raise ValueError(f"unknown command: {tokens[0]}")
        except (QConvError, OSError, ValueError, json.JSONDecodeError) as exc:
            code = exc.code if isinstance(exc, QConvError) else type(exc).__name__
            print(json.dumps({"command_error": code, "message": str(exc)}, ensure_ascii=False))
            if not interactive:
                return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qconv", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate-samples", help="write deterministic local samples")
    generate.add_argument("--out", required=True, help="destination directory")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)

    run = subparsers.add_parser("run", help="run the operator graph once")
    _add_common_arguments(run)
    run.add_argument("--report", default=None, help="JSON report path")
    run.add_argument("--export-outputs", default=None, help="directory for .npy outputs")

    repl = subparsers.add_parser("repl", help="edit graph parameters and re-run incrementally")
    _add_common_arguments(repl)
    repl.add_argument("--script", default=None, help="optional command script file")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "generate-samples":
        result = generate_samples(args.out, seed=args.seed)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "run":
        return command_run(args)
    if args.command == "repl":
        try:
            return _run_repl(args)
        except QConvError as exc:
            print(json.dumps({"status": "failed", "failure": exc.to_failure().to_dict()}, ensure_ascii=False))
            return 2
    parser.error("unknown command")
    return 2

