"""命令行入口。

用法：
  python -m token_guard.cli analyze --token "<令牌>" --key "<密钥>" [--file out.json]
  python -m token_guard.cli verify  --token "<令牌>" --key "<密钥>"
  python -m token_guard.cli demo                # 用确定性样本跑完整演示
  echo <令牌> | python -m token_guard.cli analyze --key "<密钥>"   # 管道输入

退出码：0 校验通过；1 校验不通过或检测到风险；2 输入级错误（超长/不支持/损坏）。
"""

from __future__ import annotations

import argparse
import json
import sys

from . import MAX_TOKEN_LENGTH, __version__
from .analyzer import analyze_token
from .core import TokenGuardError
from .samples import DEMO_KEY, build_samples
from .verifier import hardened_verify

# Windows 控制台默认 GBK，统一强制 UTF-8 输出，保证中文报告不崩。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def _error_payload(exc: TokenGuardError) -> dict:
    return {
        "error": True,
        "code": exc.code,
        "message": str(exc),
        "max_token_length": MAX_TOKEN_LENGTH,
    }


def _read_token(args: argparse.Namespace) -> str:
    if args.token is not None:
        return args.token
    if args.token_file is not None:
        # 令牌文件可能来自外部：按数据处理，去除首尾空白，不执行其中任何内容。
        with open(args.token_file, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    raise SystemExit("错误：必须通过 --token、--token-file 或标准输入提供令牌")


def cmd_analyze(args: argparse.Namespace) -> int:
    token = _read_token(args)
    try:
        report = analyze_token(token, args.key)
    except TokenGuardError as exc:
        print(json.dumps(_error_payload(exc), ensure_ascii=False, indent=2))
        return 2

    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.file:
        with open(args.file, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0 if report["conclusion"] == "pass" else 1


def cmd_verify(args: argparse.Namespace) -> int:
    token = _read_token(args)
    try:
        result = hardened_verify(token, args.key)
    except TokenGuardError as exc:
        print(json.dumps(_error_payload(exc), ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


def cmd_demo(_args: argparse.Namespace) -> int:
    samples = build_samples()
    # 输入级错误样本单独演示错误报告，其余走完整分析。
    error_samples = ("empty_string", "garbage", "four_segments", "too_long")
    any_fail = False

    for name, token in samples.items():
        print("=" * 78)
        print(f"样本 {name}（长度 {len(token)} 字符）")
        if len(token) <= 120:
            print(f"令牌: {token!r}")
        else:
            print(f"令牌: {token[:80]!r}...（截断显示，实际 {len(token)} 字符）")

        if name in error_samples:
            try:
                analyze_token(token, DEMO_KEY)
            except TokenGuardError as exc:
                payload = _error_payload(exc)
                print("检测报告:")
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            continue

        report = analyze_token(token, DEMO_KEY)
        print("检测报告:")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report["conclusion"] != "pass":
            any_fail = True

    print("=" * 78)
    print("演示结束：所有篡改/过期/异常样本均应 conclusion=fail 或返回输入错误，"
          "合法样本 conclusion=pass。")
    return 1 if any_fail else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="token_guard",
        description="本地令牌（JWT / 自定义 payload.signature）检测与加固验证工具",
    )
    parser.add_argument("--version", action="version", version=f"token_guard {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--token", help="待检测令牌字符串")
        sp.add_argument("--token-file", help="从文件读取令牌（路径方式，不执行文件内容）")
        sp.add_argument("--key", default=DEMO_KEY, help=f"共享密钥（默认 {DEMO_KEY!r}）")

    sp_analyze = sub.add_parser("analyze", help="输出完整 JSON 检测报告（风险列表+校验结论）")
    add_common(sp_analyze)
    sp_analyze.add_argument("--file", help="同时把 JSON 报告写入指定文件")
    sp_analyze.set_defaults(func=cmd_analyze)

    sp_verify = sub.add_parser("verify", help="仅运行加固后的校验函数")
    add_common(sp_verify)
    sp_verify.set_defaults(func=cmd_verify)

    sp_demo = sub.add_parser("demo", help="使用确定性样本跑完整演示")
    sp_demo.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
