from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import ScreenError, error_json
from .generator import DEFAULT_SEED, generate_sample
from .persist import EXIT_INTERRUPTED, atomic_write_json, recover_chunks, version_id_of
from .pipeline import InterruptFlag, prepare_data, run_pipeline
from .validate import load_events, load_manifest


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _redact_report(report: dict[str, Any], full: bool = False) -> dict[str, Any]:
    if full:
        return report
    redacted = dict(report)
    verification = dict(report.get("verification", {}))
    verification.pop("replay", None)
    redacted["verification"] = verification
    return redacted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="multiscreen", description="多屏推流状态机与快照重放验收工具")
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="生成固定种子本地样本")
    generate.add_argument("--out", default="task")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)

    validate = sub.add_parser("validate", help="只解析和校验输入，不写状态")
    validate.add_argument("--data", default="task")
    validate.add_argument("--workers", type=int, default=1)
    validate.add_argument("--budget-bytes", type=int, default=64 * 1024 * 1024)

    merge = sub.add_parser("merge", help="校验摘要并合并两代快照")
    merge.add_argument("--data", default="task")
    merge.add_argument("--workers", type=int, default=4)
    merge.add_argument("--budget-bytes", type=int, default=64 * 1024 * 1024)

    verify = sub.add_parser("verify", help="重放并裁决合并视图")
    verify.add_argument("--data", default="task")
    verify.add_argument("--workers", type=int, default=1)
    verify.add_argument("--budget-bytes", type=int, default=64 * 1024 * 1024)
    verify.add_argument("--full", action="store_true")

    run = sub.add_parser("run", help="完整流程并原子写分块状态")
    run.add_argument("--data", default="task")
    run.add_argument("--state", default="state")
    run.add_argument("--report-out", default="output/report.json")
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--budget-bytes", type=int, default=64 * 1024 * 1024)
    run.add_argument("--chunks", type=int, default=4)
    run.add_argument("--interrupt-after-chunks", type=int, default=None)
    run.add_argument("--full-report", action="store_true")

    recover = sub.add_parser("recover", help="仅恢复摘要有效的连续分块")
    recover.add_argument("--data", default="task")
    recover.add_argument("--state", default="state")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "generate":
            result = generate_sample(args.out, args.seed)
            print_json({"ok": True, "result": result})
            return 0

        if args.command == "validate":
            prep = prepare_data(args.data, workers=args.workers, budget_bytes=args.budget_bytes)
            print_json(
                {
                    "ok": True,
                    "version_id": prep["version_id"],
                    "sessions": len(prep["manifest"]["sessions"]),
                    "events": len(prep["events"]),
                    "receivers": sorted(prep["view"]),
                    "selected_v2_set_id": prep["selected_v2_set_id"],
                    "v1_loaded": prep["v1_loaded"],
                    "rejected_snapshots": prep["rejected_snapshots"],
                }
            )
            return 0

        if args.command == "merge":
            prep = prepare_data(args.data, workers=args.workers, budget_bytes=args.budget_bytes)
            print_json(
                {
                    "ok": True,
                    "version_id": prep["version_id"],
                    "fallback": prep["fallback"],
                    "rejected_snapshots": prep["rejected_snapshots"],
                    "view": prep["view"],
                    "conflicts": prep["conflicts"],
                    "adjudications": prep["adjudications"],
                }
            )
            return 0

        if args.command == "verify":
            prep = prepare_data(args.data, workers=args.workers, budget_bytes=args.budget_bytes)
            from .verify import verify_replay

            report = verify_replay(
                prep["view"],
                prep["events"],
                prep["manifest"],
                prep["records"],
                workers=args.workers,
                budget_bytes=args.budget_bytes,
                auto_switch_on_breach=prep["auto_switch_on_breach"],
            )
            print_json(_redact_report(report, args.full))
            return 0 if report["ok"] else 2

        if args.command == "run":
            flag = InterruptFlag()
            flag.install()
            report = run_pipeline(
                args.data,
                args.state,
                workers=args.workers,
                budget_bytes=args.budget_bytes,
                chunk_count=args.chunks,
                interrupt_flag=flag,
                interrupt_after_chunks=args.interrupt_after_chunks,
            )
            if report.get("interrupted"):
                print_json(
                    {
                        "ok": False,
                        "code": "E_INTERRUPT",
                        "exit_code": report["exit_code"],
                        "marker_path": report["marker_path"],
                        "next_chunk_index": report["next_chunk_index"],
                        "chunks_written": report["chunks_written"],
                        "version_id": report["version_id"],
                    }
                )
                return int(report["exit_code"])
            if args.report_out:
                atomic_write_json(args.report_out, _redact_report(report, args.full_report))
            print_json(_redact_report(report, args.full_report))
            return 0 if report["ok"] else 2

        if args.command == "recover":
            data = Path(args.data)
            manifest = load_manifest(data / "sessions.json")
            events = load_events(data / "frames.jsonl", manifest)
            version_id = version_id_of(manifest, events)
            recovery = recover_chunks(args.state, version_id)
            print_json(
                {
                    "ok": True,
                    "version_id": version_id,
                    "chunk_count": recovery["chunk_count"],
                    "chunks": recovery["chunks"],
                    "rejected": recovery["rejected"],
                    "final_states": recovery["last_state_by_receiver"],
                }
            )
            return 0

        return 2
    except ScreenError as exc:
        print(error_json(exc))
        return 1
    except Exception as exc:
        print(error_json(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
