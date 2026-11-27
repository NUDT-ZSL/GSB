"""命令行入口。

用法：
  python -m parksim gen --seed 42 --scenario normal --out samples/normal.json
  python -m parksim run config.json [--policy skip|truncate|abort] [--json out.json] [--text out.txt]
  python -m parksim diff old.json new.json          # 参数修改后对比，列出变化回合
  python -m parksim check config.json               # 仅做输入校验
"""
from __future__ import annotations

import argparse
import sys

from .configio import dump_scenario, load_scenario
from .engine import changed_rounds, simulate
from .generator import generate
from .report import dump_report, render_text, report_to_json
from .validate import ValidationError


def _cmd_gen(args) -> int:
    sc = generate(seed=args.seed, scenario=args.scenario)
    dump_scenario(sc, args.out)
    print(f"已生成样本: {args.out}（seed={args.seed}, scenario={args.scenario}）")
    return 0


def _cmd_run(args) -> int:
    try:
        sc = load_scenario(args.config)
    except ValidationError as exc:
        print(f"校验失败: {exc}", file=sys.stderr)
        return 2
    rep = simulate(sc, policy=args.policy)
    text = render_text(rep)
    if args.text:
        with open(args.text, "w", encoding="utf-8") as fh:
            fh.write(text)
    if args.json:
        dump_report(rep, args.json)
    if not args.text:
        print(text, end="")
    # 确定性自检：同输入再跑一次，序列化结果必须逐字节一致
    assert report_to_json(simulate(sc, policy=args.policy)) == report_to_json(rep)
    return 0


def _cmd_diff(args) -> int:
    try:
        old = load_scenario(args.old)
        new = load_scenario(args.new)
    except ValidationError as exc:
        print(f"校验失败: {exc}", file=sys.stderr)
        return 2
    rep_old = simulate(old, policy=args.policy)
    rep_new = simulate(new, policy=args.policy)
    changed = changed_rounds(rep_old, rep_new)
    print(f"旧整体评分: {rep_old.overall_score}  新整体评分: {rep_new.overall_score}")
    if changed:
        print(f"消耗明细发生变化的回合: {changed}")
    else:
        print("所有回合消耗明细均无变化")
    if args.json:
        dump_report(rep_new, args.json)
    return 0


def _cmd_check(args) -> int:
    try:
        load_scenario(args.config)
    except ValidationError as exc:
        print(f"校验失败: {exc}", file=sys.stderr)
        return 2
    print("校验通过")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="parksim", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("gen", help="生成确定性样本配置")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--scenario", choices=["normal", "overflow"], default="normal")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=_cmd_gen)

    p = sub.add_parser("run", help="推演并输出报告")
    p.add_argument("config")
    p.add_argument("--policy", choices=["skip", "truncate", "abort"], default="skip")
    p.add_argument("--json", help="报告 JSON 输出路径")
    p.add_argument("--text", help="报告文本输出路径")
    p.set_defaults(fn=_cmd_run)

    p = sub.add_parser("diff", help="对比修改前后两份配置，列出变化回合")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--policy", choices=["skip", "truncate", "abort"], default="skip")
    p.add_argument("--json", help="新配置报告 JSON 输出路径")
    p.set_defaults(fn=_cmd_diff)

    p = sub.add_parser("check", help="仅校验配置")
    p.add_argument("config")
    p.set_defaults(fn=_cmd_check)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
