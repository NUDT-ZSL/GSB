"""Command line entry points for the deterministic recall/fusion experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .config import load_config
from .engine import Engine
from .errors import BusinessError
from .snapshots import SnapshotStore, checksum_payload


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    engine = Engine(config, args.output)
    summary = engine.run()
    canonical_path = summary["artifacts"]["canonical"]
    summary["canonical_sha256"] = _sha256_file(canonical_path)
    with open(Path(args.output) / "run_summary.json", "w", encoding="utf-8") as handle:
        json.dump(summary, handle, sort_keys=True, indent=2)
    print(json.dumps({"status": "ok", "summary": summary}, sort_keys=True, indent=2))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    left = Path(args.left) / "results.canonical.jsonl"
    right = Path(args.right) / "results.canonical.jsonl"
    left_hash, right_hash = _sha256_file(left), _sha256_file(right)
    report = {
        "left": str(left),
        "right": str(right),
        "left_sha256": left_hash,
        "right_sha256": right_hash,
        "byte_identical": left_hash == right_hash,
    }
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if report["byte_identical"] else 1


def _cmd_snapshot_demo(args: argparse.Namespace) -> int:
    """Exercise whole-snapshot replacement, rollback and corruption paths."""
    store = SnapshotStore(Path(args.output) / "snapshots_demo")
    events: list[dict] = []
    store.submit("fusion_weights", 1, {"inverted": 0.4})
    events.append({"action": "submit", "name": "fusion_weights", "version": 1})
    store.submit("fusion_weights", 2, {"inverted": 0.5})
    events.append({"action": "submit", "name": "fusion_weights", "version": 2})
    loaded = store.load("fusion_weights")
    events.append(
        {
            "action": "load",
            "name": loaded.name,
            "version": loaded.version,
            "checksum": loaded.checksum,
        }
    )

    rollback_error = None
    try:
        store.submit("fusion_weights", 1, {"inverted": 0.1})
    except BusinessError as exc:
        rollback_error = exc.to_dict()
    events.append({"action": "rollback_attempt", "result": rollback_error})

    target = store._version_path("fusion_weights", 2)  # intentional demo tamper
    record = json.loads(target.read_text(encoding="utf-8"))
    record["payload"]["inverted"] = 0.999
    target.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    corrupt_error = None
    try:
        store.load("fusion_weights", 2)
    except BusinessError as exc:
        corrupt_error = exc.to_dict()
    events.append({"action": "corrupt_and_load", "result": corrupt_error})

    report = {
        "rollback_detected": rollback_error is not None
        and rollback_error["code"] == "E_SNAPSHOT_ROLLBACK",
        "corruption_detected": corrupt_error is not None
        and corrupt_error["code"] == "E_SNAPSHOT_CORRUPT",
        "events": events,
        "checksum_example": checksum_payload({"inverted": 0.5}),
    }
    out_path = Path(args.output) / "snapshot_demo_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, sort_keys=True, indent=2), encoding="utf-8")
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if report["rollback_detected"] and report["corruption_detected"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recall_fusion",
        description="Deterministic multi-channel recall and weighted fusion.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="generate samples and run the pipeline")
    run_parser.add_argument("--config", default=None, help="optional JSON config")
    run_parser.add_argument("--output", default="artifacts/demo/run_a")
    run_parser.set_defaults(func=_cmd_run)

    verify_parser = sub.add_parser(
        "verify", help="compare two run outputs byte-for-byte"
    )
    verify_parser.add_argument("left")
    verify_parser.add_argument("right")
    verify_parser.set_defaults(func=_cmd_verify)

    snap_parser = sub.add_parser(
        "snapshot-demo", help="demonstrate snapshot rollback/corruption handling"
    )
    snap_parser.add_argument("--output", default="artifacts/demo")
    snap_parser.set_defaults(func=_cmd_snapshot_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BusinessError as exc:
        print(json.dumps({"status": "error", "error": exc.to_dict()}, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
