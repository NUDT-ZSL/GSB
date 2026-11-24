from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from . import EXIT_ERROR, EXIT_INTERRUPTED, EXIT_OK, EXIT_VALIDATION, __version__
from .errors import BalanceLabError, error_envelope
from .evolution import run_evolution
from .ledger import Ledger
from .pipeline import run_pipeline, verify_ledger_consistency
from .report import write_interrupt_marker
from .sample import SEED, generate_sample_files

EXIT_CONSISTENCY = 3


def _print_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def _install_signal_handlers() -> tuple[threading.Event, dict[str, Any]]:
    stop_event = threading.Event()
    signal_info: dict[str, Any] = {"name": None, "number": None}

    def handler(signum: int, _frame: Any) -> None:
        stop_event.set()
        signal_info["number"] = signum
        try:
            signal_info["name"] = signal.Signals(signum).name
        except ValueError:
            signal_info["name"] = str(signum)

    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handler)
    return stop_event, signal_info


def cmd_generate(args: argparse.Namespace) -> int:
    paths = generate_sample_files(args.output, seed=args.seed)
    _print_json(
        {
            "ok": True,
            "generated": [str(path) for path in paths],
            "seed": args.seed,
        }
    )
    return EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    from .model import load_model

    model = load_model(args.input)
    _print_json(
        {
            "ok": True,
            "input_version_id": model.input_version_id,
            "battles": [
                {
                    "battle_id": battle.battle_id,
                    "rounds": battle.round_count,
                    "chunks": battle.chunk_count,
                }
                for battle in model.battle_list()
            ],
            "unit_count": len(model.units),
            "skill_count": len(model.skills),
            "drop_table_count": len(model.drop_tables),
        }
    )
    return EXIT_OK


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "parallelism": args.workers,
        "logic_time_budget_ms": args.logic_budget_ms,
        "memory_budget_bytes": args.memory_budget_bytes,
        "chunk_delay_ms": args.chunk_delay_ms,
    }


def cmd_run(args: argparse.Namespace) -> int:
    stop_event, signal_info = _install_signal_handlers()
    report_path = Path(args.report) if args.report else Path(args.reports_dir) / "balance_report.json"
    result = run_pipeline(
        args.input,
        args.state_dir,
        report_path,
        stop_event=stop_event,
        revision=args.revision,
        expected_report_version_id=args.expected_report_version,
        verify=args.verify,
        config_overrides=_overrides(args),
    )
    if result["status"] == "interrupted":
        marker_path = Path(args.state_dir) / "interrupt.json"
        marker = {
            "status": "interrupted",
            "exit_code": EXIT_INTERRUPTED,
            "boundary": "chunk_boundary",
            "signal": signal_info.get("name"),
            "signal_number": signal_info.get("number"),
            "schedule": result["schedule"],
            "recovery": result.get("recovery", []),
        }
        write_interrupt_marker(marker_path, marker)
        _print_json(
            {
                "ok": True,
                "interrupted": True,
                "exit_code": EXIT_INTERRUPTED,
                "marker": str(marker_path),
                "overview": result["schedule"]["overview"],
            }
        )
        return EXIT_INTERRUPTED

    verification = result.get("verification")
    consistent = True if verification is None else verification["consistent"]
    _print_json(
        {
            "ok": True,
            "status": "completed",
            "balanced": result["report"]["balanced"],
            "consistent": consistent,
            "report": str(report_path),
            "report_version": result["report_write"]["version_id"]
            if result.get("report_write")
            else result["report"]["version_id"],
            "scheduler_rounds": len(result["schedule"]["rounds"]),
            "admission_count": len(result["schedule"]["admission_sequence"]),
        }
    )
    if verification is not None and not verification["consistent"]:
        return EXIT_CONSISTENCY
    return EXIT_OK


