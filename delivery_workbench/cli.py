from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .canon import canonical_dumps
from .engine import (
    DEFAULT_AS_OF_CLOCK_MS,
    DEFAULT_CONCURRENCY,
    DEFAULT_INJECTION_CLOCK_MS,
    DEFAULT_TIME_BUDGET_MS,
    Engine,
)
from .errors import Code, WorkbenchError, exit_code_for
from .verifier import verify_workbench


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sample-dir", default=argparse.SUPPRESS, help="deterministic sample directory")
    parser.add_argument("--db", default=argparse.SUPPRESS, help="SQLite state database path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workbench", description="Message delivery semantics visualization and diagnosis workbench")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--sample-dir", default="samples", help="deterministic sample directory")
    parser.add_argument("--db", default="state/workbench.sqlite3", help="SQLite state database path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="parse, validate and atomically load all inputs")
    _add_common(load)

    merge = subparsers.add_parser("merge", help="merge v1/v2 snapshots or apply a versioned package")
    _add_common(merge)
    merge.add_argument("--package", default=None, help="optional semantic snapshot package directory")
    merge.add_argument("--injection-clock-ms", type=int, default=DEFAULT_INJECTION_CLOCK_MS)

    diagnose = subparsers.add_parser("diagnose", help="compute watermarks, closures and diagnostic schedule")
    _add_common(diagnose)
    diagnose.add_argument("--clock-ms", type=int, default=DEFAULT_AS_OF_CLOCK_MS, dest="clock_ms")
    diagnose.add_argument("--injection-clock-ms", type=int, default=DEFAULT_INJECTION_CLOCK_MS)
    diagnose.add_argument("--budget-ms", type=int, default=DEFAULT_TIME_BUDGET_MS)
    diagnose.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="per-partition concurrency cap")
    diagnose.add_argument("--subscriber-id", default=None)

    patch = subparsers.add_parser("patch", help="generate incremental subscription patches")
    _add_common(patch)
    patch.add_argument("--clock-ms", type=int, default=DEFAULT_AS_OF_CLOCK_MS)
    patch.add_argument("--injection-clock-ms", type=int, default=DEFAULT_INJECTION_CLOCK_MS)
    patch.add_argument("--subscriber-id", default=None)

    resume = subparsers.add_parser("resume", help="commit prepared half-committed checkpoints")
    _add_common(resume)

    status = subparsers.add_parser("status", help="show stable sorted JSON or Markdown status")
    _add_common(status)
    status.add_argument("--format", choices=["json", "markdown"], default="json")

    verify = subparsers.add_parser("verify", help="rerun the full chain in a temporary database and compare fields")
    _add_common(verify)
    return parser


def _success(command: str, result: Any) -> Dict[str, Any]:
    return {"ok": True, "code": Code.OK, "command": command, "result": result}


def render_markdown(status: Dict[str, Any]) -> str:
    lines: List[str] = ["# Delivery Workbench Status", ""]
    binding = status.get("semantic_version_binding")
    lines.append("## Semantic Binding")
    lines.append("")
    if binding is None:
        lines.append("- No semantic version is bound.")
    else:
        lines.append(f"- Binding: `{binding['binding_id']}`")
        lines.append(f"- Snapshot version: `{binding['snapshot_version']}`; generation floor: `{binding['generation_min']}`")
        lines.append(f"- View hash: `{binding.get('content_hash', '')[:16]}`")
    lines.extend(["", "## Partition Watermarks", "", "| Partition | Watermark | Backlog | Status | Blocker |", "|---|---:|---:|---|---|"])
    for record in status.get("partition_watermarks", []):
        width = record["event_count"]
        mark = min(record["watermark_offset"], width)
        bar = "█" * mark + "░" * max(0, width - mark)
        blocker = (record.get("blocker") or {}).get("code", "")
        lines.append(
            f"| `{record['partition_key']}` | {record['watermark_offset']}/{width} `{bar}` | {record['backlog']}/{record['backlog_limit']} | {record['status']} | `{blocker}` |"
        )
    lines.extend(["", "## Subscription Cursors", "", "| Subscriber | Topic | Cursor | Patches |", "|---|---|---:|---:|"])
    for cursor in status.get("subscription_cursors", []):
        lines.append(f"| `{cursor['subscriber_id']}` | `{cursor['topic']}` | {cursor['last_topic_seq']} | {cursor.get('patch_count', 0)} |")
    lines.extend(["", "## Invalidation Chains", ""])
    closures = status.get("invalidation_event_chains", [])
    if not closures:
        lines.append("- None")
    for closure in closures:
        lines.append(f"### `{closure['closure_id']}`")
        lines.append(f"- kind: `{closure.get('closure_kind')}`; affected partitions: {', '.join('`' + item + '`' for item in closure.get('affected_partitions', [])) or 'none'}")
        for root, impact in sorted(closure.get("root_impacts", {}).items()):
            lines.append(f"- `{root}` -> `{' -> '.join(impact['representative_path'])}`")
    runs = status.get("diagnostic_runs", [])
    if runs:
        lines.extend(["", "## Diagnostic Runs", ""])
        for run in runs:
            counts = run["counts"]
            lines.append(
                f"- `{run['run_id']}` budget={run['time_budget_ms']}ms used={run['budget_used_ms']}ms cap={run['concurrency_per_partition']} "
                f"success={counts['started_succeeded']} failed={counts['started_failed']} budget_unstarted={counts['budget_unstarted']} abandoned={counts['voluntarily_abandoned']}"
            )
    recovery = status.get("recovery", {})
    lines.extend(["", "## Recovery", "", f"- half-committed records: `{recovery.get('has_half_committed_record', False)}`", f"- prepared IDs: `{', '.join(recovery.get('prepared_checkpoint_ids', [])) or 'none'}`", ""])
    return "\n".join(lines)


