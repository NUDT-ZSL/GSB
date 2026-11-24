"""Command line interface for the offline engine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import Engine, InputPaths
from .errors import EngineError
from .models import EngineConfig
from .sample_gen import DEFAULT_SEED, generate_sample
from .util import stable_json


def _json_print(value: object) -> None:
    print(stable_json(value, pretty=True))


def _run_command(args: argparse.Namespace) -> int:
    config = EngineConfig(
        max_workers=args.workers,
        pending_host_queue_limit=args.queue_limit,
        per_host_timeout_ms=args.host_timeout_ms,
        max_alerts_per_host=args.max_alerts_per_host,
        checkpoint_every_batch=not args.no_batch_checkpoint,
    )
    engine = Engine(args.workspace, config)
    report = engine.run(
        InputPaths.from_directory(args.sample),
        resume=args.resume,
        write_report=not args.no_report,
    )
    summary = {
        "status": report["status"],
        "report": str(engine.report_path),
        "counts": report["counts"],
        "business_codes": report["business_codes"],
        "scheduler": {
            key: report["scheduler"][key]
            for key in (
                "hosts",
                "processed_hosts",
                "rejected_hosts",
                "elapsed_ms",
            )
        },
        "recompute_full_match": report["diagnostics"]["recompute"][
            "full_match"
        ],
    }
    _json_print(summary)
    return 0 if report["status"] == "OK" else 1


def _generate_command(args: argparse.Namespace) -> int:
    truth = generate_sample(args.out, seed=args.seed)
    _json_print(
        {
            "generated": str(args.out),
            "seed": truth["seed"],
            "observation_clock_ms": truth["observation_clock_ms"],
            "truth_sync": str(Path(args.out) / "truth_sync.json"),
        }
    )
    return 0


def _verify_command(args: argparse.Namespace) -> int:
    engine = Engine(
        args.workspace,
        EngineConfig(
            max_workers=args.workers,
            pending_host_queue_limit=args.queue_limit,
            per_host_timeout_ms=args.host_timeout_ms,
            max_alerts_per_host=args.max_alerts_per_host,
        ),
    )
    report = engine.run(
        InputPaths.from_directory(args.sample),
        resume=args.resume,
        write_report=True,
    )
    truth_path = Path(args.sample) / "truth_sync.json"
    with truth_path.open("r", encoding="utf-8") as handle:
        truth = json.load(handle)
    expected = truth.get("expected", {})
    failures: list[str] = []
    if report["status"] != expected.get("report_status", "OK"):
        failures.append(
            f"status {report['status']} != {expected.get('report_status')}"
        )
    if (
        report["diagnostics"]["recompute"]["full_match"]
        is not expected.get("recompute_full_match", True)
    ):
        failures.append("recompute full_match marker differs")
    missing_codes = sorted(
        set(expected.get("business_codes_present", []))
        - set(report["business_codes"])
    )
    if missing_codes:
        failures.append(f"missing business codes: {missing_codes}")
    actual_arrival = report["patch_processing"]["arrival_order"]
    if actual_arrival != expected.get("patch_arrival_order"):
        failures.append("patch arrival order marker differs")
    actual_logical = report["patch_processing"]["logical_order"]
    if actual_logical != expected.get("patch_logical_order"):
        failures.append("patch logical order marker differs")
    if (
        report["patch_processing"]["out_of_order_arrival"]
        is not expected.get("out_of_order_arrival")
    ):
        failures.append("out-of-order marker differs")
    result = {
        "ok": not failures,
        "failures": failures,
        "status": report["status"],
        "business_codes": report["business_codes"],
    }
    _json_print(result)
    return 0 if not failures else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m alert_engine",
        description="Offline alert correlation and de-noising engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate", help="generate the deterministic sample"
    )
    generate.add_argument("--out", type=Path, default=Path("samples"))
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate.set_defaults(handler=_generate_command)

    for name, handler in (("run", _run_command), ("verify", _verify_command)):
        command = subparsers.add_parser(name)
        command.add_argument("--sample", type=Path, default=Path("samples"))
        command.add_argument(
            "--workspace", type=Path, default=Path("build/engine-work")
        )
        command.add_argument("--workers", type=int, default=1)
        command.add_argument("--queue-limit", type=int, default=64)
        command.add_argument("--host-timeout-ms", type=int, default=10_000)
        command.add_argument("--max-alerts-per-host", type=int, default=100_000)
        command.add_argument("--resume", action="store_true")
        command.add_argument("--no-batch-checkpoint", action="store_true")
        command.add_argument("--no-report", action="store_true")
        command.set_defaults(handler=handler)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except EngineError as exc:
        print(stable_json(exc.as_dict(), pretty=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
