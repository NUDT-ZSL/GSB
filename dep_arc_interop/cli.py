from __future__ import annotations

import argparse
import json
import runpy
from collections.abc import Sequence
from pathlib import Path

from .demo import run_demo, write_report
from .errors import BusinessError

ROOT = Path(__file__).resolve().parents[1]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dep-arc-interop",
        description="Dependency arc format interop demo and verifier",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser(
        "generate",
        help="regenerate deterministic local samples and injection fixtures",
    )
    generate.add_argument(
        "--script",
        default=str(ROOT / "scripts" / "generate_samples.py"),
    )
    demo = subparsers.add_parser(
        "demo",
        help="run the complete equivalence, migration, roundtrip and failure demo",
    )
    demo.add_argument("--data-root", default=str(ROOT / "data"))
    demo.add_argument(
        "--report",
        default=str(ROOT / "artifacts" / "validation_report.json"),
    )
    demo.add_argument("--compact", action="store_true")
    check = subparsers.add_parser(
        "selfcheck",
        help="run cross-module consistency checks only",
    )
    check.add_argument("--data-root", default=str(ROOT / "data"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            runpy.run_path(args.script, run_name="__main__")
            return 0
        if args.command == "demo":
            report = run_demo(args.data_root)
            if args.report:
                write_report(report, args.report)
            summary = {
                "all_passed": report["all_passed"],
                "environment": report["environment"],
                "snapshots": report["snapshots"]["current_versions"],
                "equivalence": report["equivalence"],
                "migrations": report["migrations"],
                "roundtrips": {
                    version: result["consistent"]
                    for version, result in report["roundtrips"].items()
                },
                "resource_streaming": {
                    "degraded": report["resource_limit"]["streaming"]["degraded"],
                    "stream_arcs_preserved": report["resource_limit"][
                        "stream_arcs_preserved"
                    ],
                },
                "injection_pass_count": sum(
                    1 for item in report["injection_results"] if item["passed"]
                ),
                "injection_total": len(report["injection_results"]),
                "selfcheck": report["selfcheck"]["checks"],
                "report": args.report,
            }
            print(
                json.dumps(
                    summary if args.compact else report,
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if report["all_passed"] else 1
        if args.command == "selfcheck":
            from .config import SnapshotStore
            from .facade import run_self_check

            root = Path(args.data_root)
            store = SnapshotStore()
            store.load(root / "config" / "controlled_snapshot_v1.json")
            result = run_self_check(
                {
                    "v1": root / "samples" / "arcs_v1.txt",
                    "v2": root / "samples" / "arcs_v2.txt",
                    "v3": root / "samples" / "arcs_v3.json",
                },
                store,
            )
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 0
    except BusinessError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2))
        return 2
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "E_LOCAL_IO",
                        "message": str(exc),
                        "details": {},
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
