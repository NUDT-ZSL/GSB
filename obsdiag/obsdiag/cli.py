"""命令行入口：所有演示能力均可无 GUI 复现。"""
from __future__ import annotations

import argparse
import os
import sys

from .errors import ObsDiagError
from .injection import inject_gap, inject_order
from .loader import load_observations, load_rules, load_topology
from .persistence import WorkbenchStore
from .pipeline import (
    run_pipeline,
    run_topology_evolution,
    rerun_gap_scenario,
    rerun_order_scenario,
)
from .recovery import recover_session, render_recovery_report
from .reporting import render_canonical, render_summary


def _write_report(output_dir: str, name: str, report) -> tuple[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    pretty_path = os.path.join(output_dir, f"{name}.summary.txt")
    canonical_path = os.path.join(output_dir, f"{name}.canonical.json")
    with open(pretty_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_summary(report) + "\n")
    with open(canonical_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_canonical(report) + "\n")
    return pretty_path, canonical_path


def _default_db(workdir: str) -> str:
    return os.path.join(workdir, "workbench.sqlite3")


def cmd_validate(args: argparse.Namespace) -> int:
    topology = load_topology(args.topology)
    ruleset = load_rules(args.rules, topology)
    observations = load_observations(args.observations, topology, ruleset)
    print(
        f"OK topology=v{topology.version} rules=v{ruleset.version} "
        f"observations={len(observations)}"
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    store = WorkbenchStore(_default_db(args.workdir))
    report, _, _, _ = run_pipeline(
        args.topology,
        args.rules,
        args.observations,
        args.window_ms,
        concurrency=args.concurrency,
        tag=args.tag,
        store=store,
    )
    pretty_path, canonical_path = _write_report(args.workdir, args.tag, report)
    print(render_summary(report))
    print(f"report={pretty_path}")
    print(f"canonical={canonical_path}")
    store.close()
    return 0


def cmd_inject(args: argparse.Namespace) -> int:
    store = WorkbenchStore(_default_db(args.workdir))
    baseline, topology, _, _ = run_pipeline(
        args.topology,
        args.rules,
        args.observations,
        args.window_ms,
        concurrency=args.concurrency,
        tag="baseline",
        store=store,
    )
    _write_report(args.workdir, "01-baseline", baseline)

    if args.kind == "E_ORDER":
        shuffled_path = os.path.join(args.workdir, "observations.e_order.ndjson")
        injection, _ = inject_order(args.observations, shuffled_path, seed=args.seed)
        rerun, verified = rerun_order_scenario(
            baseline,
            args.topology,
            args.rules,
            shuffled_path,
            args.window_ms,
            args.concurrency,
            injection,
            store=store,
        )
    elif args.kind == "E_GAP":
        gapped_path = os.path.join(args.workdir, "observations.e_gap.ndjson")
        injection, _ = inject_gap(
            args.observations, gapped_path, args.service, topology
        )
        rerun, verified = rerun_gap_scenario(
            baseline,
            topology,
            args.topology,
            args.rules,
            gapped_path,
            args.window_ms,
            args.concurrency,
            injection,
            args.service,
            store=store,
        )
    else:
        raise ObsDiagError(args.kind, "use 'evolve' command for E_TOPO")
    _write_report(args.workdir, f"injection-{args.kind.lower()}", rerun)
    print(render_summary(rerun))
    print(f"verified={verified.verified} detail={verified.detail}")
    store.close()
    return 0 if verified.verified else 3


def cmd_evolve(args: argparse.Namespace) -> int:
    store = WorkbenchStore(_default_db(args.workdir))
    baseline, topology, _, _ = run_pipeline(
        args.topology,
        args.rules,
        args.observations,
        args.window_ms,
        concurrency=args.concurrency,
        tag="baseline",
        store=store,
    )
    _write_report(args.workdir, "01-baseline", baseline)
    rerun, _, verified, diff = run_topology_evolution(
        baseline,
        topology,
        args.topology,
        args.rules,
        args.observations,
        args.changeset,
        args.window_ms,
        args.concurrency,
        store=store,
    )
    _write_report(args.workdir, "03-topo-v2", rerun)
    print(render_summary(rerun))
    print(
        f"verified={verified.verified} invalidated={list(diff.invalidated_cluster_ids)} "
        f"retained={list(diff.retained_cluster_ids)} new={list(diff.new_cluster_ids)}"
    )
    store.close()
    return 0 if verified.verified else 3


def cmd_recover(args: argparse.Namespace) -> int:
    store = WorkbenchStore(_default_db(args.workdir))
    result = recover_session(store, args.session_id)
    print(render_recovery_report(result))
    store.close()
    return 0


def cmd_crash_demo(args: argparse.Namespace) -> int:
    """模拟强制终止：先写入 RUNNING 会话与游标，不写快照，再执行恢复。"""
    store = WorkbenchStore(_default_db(args.workdir))
    topology = load_topology(args.topology)
    ruleset = load_rules(args.rules, topology)
    observations = load_observations(args.observations, topology, ruleset)
    session_id = "SESS-CRASH-DEMO"
    store.begin_session(
        session_id,
        topology.version,
        ruleset.version,
        len(observations),
        args.window_ms,
        "crash-demo-fingerprint",
    )
    partial_cursor = max(1, len(observations) // 2)
    store.save_checkpoint(session_id, partial_cursor, 0, "simulated force-kill boundary")
    # 故意不调用 complete_session：等价于进程在此处被 kill -9
    store.close()

    second_store = WorkbenchStore(_default_db(args.workdir))
    result = recover_session(second_store, session_id)
    print(render_recovery_report(result))
    second_store.close()
    return 0 if result.rerun_required else 4


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import launch_gui

    launch_gui(args.workdir)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsdiag",
        description="deterministic alert correlation & root-cause diagnosis workbench",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--topology", required=True)
        target.add_argument("--rules", required=True)
        target.add_argument("--observations", required=True)
        target.add_argument("--window-ms", type=int, default=5000)
        target.add_argument("--concurrency", type=int, default=1)
        target.add_argument("--workdir", default="output")

    validate = sub.add_parser("validate", help="strong-validate inputs (E_INPUT on failure)")
    add_common(validate)
    validate.set_defaults(func=cmd_validate)

    run = sub.add_parser("run", help="full baseline correlation pipeline")
    add_common(run)
    run.add_argument("--tag", default="baseline")
    run.set_defaults(func=cmd_run)

    inject = sub.add_parser("inject", help="inject E_ORDER or E_GAP and verify effect")
    add_common(inject)
    inject.add_argument("--kind", choices=["E_ORDER", "E_GAP"], required=True)
    inject.add_argument("--service", default="payment-db")
    inject.add_argument("--seed", type=int, default=20261117)
    inject.set_defaults(func=cmd_inject)

    evolve = sub.add_parser("evolve", help="apply topology changeset (E_TOPO)")
    add_common(evolve)
    evolve.add_argument("--changeset", required=True)
    evolve.set_defaults(func=cmd_evolve)

    recover = sub.add_parser("recover", help="recover a session from SQLite")
    recover.add_argument("--session-id", required=True)
    recover.add_argument("--workdir", default="output")
    recover.set_defaults(func=cmd_recover)

    crash = sub.add_parser("crash-demo", help="simulate force-kill and recovery")
    add_common(crash)
    crash.set_defaults(func=cmd_crash_demo)

    gui = sub.add_parser("gui", help="launch Tkinter workbench")
    gui.add_argument("--workdir", default="output")
    gui.set_defaults(func=cmd_gui)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ObsDiagError as exc:
        print(exc.render(), file=sys.stderr)
        return 2
    except (ValueError, KeyError, FileNotFoundError) as exc:
        print(f"E_INPUT: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
