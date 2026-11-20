from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .checksums import canonical_json, is_finite_number, is_int, load_json
from .errors import E_INVALID_QUERY, GraphStoreError, require
from .model import ClockConfig
from .package import load_package
from .storage import GraphStore
from .traverse import run_traversal


def load_clock_config(path: Path) -> ClockConfig:
    document = load_json(path, label="logical clock injection")
    require(
        isinstance(document, dict) and document.get("schema") == "logical-clock/v1",
        E_INVALID_QUERY,
        "clock config schema must be logical-clock/v1",
        path=str(path),
    )
    anchor_ms = document.get("anchor_ms")
    step_ms = document.get("step_ms")
    max_rollback_ms = document.get("max_rollback_ms")
    require(
        is_int(anchor_ms) and anchor_ms >= 0,
        E_INVALID_QUERY,
        "clock anchor_ms must be a non-negative integer",
    )
    require(
        is_int(step_ms) and step_ms > 0,
        E_INVALID_QUERY,
        "clock step_ms must be a positive integer",
    )
    require(
        is_int(max_rollback_ms) and max_rollback_ms >= 0,
        E_INVALID_QUERY,
        "clock max_rollback_ms must be a non-negative integer",
    )
    conversion = document.get("v1_conversion")
    require(
        isinstance(conversion, dict) and isinstance(conversion.get("scale"), dict),
        E_INVALID_QUERY,
        "clock v1_conversion.scale is required",
    )
    numerator = conversion["scale"].get("numerator")
    denominator = conversion["scale"].get("denominator")
    rounding = conversion.get("rounding")
    require(
        is_int(numerator) and numerator > 0,
        E_INVALID_QUERY,
        "v1 scale numerator must be a positive integer",
    )
    require(
        is_int(denominator) and denominator > 0,
        E_INVALID_QUERY,
        "v1 scale denominator must be a positive integer",
    )
    require(
        rounding in {"floor", "ceil"},
        E_INVALID_QUERY,
        "v1 rounding must be floor or ceil",
    )
    return ClockConfig(
        anchor_ms=anchor_ms,
        step_ms=step_ms,
        max_rollback_ms=max_rollback_ms,
        v1_scale_numerator=numerator,
        v1_scale_denominator=denominator,
        v1_rounding=rounding,
    )


def _workspace_path(workspace: Path, value: str, field: str) -> Path:
    candidate = Path(value)
    require(
        not candidate.is_absolute(),
        E_INVALID_QUERY,
        f"{field} must be a workspace-relative path",
        value=value,
    )
    workspace = workspace.resolve()
    resolved = (workspace / candidate).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise GraphStoreError(
            E_INVALID_QUERY,
            f"{field} escapes the workspace",
            value=value,
        ) from exc
    return resolved


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.write(canonical_json(value) + "\n")


def _replace(args: argparse.Namespace, store: GraphStore) -> dict[str, Any]:
    package_path = _workspace_path(
        Path(args.workspace), args.package, "package"
    )
    package = load_package(package_path, store.clock)
    outcome = store.replace(package, downgrade_policy=args.downgrade_policy)
    return {
        "command": getattr(args, "command", "replace"),
        "result": outcome.to_dict(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="graphstore",
        description="Deterministic two-generation graph snapshot engine",
    )
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--db", default="data/graphstore.sqlite3")
    parser.add_argument("--clock", default="samples/clock.json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load")
    load.add_argument("--package", required=True)
    load.add_argument(
        "--downgrade-policy", choices=["reject", "allow"], default="reject"
    )

    replace = subparsers.add_parser("replace")
    replace.add_argument("--package", required=True)
    replace.add_argument(
        "--downgrade-policy", choices=["reject", "allow"], default="reject"
    )

    merge = subparsers.add_parser("merge")
    merge.add_argument("--package", required=True)
    merge.add_argument(
        "--downgrade-policy", choices=["reject", "allow"], default="reject"
    )

    traverse = subparsers.add_parser("traverse")
    traverse.add_argument("--batch-id", required=True)
    traverse.add_argument(
        "--kind",
        required=True,
        choices=["exact", "label_prefix", "reachable", "filtered", "one_hop", "two_hop"],
    )
    traverse.add_argument("--node-id")
    traverse.add_argument("--prefix")
    traverse.add_argument("--depth", type=int)
    traverse.add_argument("--edge-label", action="append")
    traverse.add_argument("--min-weight", type=float)
    traverse.add_argument("--max-weight", type=float)
    traverse.add_argument("--include-tombstones", action="store_true")

    resume = subparsers.add_parser("resume")
    resume_group = resume.add_mutually_exclusive_group(required=True)
    resume_group.add_argument("--batch-id")
    resume_group.add_argument("--package")
    resume.add_argument(
        "--downgrade-policy", choices=["reject", "allow"], default="reject"
    )

    subparsers.add_parser("status")

    verify = subparsers.add_parser("verify")
    verify.add_argument("--batch-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        workspace = Path(args.workspace).resolve()
        clock_path = _workspace_path(workspace, args.clock, "clock")
        db_path = _workspace_path(workspace, args.db, "db")
        clock = load_clock_config(clock_path)
        store = GraphStore(db_path, clock)
        if args.command in {"load", "replace"}:
            _emit(_replace(args, store))
        elif args.command == "merge":
            package_path = _workspace_path(workspace, args.package, "package")
            package = load_package(package_path, clock)
            decision = store.classify_package(
                package, downgrade_policy=args.downgrade_policy
            )
            _emit(
                {
                    "command": "merge",
                    "decision": decision,
                    "merge": {
                        "adjacency_digest": package.index.adjacency_digest(),
                        "edge_count": package.index.edge_count,
                        "migration_count": len(package.index.audit.migrations),
                        "node_count": package.index.node_count,
                        "package_id": package.package_id,
                        "package_version": package.package_version,
                        "tombstone_count": package.index.tombstone_count,
                    },
                }
            )
        elif args.command == "traverse":
            query: dict[str, Any] = {
                "kind": args.kind,
                "node_id": args.node_id,
                "prefix": args.prefix,
                "depth": args.depth,
                "edge_labels": args.edge_label,
                "min_weight": args.min_weight,
                "max_weight": args.max_weight,
                "include_tombstones": args.include_tombstones,
            }
            outcome = run_traversal(
                store, batch_id=args.batch_id, query=query
            )
            _emit(outcome.result)
        elif args.command == "resume":
            if args.package:
                _emit(_replace(args, store))
            else:
                outcome = run_traversal(
                    store, batch_id=args.batch_id, resume_only=True
                )
                _emit(outcome.result)
        elif args.command == "status":
            _emit(store.status())
        elif args.command == "verify":
            _emit(store.verify(args.batch_id))
        else:
            parser.error(f"unsupported command: {args.command}")
        return 0
    except GraphStoreError as exc:
        sys.stderr.write(canonical_json(exc.to_dict()) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

