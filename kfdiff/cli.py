"""Command line interface for the keyframe diffusion studio."""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import KFError


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _studio(root: str, overrides: Optional[Dict[str, Any]] = None):
    from .engine import Studio

    return Studio(root, config_overrides=overrides)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kfdiff",
        description="Deterministic keyframe chunked-diffusion studio",
    )
    parser.add_argument("--root", default=".", help="workspace root (default: cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="generate deterministic data/*.json inputs")
    sub.add_parser("list", help="list clips and readiness")

    plan = sub.add_parser("plan", help="show the trade-off adjudication plan")
    plan.add_argument("clip_id")
    plan.add_argument("--grid", type=int, default=None)
    plan.add_argument("--budget", type=float, default=None)
    plan.add_argument("--priority", default=None,
                      help="comma separated priority dimensions")
    plan.add_argument("--json", action="store_true")

    gen = sub.add_parser("generate", help="generate one clip or all clips")
    gen.add_argument("clip_id", nargs="?", default=None)
    gen.add_argument("--all", action="store_true")
    gen.add_argument("--grid", type=int, default=None)
    gen.add_argument("--serial", action="store_true")
    gen.add_argument("--json", action="store_true")

    query = sub.add_parser("query", help="query ready keyframes")
    query.add_argument("clip_id")
    query.add_argument("--json", action="store_true")

    sub.add_parser("events", help="list deterministic revision events")

    revise = sub.add_parser("revise", help="apply a revision event and regenerate")
    revise.add_argument("event_id")
    revise.add_argument("--no-generate", action="store_true")
    revise.add_argument("--json", action="store_true")

    check = sub.add_parser("selfcheck", help="run cross-module consistency self-check")
    check.add_argument("--json", action="store_true")

    acc = sub.add_parser("acceptance", help="run full local acceptance verification")
    acc.add_argument("--no-reset", action="store_true")

    serve = sub.add_parser("serve", help="start the local web application")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8931)

    snaps = sub.add_parser("snapshots", help="list snapshot versions for a clip")
    snaps.add_argument("clip_id")

    rollback = sub.add_parser("rollback", help="attempt a backward rollback (rejected)")
    rollback.add_argument("clip_id")
    rollback.add_argument("version", type=int)

    sub.add_parser("shell", help="interactive command shell")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except KFError as exc:
        _print_json(exc.to_envelope())
        return 1
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "error": {"code": "E_NOT_FOUND",
                                                 "message": str(exc)}}, ensure_ascii=False))
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "seed":
        from .seed import generate_seed_files

        info = generate_seed_files(args.root)
        _print_json({"ok": True, "seeded": info})
        return 0

    studio = _studio(args.root)

    if args.command == "list":
        _print_json({"ok": True, "clips": studio.list_clips()})
        return 0

    if args.command == "plan":
        priority = args.priority.split(",") if args.priority else None
        plan = studio.build_plan(args.clip_id, grid=args.grid,
                                 budget_ms=args.budget, priority_order=priority)
        if args.json:
            _print_json({"ok": True, "plan": plan})
        else:
            _print_plan(plan)
        return 0

    if args.command == "generate":
        if args.all:
            rows = []
            for clip_id in sorted(studio.tasks):
                rows.append(_generate_one(studio, clip_id, args.grid, args.serial))
            _print_generation_summary(rows)
            if args.json:
                _print_json({"ok": True, "results": rows})
        else:
            if not args.clip_id:
                print("generate requires CLIP_ID or --all", file=sys.stderr)
                return 2
            result = _generate_one(studio, args.clip_id, args.grid, args.serial)
            if args.json:
                _print_json({"ok": True, "result": result})
            else:
                _print_generation_summary([result])
        return 0

    if args.command == "query":
        result = studio.query_ready(args.clip_id)
        if args.json:
            _print_json({"ok": True, "query": result})
        else:
            print(f"{result['clip_id']} ready {result['ready_count']}/"
                  f"{result['total_keyframes']} complete={result['complete']} "
                  f"snapshot=v{result['snapshot_version']}")
            for kf in result["keyframes"]:
                print(f"  ord={kf['ordinal']:2d} frame={kf['frame_index']:3d} "
                      f"block={kf['block_index']} range={kf['value_interval']} "
                      f"mean={kf['mean']} sha={kf['content_hash'][:12]}")
        return 0

    if args.command == "events":
        _print_json({"ok": True, "events": studio.revision_events()})
        return 0

    if args.command == "revise":
        applied = studio.apply_revision_event(args.event_id)
        clip_id = applied["event"]["clip_id"]
        result = None
        if not args.no_generate:
            result = _generate_one(studio, clip_id, None, False)
        if args.json:
            _print_json({"ok": True, "applied": applied, "generation": result})
        else:
            print(f"applied {args.event_id} -> {clip_id} revision "
                  f"{studio.tasks[clip_id].revision}")
            if result:
                _print_propagation(result["propagation"])
        return 0

    if args.command == "selfcheck":
        from .selfcheck import run_selfcheck

        report = run_selfcheck(studio, raise_on_error=False)
        if args.json:
            _print_json(report)
        else:
            print(f"selfcheck ok={report['ok']} passed={report['checks_passed']}/"
                  f"{report['checks_total']} failed={report['checks_failed']}")
            for check in report["checks"]:
                if check["status"] == "fail":
                    print("  FAIL", check["check"], check["clip_id"], check["detail"])
        return 0 if report["ok"] else 1

    if args.command == "acceptance":
        from .acceptance import run_acceptance, write_report

        report = run_acceptance(args.root, reset_cache=not args.no_reset)
        path = write_report(report, Path(args.root) / "records")
        print(f"acceptance {'PASS' if report['overall_pass'] else 'FAIL'}: "
              f"{report['passed_count']}/{report['assertion_count']} assertions; "
              f"report={path}")
        for assertion in report["assertions"]:
            mark = "PASS" if assertion["passed"] else "FAIL"
            print(f"  [{mark}] {assertion['name']}")
        return 0 if report["overall_pass"] else 1

    if args.command == "serve":
        from .webapp import serve

        serve(studio, host=args.host, port=args.port)
        return 0

    if args.command == "snapshots":
        _print_json({"ok": True, "clip_id": args.clip_id,
                     "current": studio.snapshots.current_version(args.clip_id),
                     "versions": studio.snapshots.list_versions(args.clip_id)})
        return 0

    if args.command == "rollback":
        studio.snapshots.request_rollback(args.clip_id, args.version)
        return 0

    if args.command == "shell":
        return _shell(args.root)

    parser.error(f"unhandled command {args.command}")
    return 2


def _generate_one(studio, clip_id: str, grid: Optional[int], serial: bool) -> Dict[str, Any]:
    return studio.generate(clip_id, grid=grid, parallel=not serial)


def _print_plan(plan: Dict[str, Any]) -> None:
    print(f"plan {plan['clip_id']} req_g={plan['requested_block_grid']} "
          f"-> g={plan['selected_block_grid']} strength={plan['selected_strength']} "
          f"eff_steps={plan['selected_effective_steps']} status={plan['status']}")
    risk = plan["risk_summary"]
    est = plan["estimate"]
    print(f"  risk={risk['risk']} (limit {risk['risk_limit']}, "
          f"boundaries {risk['boundary_count']}, states {risk['boundary_coupling_states']})")
    print(f"  estimate={est['estimated_first_query_ms']} ms vs budget {est['budget_ms']} ms "
          f"[base {est['base_ms']} + kf {est.get('keyframe_overhead_term_ms')} + "
          f"pixel-step {est['pixel_step_term_ms']} + coupling {est['coupling_term_ms']}]")
    for reason in plan["reasons"]:
        print(f"  - {reason['code']}: {reason['detail']}")


def _print_generation_summary(rows: List[Dict[str, Any]]) -> None:
    for result in rows:
        timing = result.get("first_block_timing") or {}
        plan = result["plan"]
        print(f"{result['clip_id']}: snapshot=v{result['snapshot_version']} "
              f"g={plan['selected_block_grid']} strength={plan['selected_strength']} "
              f"first_block={timing.get('observed_first_block_ms')} ms "
              f"within_budget={timing.get('within_budget')}")
        _print_propagation(result["propagation"], indent="  ")


def _print_propagation(prop: Dict[str, Any], indent: str = "") -> None:
    recomputed = [k["ordinal"] for k in prop["recomputed_keyframes"]]
    reused = [k["ordinal"] for k in prop["reused_keyframes"]]
    print(f"{indent}recomputed_kf={recomputed} reused_kf={reused}")
    print(f"{indent}blocks recompute={prop['recomputed_blocks']} "
          f"reuse={prop['reused_blocks']} segments recompute={prop['recomputed_segments']}")


def _shell(root: str) -> int:
    print("kfdiff interactive shell. Type 'help' for commands, 'exit' to quit.")
    while True:
        try:
            line = input("kfdiff> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("exit", "quit"):
            return 0
        if line == "help":
            print("commands: list, plan CLIP, generate CLIP [--serial], generate --all, "
                  "query CLIP, events, revise EVENT, selfcheck, dashboard, exit")
            continue
        try:
            argv = shlex.split(line)
            if argv and argv[0] == "dashboard":
                argv = ["list"]
            main(["--root", root] + argv)
        except SystemExit:
            continue


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
