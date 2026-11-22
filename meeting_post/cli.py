"""Command-line entry points for demo, validation, migration and dashboard."""

from __future__ import annotations

import argparse
import json
import sys
import unittest
from pathlib import Path

from .app_state import DashboardState
from .config import migrate_config, parse_config, render_config
from .demo import build_demo, sha256sums_text, write_run_log
from .errors import MeetingError
from .webapp import run_server


def _add_demo_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--artifact-dir", default="data/demo", help="generated sample artifacts")
    parser.add_argument("--config-dir", default="configs", help="three-version sample configs")
    parser.add_argument("--runtime-dir", default="data/runtime", help="snapshot persistence root")
    parser.add_argument("--seed", type=int, default=20261123, help="fixed synthesis seed")


def command_demo(args: argparse.Namespace) -> int:
    report = build_demo(
        artifact_dir=Path(args.artifact_dir),
        config_dir=Path(args.config_dir),
        runtime_dir=Path(args.runtime_dir),
        seed=args.seed,
        reset_runtime=True,
    )
    run_log = write_run_log(report, Path(args.artifact_dir) / "run_log.txt")
    (Path(args.artifact_dir) / "sha256sums.txt").write_text(
        sha256sums_text(Path(args.artifact_dir)),
        encoding="utf-8",
    )
    summary = {
        "seed": report["seed"],
        "frame_count": report["frame_count"],
        "config_interop_equal": report["config_interop"]["equal"],
        "self_check_ok": report["self_check"]["ok"],
        "segments": report["self_check"]["counts"]["segments"],
        "identities": report["self_check"]["counts"]["identities"],
        "pcm_sha256": report["serialized"]["mixed_pcm_sha256"],
        "run_log": str(run_log),
        "artifact_dir": str(Path(args.artifact_dir)),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    ok = (
        report["config_interop"]["equal"]
        and report["self_check"]["ok"]
        and report["snapshot_lab"]["rollback_blocked"]
        and report["snapshot_lab"]["corruption_detected"]
        and bool(report["migration"]["errors"])
    )
    return 0 if ok else 1


def command_serve(args: argparse.Namespace) -> int:
    state = DashboardState(
        artifact_dir=Path(args.artifact_dir),
        config_dir=Path(args.config_dir),
        runtime_dir=Path(args.runtime_dir),
        seed=args.seed,
        regenerate=not args.keep_state,
    )
    run_server(state, host=args.host, port=args.port, open_browser=args.open)
    return 0


def command_selftest(args: argparse.Namespace) -> int:
    report = build_demo(
        artifact_dir=Path(args.artifact_dir),
        config_dir=Path(args.config_dir),
        runtime_dir=Path(args.runtime_dir),
        seed=args.seed,
        reset_runtime=True,
    )
    write_run_log(report, Path(args.artifact_dir) / "run_log.txt")
    (Path(args.artifact_dir) / "sha256sums.txt").write_text(
        sha256sums_text(Path(args.artifact_dir)),
        encoding="utf-8",
    )
    checks = {
        "deterministic_three_version_interop": report["config_interop"]["equal"],
        "frame_count_matches_truth": report["frame_count"] * 3 == report["truth_record_count"],
        "cross_module_self_check": report["self_check"]["ok"],
        "snapshot_rollback_blocked": report["snapshot_lab"]["rollback_blocked"],
        "snapshot_corruption_detected": report["snapshot_lab"]["corruption_detected"],
        "semantic_loss_blocked": any(
            item["code"] == "E_SEMANTIC_LOSS" for item in report["migration"]["errors"]
        ),
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


def command_migrate(args: argparse.Namespace) -> int:
    source = Path(args.input).read_text(encoding="utf-8")
    config = parse_config(source)
    migrated, audit = migrate_config(config, args.target, lossy=args.lossy)
    text = render_config(migrated, args.target)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(
            json.dumps(
                {
                    "ok": True,
                    "output": str(output),
                    "audit": audit.to_dict(),
                    "semantic_digest": migrated.semantic_digest(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        sys.stdout.write(text)
        sys.stderr.write(json.dumps(audit.to_dict(), ensure_ascii=False, indent=2) + "\n")
    return 0


def command_test(_: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parent.parent
    suite = unittest.defaultTestLoader.discover(str(root / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meeting-post",
        description="Local deterministic meeting interpretation post-production toolkit",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo_parser = subparsers.add_parser("demo", help="generate samples and run acceptance checks")
    _add_demo_arguments(demo_parser)
    demo_parser.set_defaults(func=command_demo)

    selftest_parser = subparsers.add_parser("selftest", help="run end-to-end acceptance checks")
    _add_demo_arguments(selftest_parser)
    selftest_parser.set_defaults(func=command_selftest)

    serve_parser = subparsers.add_parser("serve", help="start the local interactive dashboard")
    _add_demo_arguments(serve_parser)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    serve_parser.add_argument(
        "--keep-state",
        action="store_true",
        help="reuse existing demo artifacts and snapshots instead of regenerating",
    )
    serve_parser.set_defaults(func=command_serve)

    migrate_parser = subparsers.add_parser("migrate", help="migrate one local configuration")
    migrate_parser.add_argument("--input", required=True)
    migrate_parser.add_argument("--target", required=True, choices=("v1", "v2", "v3"))
    migrate_parser.add_argument("--lossy", action="store_true")
    migrate_parser.add_argument("--output")
    migrate_parser.set_defaults(func=command_migrate)

    test_parser = subparsers.add_parser("test", help="run the local unittest suite")
    test_parser.set_defaults(func=command_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except MeetingError as exc:
        sys.stderr.write(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2) + "\n")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
