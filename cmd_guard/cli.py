"""命令行入口。

用法::

    python -I auditor.py --config <规则.json> [--config <规则2.json> ...]
                         --commands <命令清单> --outdir <输出目录>
                         [--stage audit|remediated] [--retry N]

退出码：
    0  所有输入均成功读取，报告与判定文件已生成；
    1  存在缺失/损坏输入（missing_inputs 非空），已产出的部分照常落盘；
    2  参数缺失或非法（同时打印用法）。
"""

from __future__ import annotations

import argparse
import os
import sys

from .engine import (
    build_judgements,
    build_report,
    build_rule_set,
    dump_json,
    evaluate_commands,
    load_commands,
    load_sources,
)

USAGE = (
    "用法: python -I auditor.py --config <规则.json> [--config ...] "
    "--commands <命令清单> --outdir <输出目录> "
    "[--stage audit|remediated] [--retry N]"
)


def _print_usage_and_exit(code: int) -> int:
    print(USAGE, file=sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认可能为 GBK，统一为 UTF-8，保证中文提示与报告编码一致
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="auditor",
        description="离线危险命令拦截规则绕过检测与修复验证工具（无网络、仅标准库）",
        add_help=True,
    )
    parser.add_argument("--config", action="append", default=[], metavar="PATH",
                        help="规则 JSON 路径，可重复传入多个来源")
    parser.add_argument("--commands", metavar="PATH", help="命令清单路径（JSON 或每行一条的文本）")
    parser.add_argument("--outdir", metavar="DIR", help="报告与判定文件输出目录")
    parser.add_argument("--stage", choices=("audit", "remediated"), default="audit",
                        help="audit=复现历史拼接匹配并检测绕过；remediated=修复后语义判定")
    parser.add_argument("--retry", type=int, default=1, metavar="N",
                        help="配置暂不可读时的重试次数（默认 1）")

    # argparse 自带缺失参数退出码就是 2，并会打印用法；这里统一兜底
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 2

    if not args.config or not args.commands or not args.outdir:
        return _print_usage_and_exit(2)
    if args.retry < 0:
        print("[error] --retry 必须为非负整数", file=sys.stderr)
        return _print_usage_and_exit(2)

    # 1) 加载配置来源（单条失败不阻断其它来源）
    load_result = load_sources(args.config, retry_attempts=args.retry)

    # 2) 加载命令清单（持续失败同样记入 missing_inputs，不当空清单处理）
    commands, cmd_error = load_commands(args.commands)
    if commands is None:
        print(f"[error] {cmd_error}: {args.commands}，记入 missing_inputs", file=sys.stderr)
        load_result.missing_inputs.append(args.commands)
        commands = []

    # 3) 校验/去重/编译规则。已成功解析的数据不受后续坏输入影响
    rule_set = build_rule_set(load_result)

    # 4) 检测与修复后判定
    report = build_report(
        rule_set,
        commands,
        stage=args.stage,
        missing_inputs=load_result.missing_inputs,
        retried_paths=load_result.retried_paths,
    )
    verdicts = evaluate_commands(rule_set, commands)
    judgements = build_judgements(verdicts, stage=args.stage)

    # 5) 落盘两个结果文件
    os.makedirs(args.outdir, exist_ok=True)
    report_path = os.path.join(args.outdir, "detection_report.json")
    verdict_path = os.path.join(args.outdir, "command_judgements.json")
    with open(report_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dump_json(report))
    with open(verdict_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dump_json(judgements))

    print(f"[ok] 检测报告已生成: {report_path}（stage={args.stage}）")
    print(f"[ok] 判定文件已生成: {verdict_path}（{len(verdicts)} 条命令）")
    if load_result.missing_inputs:
        print(f"[warn] missing_inputs={load_result.missing_inputs}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