def cmd_evolve(args: argparse.Namespace) -> int:
    stop_event, signal_info = _install_signal_handlers()
    result = run_evolution(
        args.input,
        args.work_dir,
        args.state_dir,
        args.reports_dir,
        stop_event=stop_event,
    )
    if result.get("status") == "interrupted":
        marker_path = Path(args.state_dir) / "interrupt.json"
        write_interrupt_marker(
            marker_path,
            {
                "status": "interrupted",
                "exit_code": EXIT_INTERRUPTED,
                "signal": signal_info.get("name"),
                "schedule": result.get("schedule"),
            },
        )
        _print_json({"ok": True, "interrupted": True, "marker": str(marker_path)})
        return EXIT_INTERRUPTED
    _print_json(
        {
            "ok": True,
            "all_consistent": result["all_consistent"],
            "steps": result["total_steps"],
            "total_invalid_chunks": result["total_invalid_chunks"],
            "invalidation_report": str(Path(args.reports_dir) / "invalidation_report.json"),
        }
    )
    return EXIT_OK if result["all_consistent"] else EXIT_CONSISTENCY


def cmd_verify(args: argparse.Namespace) -> int:
    from .model import load_model

    model = load_model(args.input)
    with Ledger(Path(args.state_dir) / "balance.db") as ledger:
        result = verify_ledger_consistency(model, ledger)
    _print_json({"ok": True, **result})
    return EXIT_OK if result["consistent"] else EXIT_CONSISTENCY


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="balance-lab",
        description="Deterministic combat balance, incremental chunks and recovery lab.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate-samples", help="generate deterministic JSON samples")
    generate.add_argument("--output", default="samples")
    generate.add_argument("--seed", type=int, default=SEED)
    generate.set_defaults(func=cmd_generate)

    validate = sub.add_parser("validate", help="parse and validate all input JSON")
    validate.add_argument("--input", default="samples")
    validate.set_defaults(func=cmd_validate)

    run = sub.add_parser("run", help="run chunks, SQLite ledger and balance report")
    run.add_argument("--input", default="samples")
    run.add_argument("--state-dir", default="state")
    run.add_argument("--reports-dir", default="reports")
    run.add_argument("--report", default=None)
    run.add_argument("--revision", type=int, default=0)
    run.add_argument("--expected-report-version", default=None)
    run.add_argument("--verify", action="store_true")
    run.add_argument("--workers", type=int, default=None)
    run.add_argument("--logic-budget-ms", type=int, default=None)
    run.add_argument("--memory-budget-bytes", type=int, default=None)
    run.add_argument("--chunk-delay-ms", type=int, default=None)
    run.set_defaults(func=cmd_run)

    evolve = sub.add_parser("evolve", help="apply edit_script.json step by step")
    evolve.add_argument("--input", default="samples")
    evolve.add_argument("--work-dir", default="build/edits")
    evolve.add_argument("--state-dir", default="state-evolve")
    evolve.add_argument("--reports-dir", default="reports-evolve")
    evolve.set_defaults(func=cmd_evolve)

    verify = sub.add_parser("verify-consistency", help="compare ledger with full recomputation")
    verify.add_argument("--input", default="samples")
    verify.add_argument("--state-dir", default="state")
    verify.set_defaults(func=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BalanceLabError as exc:
        _print_json({"ok": False, "error": error_envelope(exc)["error"]})
        return EXIT_VALIDATION if isinstance(exc, BalanceLabError) and exc.code.startswith("E_") and exc.code not in {"E_LEDGER", "E_BUDGET", "E_REPORT_VERSION", "E_REPORT_VERSION_CONFLICT"} else EXIT_ERROR
    except KeyboardInterrupt:
        stop_marker = Path("state/interrupt.json")
        write_interrupt_marker(
            stop_marker,
            {"status": "interrupted", "exit_code": EXIT_INTERRUPTED, "signal": "SIGINT"},
        )
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - CLI boundary returns JSON.
        _print_json(error_envelope(exc))
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
