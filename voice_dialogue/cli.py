"""Command-line entry points for generation, processing and verification."""

from __future__ import annotations

import argparse
import json
import sys

from .config import ComponentConfig
from .errors import BusinessError
from .pipeline import run_pipeline
from .snapshot import load_snapshot
from .synth import generate_sample_bundle


def _load_config(path: str | None) -> ComponentConfig:
    if path is None:
        return ComponentConfig()
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return ComponentConfig.from_dict(data)


def _write_error(exc: BusinessError, report_path: str | None) -> dict:
    payload = {"status": "error", "business_code": exc.code, "error": exc.to_dict()}
    if report_path is not None:
        with open(report_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, sort_keys=True, ensure_ascii=False, indent=2)
            fh.write("\n")
    return payload


def cmd_generate(args) -> int:
    path = generate_sample_bundle(
        out_dir=args.out_dir,
        seed=args.seed,
        small_dialogs=args.small,
        large_dialogs=args.large,
    )
    print(json.dumps({"status": "ok", "manifest": path}, sort_keys=True))
    return 0


def cmd_run(args) -> int:
    try:
        config = _load_config(args.config)
        report = run_pipeline(
            manifest_path=args.manifest,
            config=config,
            report_path=args.report,
            snapshot_dir=args.snapshot_dir,
        )
    except BusinessError as exc:
        payload = _write_error(exc, args.report)
        print(json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2))
        return 2
    summary = {
        "status": report["status"],
        "business_code": report["business_code"],
        "path": report["path_decision"]["path"],
        "dialogs": len(report["dialogs"]),
        "report": args.report,
    }
    print(json.dumps(summary, sort_keys=True, ensure_ascii=False, indent=2))
    return 0


def cmd_verify_snapshot(args) -> int:
    try:
        snapshot = load_snapshot(args.snapshot_dir)
    except BusinessError as exc:
        print(json.dumps(exc.to_dict(), sort_keys=True, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(
        {
            "status": "ok",
            "business_code": "OK",
            "version": snapshot["version"],
            "checksum": snapshot["checksum"],
        },
        sort_keys=True,
    ))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voice-dialogue",
        description="Deterministic voice emotion & dialogue understanding component",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="synthesize deterministic samples")
    gen.add_argument("--out-dir", required=True)
    gen.add_argument("--seed", type=int, default=20261120)
    gen.add_argument("--small", type=int, default=3)
    gen.add_argument("--large", type=int, default=6)
    gen.set_defaults(func=cmd_generate)

    run = sub.add_parser("run", help="process a manifest and emit a JSON report")
    run.add_argument("--manifest", required=True)
    run.add_argument("--config", default=None)
    run.add_argument("--report", required=True)
    run.add_argument("--snapshot-dir", default=None)
    run.set_defaults(func=cmd_run)

    ver = sub.add_parser("verify-snapshot", help="validate a stored snapshot")
    ver.add_argument("--snapshot-dir", required=True)
    ver.set_defaults(func=cmd_verify_snapshot)
    return parser


def main(argv: list | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
