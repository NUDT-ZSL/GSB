"""Local command-line entry point for convedit."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import parse_config
from .engine import Engine
from .errors import BusinessError, E_GRAPH_INVALID, E_IO
from .graph import load_graph_file, validate_graph
from .injections import load_injections
from .inputs import generate_input, load_input_descriptor
from .reports import write_report
from .runtime import StopController
from .state import StateStore
from .util import load_json, sha256_json


SAMPLE_ROOT = Path(__file__).resolve().parent.parent / "samples"


def _read_json(path: Path) -> Any:
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(E_IO, f"cannot read JSON file {path}: {exc}", stage="cli") from exc


def _error_report(error: BusinessError, *, graph_version: int | None = None) -> dict[str, Any]:
    failure = error.to_failure()
    return {
        "report_version": 1,
        "status": "error",
        "exit_code": 2,
        "graph_version": graph_version,
        "business_codes": [error.code],
        "failures": [failure],
        "recalculated_nodes": [],
        "reused_nodes": [],
        "invalidated_nodes": [],
        "invalidation_chains": {},
        "removed_nodes": [],
        "injections_hit": [],
    }


def _write_and_print(report_path: Path, report: dict[str, Any]) -> None:
    write_report(report_path, report)
    summary = {
        "status": report.get("status"),
        "exit_code": report.get("exit_code"),
        "graph_version": report.get("graph_version"),
        "business_codes": report.get("business_codes", []),
        "recalculated_nodes": report.get("recalculated_nodes", []),
        "reused_nodes": report.get("reused_nodes", []),
        "report": str(report_path),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


def _injections_from_args(args: argparse.Namespace):
    path = Path(args.injections) if getattr(args, "injections", None) else None
    return load_injections(
        path,
        enable_ids=getattr(args, "enable_injection", []) or [],
        disable_ids=getattr(args, "disable_injection", []) or [],
    )


def _load_config_for_state(store: StateStore, path: str | None, snapshot: dict[str, Any]):
    if path:
        raw = _read_json(Path(path))
        if sha256_json(raw) != snapshot.get("config_hash"):
            store.verify_resume(snapshot, config_raw=raw)
        return raw
    return store.load_config()


def _invalid_graph_report(validation, *, graph_version: int, path: Path) -> int:
    report = {
        "report_version": 1,
        "status": "invalid_graph",
        "exit_code": 2,
        "graph_version": graph_version,
        "topology": [],
        "nodes": [],
        "recalculated_nodes": [],
        "reused_nodes": [],
        "invalidated_nodes": [],
        "invalidation_chains": {},
        "removed_nodes": [],
        "failures": validation.failures,
        "business_codes": sorted({str(item.get("code")) for item in validation.failures}),
        "injections_hit": [],
    }
    _write_and_print(path, report)
    return 2


def cmd_validate(args: argparse.Namespace) -> int:
    validation = load_graph_file(Path(args.graph))
    if args.config:
        try:
            parse_config(_read_json(Path(args.config)))
        except BusinessError as exc:
            validation.failures.append(exc.to_failure())
    report = {
        "report_version": 1,
        "status": "valid" if validation.ok else "invalid",
        "exit_code": 0 if validation.ok else 2,
        "graph_fingerprint": validation.graph.fingerprint if validation.graph else None,
        "topology": validation.graph.topology_records() if validation.graph else [],
        "failures": validation.failures,
        "business_codes": sorted({str(item.get("code")) for item in validation.failures}),
    }
    if args.report:
        write_report(Path(args.report), report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if validation.ok else 2


def cmd_init(args: argparse.Namespace) -> int:
    graph_raw = _read_json(Path(args.graph))
    validation = validate_graph(graph_raw)
    if not validation.ok or validation.graph is None:
        report_path = Path(args.report) if args.report else Path(args.state) / "init_failure.json"
        return _invalid_graph_report(validation, graph_version=0, path=report_path)
    config_raw = _read_json(Path(args.config))
    parse_config(config_raw)
    descriptor = load_input_descriptor(Path(args.input))
    generated = generate_input(descriptor, validation.graph)
    store = StateStore(Path(args.state))
    with store.lock():
        snapshot = store.initialize(
            graph_raw=graph_raw,
            config_raw=config_raw,
            input_descriptor_raw=descriptor.raw,
            input_array=generated.tensor,
            graph_version=args.graph_version,
        )
    report = {
        "report_version": 1,
        "status": "initialized",
        "exit_code": 0,
        "graph_version": snapshot["graph_version"],
        "graph_fingerprint": snapshot["graph_fingerprint"],
        "config_hash": snapshot["config_hash"],
        "input_fingerprint": snapshot["input_fingerprint"],
        "input": snapshot["input_summary"],
        "topology": validation.graph.topology_records(),
        "business_codes": [],
        "failures": [],
        "state": str(store.root),
    }
    if args.report:
        write_report(Path(args.report), report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def _run_state(args: argparse.Namespace, *, resume_only: bool = False) -> int:
    store = StateStore(Path(args.state))
    with store.lock():
        snapshot = store.load()
        config_raw = _load_config_for_state(store, getattr(args, "config", None), snapshot)
        if getattr(args, "graph", None):
            graph_raw = _read_json(Path(args.graph))
            store.verify_resume(snapshot, graph_raw=graph_raw)
        if getattr(args, "expected_version", None) is not None:
            store.verify_resume(snapshot, graph_version=args.expected_version)
        injections = _injections_from_args(args)
        engine = Engine(store, config_raw, injections)
        controller = StopController()
        report_path = Path(args.report)
        try:
            with controller.installed_handlers():
                outcome = engine.run(snapshot, stop_controller=controller, full_recompute=args.full)
        except BusinessError as exc:
            report = _error_report(exc, graph_version=snapshot.get("graph_version"))
            _write_and_print(report_path, report)
            return 2
    report = outcome.report
    if resume_only:
        report["resume"] = True
    _write_and_print(report_path, report)
    return int(report.get("exit_code", 0))


def cmd_run(args: argparse.Namespace) -> int:
    return _run_state(args)


def cmd_resume(args: argparse.Namespace) -> int:
    return _run_state(args, resume_only=True)


def _load_operations(path: Path) -> list[dict[str, Any]]:
    raw = _read_json(path)
    if isinstance(raw, list):
        operations = raw
    elif isinstance(raw, dict) and isinstance(raw.get("operations"), list):
        operations = raw["operations"]
    elif isinstance(raw, dict):
        operations = [raw]
    else:
        raise BusinessError(E_GRAPH_INVALID, "edit file must contain an operation or operations array", stage="edit")
    if not operations or any(not isinstance(item, dict) for item in operations):
        raise BusinessError(E_GRAPH_INVALID, "edit operations must be a non-empty array of objects", stage="edit")
    return operations


def cmd_edit(args: argparse.Namespace) -> int:
    store = StateStore(Path(args.state))
    report_dir = Path(args.report_dir) if args.report_dir else store.root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    operations = _load_operations(Path(args.edit_file))
    if args.index is not None:
        if not 1 <= args.index <= len(operations):
            raise BusinessError(
                E_GRAPH_INVALID,
                f"--index must be in [1, {len(operations)}]",
                stage="edit",
            )
        selected = [(args.index - 1, operations[args.index - 1])]
    else:
        selected = list(enumerate(operations))
    overall_code = 0
    with store.lock():
        snapshot = store.load()
        config_raw = _load_config_for_state(store, args.config, snapshot)
        injections = _injections_from_args(args)
        engine = Engine(store, config_raw, injections)
        for sequence_index, operation in selected:
            commit = engine.commit_edit(snapshot, operation)
            if not commit.committed:
                report = {
                    "report_version": 1,
                    "status": "edit_rejected",
                    "exit_code": 2,
                    "graph_version": snapshot.get("graph_version"),
                    "edit": commit.edit_info,
                    "failures": commit.failures,
                    "business_codes": sorted({str(item.get("code")) for item in commit.failures}),
                    "recalculated_nodes": [],
                    "reused_nodes": [],
                    "invalidated_nodes": [],
                    "invalidation_chains": {},
                    "removed_nodes": [],
                    "injections_hit": [],
                }
                report_path = Path(args.report) if args.report else report_dir / f"edit_rejected_{sequence_index + 1:03d}.json"
                _write_and_print(report_path, report)
                return 2
            controller = StopController()
            try:
                with controller.installed_handlers():
                    outcome = engine.run(commit.snapshot, stop_controller=controller)
            except BusinessError as exc:
                report_path = Path(args.report) if args.report else report_dir / f"graph_v{commit.edit_info['to_version']:04d}.json"
                _write_and_print(report_path, _error_report(exc, graph_version=commit.edit_info["to_version"]))
                return 2
            report = outcome.report
            report["edit"] = commit.edit_info
            report["edit_commit"] = {
                "invalidated_nodes": commit.invalidated_nodes,
                "reused_nodes": commit.reused_nodes,
                "removed_nodes": commit.removed_nodes,
                "invalidation_chains": commit.invalidation_chains,
            }
            if args.report and len(selected) == 1:
                report_path = Path(args.report)
            else:
                report_path = report_dir / f"graph_v{commit.edit_info['to_version']:04d}.json"
            _write_and_print(report_path, report)
            snapshot = outcome.snapshot
            if outcome.stop_requested:
                return 0
            if report.get("status") == "completed_with_failures":
                overall_code = 2
                if args.stop_on_failure:
                    return 2
    return overall_code


def _operation_from_shell(command: str, parts: list[str]) -> dict[str, Any] | None:
    if command in {"update-params", "update_params"}:
        if len(parts) < 3:
            raise ValueError("usage: update-params NODE JSON [replace]")
        operation = {"op": "update_params", "node_id": parts[1], "params": json.loads(parts[2])}
        if len(parts) > 3 and parts[3] == "replace":
            operation["replace"] = True
        return operation
    if command in {"set-enabled", "enable"}:
        if len(parts) != 3:
            raise ValueError("usage: set-enabled NODE true|false")
        return {"op": "set_enabled", "node_id": parts[1], "enabled": parts[2].lower() == "true"}
    if command in {"delete-node", "delete"}:
        if len(parts) != 2:
            raise ValueError("usage: delete-node NODE")
        return {"op": "delete_node", "node_id": parts[1]}
    if command in {"replace-subgraph", "replace"}:
        if len(parts) != 2:
            raise ValueError("usage: replace-subgraph JSON")
        operation = json.loads(parts[1])
        operation.setdefault("op", "replace_subgraph")
        return operation
    if command in {"add-node", "add"}:
        if len(parts) != 2:
            raise ValueError("usage: add-node JSON")
        operation = json.loads(parts[1])
        operation.setdefault("op", "add_node")
        return operation
    if command in {"edit", "apply"}:
        if len(parts) != 2:
            raise ValueError("usage: edit JSON")
        return json.loads(parts[1])
    return None


def cmd_shell(args: argparse.Namespace) -> int:
    store = StateStore(Path(args.state))
    report_dir = store.root / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    with store.lock():
        snapshot = store.load()
        config_raw = store.load_config()
        engine = Engine(store, config_raw)
        print("convedit interactive shell. Type 'help' for commands.")
        while True:
            try:
                line = input("convedit> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not line:
                continue
            parts = line.split()
            command = parts[0].lower()
            if command in {"quit", "exit", "q"}:
                return 0
            if command in {"help", "?"}:
                print(
                    "update-params NODE JSON [replace] | set-enabled NODE BOOL | delete-node NODE\n"
                    "replace-subgraph JSON | add-node JSON | edit JSON | run | status | reuse | show"
                )
                continue
            if command == "status":
                print(
                    json.dumps(
                        {
                            "graph_version": snapshot.get("graph_version"),
                            "status": snapshot.get("status"),
                            "stage": snapshot.get("stage"),
                            "completed_nodes": snapshot.get("completed_nodes", []),
                            "failures": snapshot.get("failures", []),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                continue
            if command == "reuse":
                print(json.dumps(snapshot.get("reused_nodes", []), ensure_ascii=False))
                continue
            if command == "show":
                print(json.dumps(snapshot.get("graph"), ensure_ascii=False, sort_keys=True))
                continue
            if command == "run":
                operation = None
            else:
                try:
                    operation = _operation_from_shell(command, parts)
                except (ValueError, json.JSONDecodeError) as exc:
                    print(f"error: {exc}")
                    continue
                if operation is None:
                    print(f"unknown command: {command}")
                    continue
            if operation is not None:
                commit = engine.commit_edit(snapshot, operation)
                if not commit.committed:
                    print(
                        json.dumps(
                            {"status": "edit_rejected", "failures": commit.failures},
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
                    continue
                snapshot = commit.snapshot
            controller = StopController()
            try:
                with controller.installed_handlers():
                    outcome = engine.run(snapshot, stop_controller=controller)
            except BusinessError as exc:
                print(json.dumps(exc.to_failure(), ensure_ascii=False, sort_keys=True))
                continue
            snapshot = outcome.snapshot
            report_path = report_dir / f"shell_v{snapshot.get('graph_version'):04d}.json"
            write_report(report_path, outcome.report)
            print(
                json.dumps(
                    {
                        "status": outcome.report["status"],
                        "graph_version": outcome.report["graph_version"],
                        "recalculated": outcome.report["recalculated_nodes"],
                        "reused": outcome.report["reused_nodes"],
                        "invalidated": outcome.report["invalidated_nodes"],
                        "failures": outcome.report.get("business_codes", []),
                        "report": str(report_path),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo

    return run_demo(Path(args.output), project_root=Path(__file__).resolve().parent.parent)


def _add_injection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--injections", help="injection configuration JSON")
    parser.add_argument("--enable-injection", action="append", default=[], metavar="ID")
    parser.add_argument("--disable-injection", action="append", default=[], metavar="ID")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="convedit", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate a graph JSON file")
    validate.add_argument("--graph", required=True)
    validate.add_argument("--config")
    validate.add_argument("--report")
    validate.set_defaults(func=cmd_validate)

    init = sub.add_parser("init", help="initialize a run state from graph/input/config")
    init.add_argument("--graph", required=True)
    init.add_argument("--input", required=True)
    init.add_argument("--config", required=True)
    init.add_argument("--state", required=True)
    init.add_argument("--graph-version", type=int, default=0)
    init.add_argument("--report")
    init.set_defaults(func=cmd_init)

    run = sub.add_parser("run", help="execute or continue a run")
    run.add_argument("--state", required=True)
    run.add_argument("--report", required=True)
    run.add_argument("--config", help="must match the snapshot config hash")
    run.add_argument("--graph", help="must match the snapshot graph fingerprint")
    run.add_argument("--expected-version", type=int)
    run.add_argument("--full", action="store_true", help="discard node products and recompute all")
    _add_injection_args(run)
    run.set_defaults(func=cmd_run)

    resume = sub.add_parser("resume", help="resume an interrupted snapshot")
    resume.add_argument("--state", required=True)
    resume.add_argument("--report", required=True)
    resume.add_argument("--config", help="must match the snapshot config hash")
    resume.add_argument("--graph", help="must match the snapshot graph fingerprint")
    resume.add_argument("--expected-version", type=int)
    resume.add_argument("--full", action="store_true")
    _add_injection_args(resume)
    resume.set_defaults(func=cmd_resume)

    edit = sub.add_parser("edit", help="apply edit operations and incrementally re-execute")
    edit.add_argument("--state", required=True)
    edit.add_argument("--edit-file", required=True)
    edit.add_argument("--index", type=int, help="1-based operation index; default applies all")
    edit.add_argument("--report")
    edit.add_argument("--report-dir")
    edit.add_argument("--config")
    edit.add_argument("--stop-on-failure", action="store_true")
    _add_injection_args(edit)
    edit.set_defaults(func=cmd_edit)

    shell = sub.add_parser("shell", help="interactive parameter/subgraph editor")
    shell.add_argument("--state", required=True)
    shell.set_defaults(func=cmd_shell)

    demo = sub.add_parser("demo", help="run samples, equivalence checks, recovery, and fault scenarios")
    demo.add_argument("--output", default="artifacts/demo")
    demo.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BusinessError as exc:
        report_path = getattr(args, "report", None)
        report = _error_report(exc)
        if report_path:
            write_report(Path(report_path), report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
