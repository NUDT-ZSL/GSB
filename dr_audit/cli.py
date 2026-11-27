"""命令行入口：工程生成、报表核算、独立自检。"""

import argparse
import json
import sys

from .generator import generate_project
from .reference import check_report
from .report import build_report
from .selfcheck import run_self_check


def _load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _dump_json(payload, path):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")


def _print_json(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_generate(args):
    project = generate_project(
        args.seed,
        n_sites=args.sites,
        batches_per_element=args.batches,
    )
    if args.output:
        _dump_json(project, args.output)
        print(f"工程已生成: {args.output}")
    else:
        _print_json(project)
    return 0


def cmd_report(args):
    project = _load_json(args.project)
    report = build_report(project)
    if args.output:
        _dump_json(report, args.output)
        print(f"报表已生成: {args.output}")
    else:
        _print_json(report)
    return 0


def cmd_check(args):
    project = _load_json(args.project)
    report = build_report(project)
    result = check_report(project, report)
    payload = {
        "project": args.project,
        "ok": result["ok"],
        "first_mismatch": result.get("first_mismatch"),
        "stats": {
            "gap_intervals": len(report["gap_intervals"]),
            "violated_gaps": sum(
                1 for item in report["gap_intervals"] if item["adjudication"]["violated"]
            ),
            "actions": len(report["actions"]),
            "recovery_events": len(report["recovery_settlements"]),
        },
    }
    if args.output:
        _dump_json(payload, args.output)
    _print_json(payload)
    return 0 if result["ok"] else 2


def cmd_selfcheck(args):
    result = run_self_check(seed=args.seed, n_sites=args.sites, batches_per_element=args.batches)
    if args.output:
        _dump_json(result, args.output)
    _print_json(result)
    return 0 if result["passed"] else 2


def cmd_run(args):
    project = generate_project(
        args.seed,
        n_sites=args.sites,
        batches_per_element=args.batches,
    )
    if args.project_output:
        _dump_json(project, args.project_output)
    report = build_report(project)
    if args.output:
        _dump_json(report, args.output)
    result = check_report(project, report)
    summary = {
        "seed": args.seed,
        "reference_check": result,
        "stats": {
            "sites": len(project["sites"]),
            "manifest_records": len(project["manifest"]),
            "gap_intervals": len(report["gap_intervals"]),
            "violated_gaps": sum(
                1 for item in report["gap_intervals"] if item["adjudication"]["violated"]
            ),
            "actions": len(report["actions"]),
            "unmet_sites_latest": (
                len(report["recovery_settlements"][-1]["unmet_sites"])
                if report["recovery_settlements"]
                else 0
            ),
        },
        "outputs": {
            "project": args.project_output,
            "report": args.output,
        },
    }
    _print_json(summary)
    return 0 if result["ok"] else 2


def build_parser():
    parser = argparse.ArgumentParser(
        prog="dr_audit",
        description="归档数据灾备缺口核算与恢复点结算组件（纯标准库）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="确定性生成归档工程")
    gen.add_argument("--seed", type=int, default=20261128)
    gen.add_argument("--sites", type=int, default=8)
    gen.add_argument("--batches", type=int, default=24)
    gen.add_argument("-o", "--output", help="工程 JSON 输出路径")
    gen.set_defaults(func=cmd_generate)

    rep = sub.add_parser("report", help="读取工程并输出缺口 / 恢复点报表")
    rep.add_argument("project", help="工程 JSON 路径")
    rep.add_argument("-o", "--output", help="报表 JSON 输出路径")
    rep.set_defaults(func=cmd_report)

    chk = sub.add_parser("check", help="独立参考计算器复核主流程报表")
    chk.add_argument("project", help="工程 JSON 路径")
    chk.add_argument("-o", "--output", help="核对结果 JSON 输出路径")
    chk.set_defaults(func=cmd_check)

    sel = sub.add_parser("selfcheck", help="运行内置参考核对与人工样本自检")
    sel.add_argument("--seed", type=int, default=20261128)
    sel.add_argument("--sites", type=int, default=8)
    sel.add_argument("--batches", type=int, default=24)
    sel.add_argument("-o", "--output", help="自检结果 JSON 输出路径")
    sel.set_defaults(func=cmd_selfcheck)

    run = sub.add_parser("run", help="生成工程 + 主流程报表 + 独立核对一体化")
    run.add_argument("--seed", type=int, default=20261128)
    run.add_argument("--sites", type=int, default=8)
    run.add_argument("--batches", type=int, default=24)
    run.add_argument("--project-output", default="project.json")
    run.add_argument("-o", "--output", default="report.json")
    run.set_defaults(func=cmd_run)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