def run(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        engine = Engine(args.sample_dir, args.db)
        if args.command == "load":
            result = engine.load()
            payload = _success("load", result)
        elif args.command == "merge":
            result = engine.merge(args.injection_clock_ms, args.package)
            payload = _success("merge", result)
        elif args.command == "diagnose":
            result = engine.diagnose(args.clock_ms, args.budget_ms, args.concurrency, args.subscriber_id, args.injection_clock_ms)
            payload = _success("diagnose", _compact_diagnose(result))
        elif args.command == "patch":
            result = engine.patch(args.subscriber_id, args.injection_clock_ms, args.clock_ms)
            payload = _success("patch", result)
        elif args.command == "resume":
            payload = _success("resume", engine.resume())
        elif args.command == "status":
            status = engine.status()
            if args.format == "markdown":
                sys.stdout.write(render_markdown(status))
                return 0
            payload = {"ok": True, "code": Code.OK, "command": "status", "status": status}
        elif args.command == "verify":
            result = verify_workbench(args.sample_dir, args.db)
            payload = {"ok": result["ok"], "code": result["code"], "command": "verify", "result": result}
            if not result["ok"]:
                print(canonical_dumps(payload, indent=2))
                return exit_code_for(Code.VERIFY_MISMATCH)
        else:  # pragma: no cover
            parser.error(f"unknown command {args.command}")
            return 2
        print(canonical_dumps(payload, indent=2))
        return 0
    except WorkbenchError as exc:
        payload = exc.to_payload()
        payload["command"] = args.command
        print(canonical_dumps(payload, indent=2))
        return exit_code_for(exc.code)
    except Exception as exc:  # deterministic CLI error boundary
        payload = {
            "ok": False,
            "code": "WB_INTERNAL_ERROR",
            "command": getattr(args, "command", None),
            "error": {"code": "WB_INTERNAL_ERROR", "message": str(exc), "type": type(exc).__name__},
        }
        print(canonical_dumps(payload, indent=2))
        return 1


def _compact_diagnose(result: Dict[str, Any]) -> Dict[str, Any]:
    run = result["diagnostic_run"]
    return {
        "commit": result["commit"],
        "recovered": result["recovered"],
        "watermark_recompute": result["watermark_recompute"],
        "watermarks": [
            {
                "partition_key": record["partition_key"],
                "watermark_offset": record["watermark_offset"],
                "backlog": record["backlog"],
                "status": record["status"],
                "blocker_code": None if record["blocker"] is None else record["blocker"]["code"],
                "content_hash": record["content_hash"],
            }
            for record in result["watermarks"]
        ],
        "patch_count": len(result["patches"]),
        "cursor_updates": result["cursor_updates"],
        "diagnostic_run": {
            "run_id": run["run_id"],
            "counts": run["counts"],
            "budget_used_ms": run["budget_used_ms"],
            "budget_remaining_ms": run["budget_remaining_ms"],
            "tasks": [
                {
                    "task_id": task["task_id"],
                    "subscriber_id": task["subscriber_id"],
                    "partition_key": task["partition_key"],
                    "stable_rank": task["stable_rank"],
                    "end_category": task["end_category"],
                    "outcome_class": task.get("outcome_class"),
                    "label_zh": task.get("label_zh"),
                    "outcome": task["outcome"],
                    "reason": task["reason"],
                    "logical_cost_ms": task["logical_cost_ms"],
                }
                for task in run["tasks"]
            ],
        },
    }


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
