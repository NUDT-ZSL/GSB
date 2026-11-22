from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .fixtures import generate_fixtures
from .inference import DeterministicEngine, LayerCache
from .service import load_fixtures, write_acceptance_report
from .snapshots import build_snapshot


DEFAULT_ROOT = "workspace_data"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quantdiag",
        description=(
            "Local deterministic CNN quantization diagnostics workbench. "
            "All samples and snapshots are generated locally from fixed seeds."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate", help="generate local images, v1/v2/v3 samples and snapshots"
    )
    generate.add_argument("--root", default=DEFAULT_ROOT)
    generate.add_argument("--force", action="store_true")

    acceptance = subparsers.add_parser(
        "acceptance", help="run the complete headless acceptance suite"
    )
    acceptance.add_argument("--root", default=DEFAULT_ROOT)
    acceptance.add_argument(
        "--report",
        default=None,
        help="report path (default: <root>/acceptance_report.json)",
    )
    acceptance.add_argument(
        "--json", action="store_true", help="print the full JSON report"
    )

    inspect = subparsers.add_parser(
        "inspect", help="query a layer cache key and product hash"
    )
    inspect.add_argument("--root", default=DEFAULT_ROOT)
    inspect.add_argument(
        "--layer", default="conv2", choices=("conv1", "conv2", "fc")
    )

    gui = subparsers.add_parser(
        "gui", help="launch the PyQt5 interactive diagnostic workbench"
    )
    gui.add_argument("--root", default=DEFAULT_ROOT)
    return parser


def _print_acceptance_summary(report: dict, report_path: Path) -> None:
    status = "PASS" if report["passed"] else "FAIL"
    print(f"[{status}] deterministic CNN quantization diagnostics")
    for check in report["checks"]:
        marker = "PASS" if check["passed"] else "FAIL"
        print(f"  [{marker}] {check['name']}")
    codes = [item["code"] for item in report.get("recent_errors", [])]
    print(f"  recent error codes: {', '.join(codes) if codes else '(none)'}")
    cache = report["sections"]["cache"]
    print(
        "  warm-cache reuse: "
        + (", ".join(cache["second"]["reused"]) or "(none)")
    )
    invalidation = report["sections"]["invalidation"]
    for scenario, payload in invalidation.items():
        inference = payload["inference"]
        print(
            f"  {scenario}: recompute={inference['recomputed']} "
            f"reuse={inference['reused']}"
        )
    print(f"  report: {report_path}")


def _inspect(root: str, layer: str) -> dict:
    bundle = load_fixtures(root)
    snapshot = build_snapshot(
        bundle.v2,
        bundle.calibration,
        version=20,
        model_fingerprint=bundle.weights.fingerprint(),
    )
    engine = DeterministicEngine(bundle.weights)
    cache = LayerCache()
    first = engine.infer(snapshot, bundle.images, cache=cache)
    second = engine.infer(snapshot, bundle.images, cache=cache)
    return {
        "snapshot_version": snapshot.version,
        "cache_key": cache.explain(layer),
        "first_inference": {
            "recomputed": list(first.recomputed_layers),
            "reused": list(first.reused_layers),
        },
        "second_inference": {
            "recomputed": list(second.recomputed_layers),
            "reused": list(second.reused_layers),
        },
        "layer_checksums": {
            item.layer: item.activation_checksum for item in second.layers
        },
        "scores_checksum": second.scores_checksum,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "generate":
        result = generate_fixtures(args.root, force=args.force)
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    if args.command == "acceptance":
        report, path = write_acceptance_report(args.root, args.report)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
        else:
            _print_acceptance_summary(report, path)
        return 0 if report["passed"] else 1
    if args.command == "inspect":
        print(
            json.dumps(
                _inspect(args.root, args.layer),
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "gui":
        try:
            from .gui import run_gui
        except ImportError:
            print(
                "PyQt5 is unavailable. Use the headless path instead: "
                "python -m quantdiag acceptance",
                file=sys.stderr,
            )
            return 2
        try:
            return run_gui(args.root)
        except Exception as exc:
            print(
                f"Qt window could not be created: {exc}\n"
                "Use the headless path: python -m quantdiag acceptance",
                file=sys.stderr,
            )
            return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

