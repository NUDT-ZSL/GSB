"""Command-line entry points for local generation, diagnostics, and workbench."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import load_config, validate_config
from .data import write_demo_sample
from .demo import run_demo
from .engine import DiagnosticEngine
from .errors import BusinessError
from .serialization import pretty_json_bytes


def _print_json(value: Any, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    stream.write("\n")


def _load_mutated_config(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    config = load_config(args.config)
    if args.method:
        config["method"] = args.method
    if args.precision:
        config["precision"]["mode"] = args.precision
    if args.fixed_scale is not None:
        config["precision"]["fixed_scale"] = args.fixed_scale
    if args.threshold_strategy:
        config["evaluation"]["threshold_strategy"] = args.threshold_strategy
    if args.no_standardize:
        config["standardize"] = False
    config = validate_config(config)
    return config, Path(args.config).resolve().parent


def _summary(outcome: Any) -> dict[str, Any]:
    response = outcome.response
    return {
        "ok": True,
        "snapshot_version": response["snapshot"]["version"],
        "snapshot_checksum": response["snapshot"]["overall_checksum"],
        "precision": response["precision"],
        "method": response["method"],
        "dataset": response["dataset"],
        "metrics": {
            "class_consistency": response["metrics"]["class_consistency"],
            "variance_explained": response["metrics"]["variance_explained"],
        },
        "threshold_decisions": [
            {
                "stage": decision["stage"],
                "decision": decision["decision_text"],
                "value": decision.get("value"),
                "threshold": decision["threshold"],
                "strategy": decision["strategy"],
            }
            for decision in response["threshold_trace"]["decisions"]
        ],
        "error_spectrum": {
            "dominant_stage": response["error_spectrum"]["dominant_stage"],
            "dominant_source": response["error_spectrum"]["dominant_source"],
            "tolerance_passed": response["error_spectrum"]["tolerance_passed"],
            "stages": [
                {
                    "stage": stage["stage"],
                    "absolute_rms": stage["absolute_error"]["rms"],
                    "absolute_max": stage["absolute_error"]["max"],
                    "relative_rms": stage["relative_error"]["rms"],
                    "amplification_ratio": stage["error_amplification_ratio"],
                }
                for stage in response["error_spectrum"]["stages"]
            ],
        },
        "selfcheck": response["selfcheck"],
        "checksums": response["checksums"],
        "degraded": response["method"]["degraded"],
        "degradation": response["method"]["degradation"],
        "recent_errors": response["recent_errors"][-5:],
    }


def _workspace_root(config_path: str | Path) -> Path:
    parent = Path(config_path).resolve().parent
    return parent.parent if parent.name == "configs" else parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dimdiag",
        description="Deterministic dimensionality-reduction precision diagnostics",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate local deterministic data")
    generate.add_argument("--workspace", default=".", help="workspace root")

    run = subparsers.add_parser("run", help="execute and commit one diagnostic snapshot")
    run.add_argument("--config", default="configs/demo.json")
    run.add_argument(
        "--method",
        choices=("pca", "random_projection", "pca_random_projection"),
    )
    run.add_argument("--precision", choices=("float64", "float32", "fixed"))
    run.add_argument("--fixed-scale", type=int)
    run.add_argument(
        "--threshold-strategy", choices=("strict", "tolerance_band")
    )
    run.add_argument("--no-standardize", action="store_true")
    run.add_argument("--json", action="store_true")

    demo = subparsers.add_parser("demo", help="run all three paths and write artifacts")
    demo.add_argument("--workspace", default=".")
    demo.add_argument("--config", default=None)
    demo.add_argument("--precision", choices=("float64", "float32", "fixed"))
    demo.add_argument("--no-reset", action="store_true")
    demo.add_argument("--json", action="store_true")

    selfcheck = subparsers.add_parser("selfcheck", help="re-check the active snapshot")
    selfcheck.add_argument("--config", default="configs/demo.json")
    selfcheck.add_argument("--json", action="store_true")

    inspect = subparsers.add_parser("inspect", help="print the active snapshot state")
    inspect.add_argument("--config", default="configs/demo.json")
    inspect.add_argument("--json", action="store_true")

    reset = subparsers.add_parser("reset", help="archive the active snapshot")
    reset.add_argument("--config", default="configs/demo.json")
    reset.add_argument("--json", action="store_true")

    workbench = subparsers.add_parser("workbench", help="start the local web workbench")
    workbench.add_argument("--config", default="configs/demo.json")
    workbench.add_argument("--host", default="127.0.0.1")
    workbench.add_argument("--port", type=int, default=8765)
    workbench.add_argument("--open", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            data_path, config_path = write_demo_sample(args.workspace)
            _print_json(
                {
                    "ok": True,
                    "data": str(data_path),
                    "config": str(config_path),
                }
            )
            return 0
        if args.command == "demo":
            report = run_demo(
                args.workspace,
                config_path=args.config,
                precision_mode=args.precision,
                reset=not args.no_reset,
            )
            if args.json:
                _print_json(report)
            else:
                print(
                    f"demo {'PASS' if report['all_passed'] else 'FAIL'}: "
                    f"{len(report['runs'])} runs; report=artifacts/demo_report.json"
                )
                for run in report["runs"]:
                    if run["ok"]:
                        print(
                            f"- {run['method']}: snapshot=v{run['snapshot_version']}, "
                            f"coords={run['coordinate_checksum'][:16]}, "
                            f"selfcheck={'PASS' if run['selfcheck_passed'] else 'FAIL'}"
                        )
                    else:
                        print(f"- {run['method']}: {run['error']['code']}")
            return 0 if report["all_passed"] else 2
        if args.command == "run":
            config, base_dir = _load_mutated_config(args)
            engine = DiagnosticEngine(_workspace_root(args.config), base_dir=base_dir)
            outcome = engine.run(config, base_dir=base_dir, commit=True)
            payload = _summary(outcome)
            if args.json:
                _print_json(payload)
            else:
                print(
                    f"snapshot v{payload['snapshot_version']} "
                    f"{payload['method']['effective']} "
                    f"{payload['precision']['mode']} "
                    f"coords={payload['checksums']['coordinates'][:16]}"
                )
                print(
                    f"class_consistency={payload['metrics']['class_consistency']:.6f} "
                    f"variance_explained={payload['metrics']['variance_explained']} "
                    f"dominant={payload['error_spectrum']['dominant_source']}"
                )
                print(
                    "thresholds: "
                    + ", ".join(
                        f"{item['stage']}={item['decision']}"
                        for item in payload["threshold_decisions"]
                    )
                )
            return 0
        if args.command == "selfcheck":
            config = load_config(args.config)
            engine = DiagnosticEngine(
                _workspace_root(args.config), base_dir=Path(args.config).resolve().parent
            )
            result = engine.selfcheck_current()
            if args.json:
                _print_json(result)
            else:
                check = result["selfcheck"]
                print(
                    f"selfcheck {'PASS' if check['overall_passed'] else 'FAIL'} "
                    f"snapshot=v{result['snapshot_version']}"
                )
                for item in check["checks"]:
                    print(f"- {item['category']}: {'PASS' if item['passed'] else 'FAIL'}")
            return 0
        if args.command == "inspect":
            engine = DiagnosticEngine(
                _workspace_root(args.config), base_dir=Path(args.config).resolve().parent
            )
            state = engine.inspect()
            if args.json:
                _print_json(state)
            else:
                active = state.get("active")
                if not active:
                    print("no active snapshot")
                else:
                    print(
                        f"snapshot v{active['snapshot_version']} "
                        f"{active['effective_method']} {active['precision']['mode']}"
                    )
                    print(f"overall_checksum={active['overall_checksum']}")
                    print(f"coordinates_checksum={active['coordinates_checksum']}")
                    print(
                        f"class_consistency={active['metrics']['class_consistency']:.6f} "
                        f"variance_explained={active['metrics']['variance_explained']}"
                    )
            return 0
        if args.command == "reset":
            engine = DiagnosticEngine(
                _workspace_root(args.config), base_dir=Path(args.config).resolve().parent
            )
            result = engine.store.reset("cli_reset")
            if args.json:
                _print_json({"ok": True, "archived": result})
            else:
                print("active snapshot archived" if result else "no active snapshot")
            return 0
        if args.command == "workbench":
            from .workbench import serve

            serve(
                config_path=Path(args.config).resolve(),
                host=args.host,
                port=args.port,
                open_browser=args.open,
            )
            return 0
        parser.error("unknown command")
    except BusinessError as error:
        _print_json(error.to_dict(), error=True)
        return 2
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

