from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .canonical import pretty_json, write_text
from .compiler import compile_templates
from .errors import BusinessError
from .interactive import InteractiveApp
from .migration import (
    downgrade_ir_to_v1,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    migrate_v2_to_v3,
)
from .models import CompilerConfig
from .pipeline import report_summary, run_demo, write_report
from .readers import load_slot_types, read_template_file


def default_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _emit(value: Any) -> None:
    print(pretty_json(value))


def _demo(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    report = run_demo(root, CompilerConfig())
    output = Path(args.output)
    if not output.is_absolute():
        output = root / output
    write_report(report, output)
    summary = report_summary(report)
    summary["report_path"] = str(output)
    _emit(summary)
    return 0 if report["ok"] else 1


def _validate(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    report = run_demo(root, CompilerConfig())
    advanced = report["advanced_equivalence"]
    advanced_summary = {
        "status": advanced["status"],
        "code": advanced["code"],
        "unrecoverable_categories": advanced["unrecoverable_categories"],
        "first_difference": advanced["first_difference"],
    }
    payload = {
        "ok": report["validation"]["ok"],
        "validation": report["validation"],
        "determinism": report["determinism"],
        "equivalence": report["equivalence"]["status"],
        "advanced_equivalence": advanced if args.full else advanced_summary,
    }
    _emit(payload)
    return 0 if report["validation"]["ok"] and report["determinism"]["ok"] else 1


def _migrate(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    config = CompilerConfig()
    slot_types_path = Path(args.slot_types)
    if not slot_types_path.is_absolute():
        slot_types_path = root / slot_types_path
    slot_types = load_slot_types(slot_types_path)
    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = root / input_path
    parsed = read_template_file(input_path, slot_types, config)
    if args.from_dialect == "v1" and args.to_dialect == "v2":
        result = migrate_v1_to_v2(parsed, config)
    elif args.from_dialect == "v2" and args.to_dialect == "v1":
        result = migrate_v2_to_v1(parsed)
    elif args.from_dialect == "v2" and args.to_dialect == "v3":
        result = migrate_v2_to_v3(parsed)
    elif args.from_dialect == "v3" and args.to_dialect == "v1":
        compiled = compile_templates(parsed, config)
        result = downgrade_ir_to_v1(compiled)
    else:
        raise BusinessError(
            "E_FORMAT_CORRUPT",
            f"unsupported migration {args.from_dialect}->{args.to_dialect}",
            field="migration",
        )
    if args.output:
        output = Path(args.output)
        if not output.is_absolute():
            output = root / output
        write_text(output, result["artifact"])
    _emit(
        {
            "source_dialect": result["source_dialect"],
            "target_dialect": result["target_dialect"],
            "fill_ins": result["fill_ins"],
            "discards": result["discards"],
            "artifact": result["artifact"],
            "output": args.output,
        }
    )
    return 0


def _interactive(args: argparse.Namespace) -> int:
    app = InteractiveApp(Path(args.root).resolve(), CompilerConfig())
    return app.run()


def build_parser() -> argparse.ArgumentParser:
    root_default = str(default_root())
    parser = argparse.ArgumentParser(
        prog="qrw",
        description="Local deterministic query rewrite template compiler and verifier",
    )
    parser.add_argument("--root", default=root_default, help="project root containing samples/")
    subparsers = parser.add_subparsers(dest="command")

    demo = subparsers.add_parser("demo", help="run the full deterministic demo and write JSON")
    demo.add_argument("--root", default=argparse.SUPPRESS)
    demo.add_argument("--output", default="artifacts/demo_report.json")
    demo.set_defaults(handler=_demo)

    validate = subparsers.add_parser("validate", help="run independent cross-dialect verification")
    validate.add_argument("--root", default=argparse.SUPPRESS)
    validate.add_argument("--full", action="store_true", help="include complete drift details")
    validate.set_defaults(handler=_validate)

    migrate = subparsers.add_parser("migrate", help="migrate template dialects")
    migrate.add_argument("--root", default=argparse.SUPPRESS)
    migrate.add_argument("--from", dest="from_dialect", required=True, choices=["v1", "v2", "v3"])
    migrate.add_argument("--to", dest="to_dialect", required=True, choices=["v1", "v2", "v3"])
    migrate.add_argument("--input", required=True)
    migrate.add_argument("--output")
    migrate.add_argument("--slot-types", default="samples/slot_types.json")
    migrate.set_defaults(handler=_migrate)

    interactive = subparsers.add_parser("interactive", help="start the local interactive application")
    interactive.add_argument("--root", default=argparse.SUPPRESS)
    interactive.set_defaults(handler=_interactive)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    args = parser.parse_args(raw_args)
    if not getattr(args, "command", None):
        args = parser.parse_args([*raw_args, "demo"])
    try:
        return int(args.handler(args))
    except BusinessError as exc:
        _emit({"ok": False, "error": exc.to_dict()})
        return 1
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "E_INTERNAL",
                    "message": str(exc),
                    "type": type(exc).__name__,
                },
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

