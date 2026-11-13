"""Command line interface for the offline JSON configuration scanner."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .errors import SecscanError, VerificationError
from .jsonio import (
    atomic_write_text,
    dumps_document,
    load_config_document,
    validate_output_paths,
)
from .rules import RulesLoadResult, load_rules
from .scanner import (
    ScanResult,
    apply_fixes,
    build_report,
    scan_document,
)

PROGRAM = "secscan"
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INTERNAL = 1


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that keeps errors on stderr and exits with code 2."""

    def error(self, message: str) -> None:  # pragma: no cover - argparse path
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog=PROGRAM,
        description=(
            "离线 JSON 配置安全扫描器：检测弱口令、明文敏感值与危险反序列化设置，"
            "生成可复核 JSON 报告，并支持安全修复后重扫验证。"
        ),
        epilog=(
            "示例:\n"
            "  secscan --config samples\\vulnerable.json --output report.json\n"
            "  secscan --config samples\\vulnerable.json --output report.json "
            "--rules samples\\rules.json\n"
            "  secscan --config samples\\vulnerable.json --output report.json "
            "--fix --fixed-config samples\\vulnerable.fixed.json\n"
            "\n"
            "退出码: 0 扫描成功; 2 参数错误; 3 配置缺失/非法; 4 规则非法; "
            "5 输出失败; 6 修复后验证未通过。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="PATH",
        help="待扫描的 JSON 配置文件路径（必填）",
    )
    parser.add_argument(
        "--output",
        required=True,
        metavar="PATH",
        help="JSON 检测报告输出路径（必填）",
    )
    parser.add_argument(
        "--rules",
        metavar="PATH",
        default=None,
        help="可选的自定义 JSON 规则文件；缺失时使用内置默认规则并告警",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="修复可修复项（弱口令、明文敏感值），并对修复结果再次扫描验证",
    )
    parser.add_argument(
        "--fixed-config",
        metavar="PATH",
        default=None,
        help="修复后配置的写出路径（仅与 --fix 合用，默认同目录 *.fixed.json）",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _log(message: str, *, stream: Any = sys.stderr) -> None:
    print(message, file=stream)


def _print_human_summary(
    *,
    scan: ScanResult,
    report_path: str,
    fixed_path: str | None,
) -> None:
    """Print a short operator-facing summary to stdout."""

    high = sum(1 for f in scan.findings if f.rule.severity == "high")
    medium = sum(1 for f in scan.findings if f.rule.severity == "medium")
    low = sum(1 for f in scan.findings if f.rule.severity == "low")
    print(f"[INFO] 检测报告已写入: {report_path}")
    print(
        f"[INFO] 发现 {scan.total} 个问题 (高危 {high}, 中危 {medium}, 低危 {low}); "
        f"跳过 {len(scan.skipped)} 个路径待人工复核"
    )
    for finding in scan.sorted_findings():
        print(
            f"  - [{finding.rule.severity_label}] {finding.rule.rule_type} "
            f"{finding.path} ({finding.rule.rule_id})"
        )
    if fixed_path is not None:
        print(f"[INFO] 修复后的配置已写入: {fixed_path}")


def _fixed_config_path(args: argparse.Namespace) -> Path:
    if args.fixed_config:
        return Path(args.fixed_config)
    config = Path(args.config)
    return config.with_name(f"{config.stem}.fixed{config.suffix or '.json'}")


def run(args: argparse.Namespace) -> int:
    if args.fixed_config and not args.fix:
        _log("[ERROR] --fixed-config 只能与 --fix 一起使用 "
             "(--fixed-config requires --fix)")
        return EXIT_USAGE

    # 1) Load config first: missing/invalid input must not leave a report.
    document = load_config_document(args.config)

    # 2) Load rules (missing custom file => warning + defaults).
    rule_result: RulesLoadResult = load_rules(args.rules)
    for warning in rule_result.warnings:
        _log(warning)

    fixed_path = str(_fixed_config_path(args)) if args.fix else None
    validate_output_paths(args.output, args.config, fixed_path)

    # 3) Scan the original document.
    scan = scan_document(document, rule_result.rules)

    if not args.fix:
        report = build_report(
            config_path=str(args.config),
            rule_result=rule_result,
            scan=scan,
            mode="scan",
        )
        atomic_write_text(args.output, dumps_document(report))
        _print_human_summary(scan=scan, report_path=args.output, fixed_path=None)
        print(f"[OK] 扫描完成: {scan.total} 个问题，报告 {args.output}")
        return EXIT_OK

    # 4) Fix, then verify by re-scanning the fixed in-memory document.
    outcome = apply_fixes(document, scan.findings)
    post_scan = scan_document(outcome.document, rule_result.rules)
    remaining_fixable = [finding for finding in post_scan.findings if finding.fixable]

    report = build_report(
        config_path=str(args.config),
        rule_result=rule_result,
        scan=post_scan,
        mode="fix",
        fixes=outcome.fixes,
        fixed_config_path=fixed_path,
        pre_fix_total=scan.total,
        pre_fix_findings=scan.sorted_findings(),
    )

    # Write the fixed config first, then the report. Both writes are atomic.
    assert fixed_path is not None
    atomic_write_text(fixed_path, dumps_document(outcome.document))
    atomic_write_text(args.output, dumps_document(report))

    _print_human_summary(scan=post_scan, report_path=args.output, fixed_path=fixed_path)
    print(
        f"[OK] 修复完成: 替换 {len(outcome.fixes)} 处；复扫剩余 {post_scan.total} 个问题 "
        f"(其中可修复项 {len(remaining_fixable)} 个)"
    )
    if remaining_fixable:
        for finding in remaining_fixable:
            print(
                f"[ERROR] 修复后仍存在: {finding.path} ({finding.rule.rule_id})",
                file=sys.stderr,
            )
        raise VerificationError(
            "[ERROR] 修复后复扫仍发现可修复项 (post-fix verification failed)"
        )

    print("[OK] 复扫验证通过：弱口令与明文敏感值均已清除")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Avoid UnicodeEncodeError on legacy Windows consoles / redirected IO.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help/--version or usage error
        return int(exc.code or 0)

    try:
        return run(args)
    except SecscanError as exc:
        _log(str(exc))
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover
        _log("[ERROR] 已中断 (interrupted)")
        return 130
    except Exception as exc:  # pragma: no cover - last-resort guard
        _log(f"[ERROR] 未预期错误 (unexpected error): {type(exc).__name__}: {exc}")
        return EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
