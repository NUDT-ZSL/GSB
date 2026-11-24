"""Command line interface for the VPN diagnostic engine."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from .checkpoint import CheckpointManager, atomic_write_json
from .parser import parse_sample
from .report import write_report
from .runtime import execute_sample, export_latest_report, query_progress, verify_workspace
from .samplegen import write_sample


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _load_and_import(sample: Path, workspace: Path) -> dict[str, Any]:
    raw_text = sample.read_text(encoding="utf-8")
    raw = json.loads(raw_text)
    parsed = parse_sample(raw)
    workspace.mkdir(parents=True, exist_ok=True)
    atomic_write_json(workspace / "normalized_sample.json", {
        "summary": parsed.to_summary(),
        "peers": [peer.to_dict() for peer in parsed.peers],
        "profiles": {name: profile.to_dict() for name, profile in parsed.profiles.items()},
        "events": [event.to_dict() for event in parsed.events],
    })
    (workspace / "imported_sample.json").write_text(raw_text, encoding="utf-8")
    manager, rebuild = CheckpointManager.prepare(workspace)
    try:
        manager.save_named_json("last_import", {
            "sample_path": str(sample.resolve()),
            "accepted_peers": len(parsed.peers),
            "rejections": parsed.rejections,
            "rebuild": rebuild,
        })
    finally:
        manager.close()
    return parsed.to_summary()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vpn-diag",
        description="Offline deterministic VPN tunnel forwarding diagnostics",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate-sample", help="generate fixed-seed JSON sample")
    generate.add_argument("--output", "-o", required=True)

    import_parser = subparsers.add_parser("import-sample", help="parse, validate and normalize a sample")
    import_parser.add_argument("--input", "-i", required=True, type=Path)
    import_parser.add_argument("--workspace", "-w", required=True, type=Path)

    run = subparsers.add_parser("run", help="execute deterministic forwarding rounds")
    run.add_argument("--sample", "-s", type=Path)
    run.add_argument("--workspace", "-w", required=True, type=Path)
    run.add_argument("--max-rounds", type=int, default=None)
    run.add_argument("--udp-timeout-ms", type=int, default=None)
    run.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument("--start-echo", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument("--rebuild-on-version-mismatch", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument("--report", "-r", type=Path, default=None)

    progress = subparsers.add_parser("progress", help="query peer progress from the latest checkpoint")
    progress.add_argument("--workspace", "-w", required=True, type=Path)
    progress.add_argument("--peer", "-p", default=None)

    export = subparsers.add_parser("export-report", help="export the latest JSON diagnostic report")
    export.add_argument("--workspace", "-w", required=True, type=Path)
    export.add_argument("--output", "-o", required=True, type=Path)

    verify = subparsers.add_parser("verify-checkpoint", help="verify SQLite and snapshot checkpoints")
    verify.add_argument("--workspace", "-w", required=True, type=Path)
    verify.add_argument("--sample", "-s", type=Path, default=None)
    verify.add_argument("--clean-residue", action="store_true")

    echo = subparsers.add_parser("echo-server", help="run a standalone loopback echo server")
    echo.add_argument("--host", default="127.0.0.1")
    echo.add_argument("--port", type=int, default=0)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "generate-sample":
        path = write_sample(args.output)
        print(f"generated {path}")
        return 0

    if args.command == "import-sample":
        summary = _load_and_import(args.input, args.workspace)
        _print_json(summary)
        return 0

    if args.command == "run":
        sample = args.sample
        if sample is None:
            sample = args.workspace / "imported_sample.json"
        if not sample.exists():
            print(f"sample not found: {sample}", file=sys.stderr)
            return 2
        report = execute_sample(
            sample,
            args.workspace,
            max_rounds=args.max_rounds,
            resume=args.resume,
            start_echo=args.start_echo,
            rebuild_on_version_mismatch=args.rebuild_on_version_mismatch,
            udp_timeout_ms=args.udp_timeout_ms,
        )
        if args.report:
            write_report(args.report, report)
        _print_json({
            "status": "ok",
            "report_path": str(args.report) if args.report else None,
            "summary": report["summary"],
            "business_codes": report["business_codes"],
        })
        return 0

    if args.command == "progress":
        try:
            progress = query_progress(args.workspace, args.peer)
        except KeyError:
            print(f"unknown peer: {args.peer}", file=sys.stderr)
            return 4
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        _print_json(progress)
        return 0

    if args.command == "export-report":
        try:
            path = export_latest_report(args.workspace, args.output)
        except (FileNotFoundError, Exception) as exc:
            print(f"export failed: {exc}", file=sys.stderr)
            return 2
        print(f"exported {path}")
        return 0

    if args.command == "verify-checkpoint":
        result = verify_workspace(
            args.workspace, sample_path=args.sample, clean_residue=args.clean_residue
        )
        _print_json(result)
        return 0 if result.get("valid") else 3

    if args.command == "echo-server":
        from .echo import EchoServer

        server = EchoServer(args.host, args.port)
        server.start()
        print(json.dumps({"host": server.host, "port": server.port, "address": server.address}))
        try:
            while True:
                import time

                time.sleep(3600)
        except KeyboardInterrupt:
            server.stop()
        return 0

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
