"""统一命令行入口。

子命令：
  serve     启动本地漏洞样本服务
  lab       生成目标清单/策略/非法输入样例
  scan      执行扫描，输出 JSON 报告与 JSONL 明细
  compare   对比两份报告（新增/已修复/未变化）
  query     按 URL 前缀与严重性查询 JSONL 明细
  verify    应用补丁 -> 重新扫描 -> 输出修复前后对比（需求 14）
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

from .config import ConfigError, load_targets, load_policy
from .scanner import scan, write_report
from .report_compare import compare_reports
from .records import query_records
from .lab import generate as generate_lab
from .http_client import http_get, TargetUnreachable, RequestTimeout
from .patch import apply_patch, restore_backups

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATCH = str(PROJECT_ROOT / "patches" / "fix_vulnerabilities.patch")


# ------------------------------------------------------------- 样本服务管理
class LocalSampleServer:
    """以子进程方式管理样本服务，支持自动挑选空闲端口与就绪等待。"""

    def __init__(self, port: int | None = None):
        self.port = port or self._free_port()
        self.process: subprocess.Popen | None = None

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def start(self) -> None:
        env = dict(__import__("os").environ)
        env["SAMPLE_PORT"] = str(self.port)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "vuln_scanner.sample_app"],
            cwd=str(PROJECT_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8",
        )
        deadline = time.time() + 10
        health_url = "http://127.0.0.1:%d/health" % self.port
        while time.time() < deadline:
            if self.process.poll() is not None:
                output = self.process.stdout.read() if self.process.stdout else ""
                raise RuntimeError("样本服务启动失败:\n%s" % output)
            try:
                resp = http_get(health_url, timeout=1.0)
                if resp.status == 200:
                    return
            except (TargetUnreachable, RequestTimeout):
                time.sleep(0.15)
        raise RuntimeError("样本服务在 10 秒内未就绪")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


# ------------------------------------------------------------- 子命令实现
def cmd_serve(args) -> int:
    from .sample_app import app
    app.run(host="127.0.0.1", port=args.port)
    return 0


def cmd_lab(args) -> int:
    for path in generate_lab(args.lab_dir, args.port):
        print("generated:", path)
    return 0


def _run_scan(targets_path: str, policy_path: str, report_path: str,
              record_path: str, cancel_file: str | None) -> dict:
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(record_path).parent.mkdir(parents=True, exist_ok=True)
    targets = load_targets(targets_path)
    policy = load_policy(policy_path)
    report = scan(targets, policy, record_path=record_path,
                  cancel_file=cancel_file)
    write_report(report, report_path)
    return report


def cmd_scan(args) -> int:
    report = _run_scan(args.targets, args.policy, args.report,
                       args.records, args.cancel_file)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("\n报告已写入: %s" % args.report, file=sys.stderr)
    print("JSONL 明细已写入: %s" % args.records, file=sys.stderr)
    return 0


def cmd_compare(args) -> int:
    result = compare_reports(args.before, args.after)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.output:
        Path(args.output).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("对比结果已写入: %s" % args.output, file=sys.stderr)
    return 0


def cmd_query(args) -> int:
    try:
        records = query_records(args.records,
                                url_prefix=args.url_prefix,
                                severity=args.severity)
    except ValueError as exc:
        print("查询参数错误: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps({"count": len(records), "records": records},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_verify(args) -> int:
    """应用补丁前后各扫描一次并对比，结束后默认恢复样本文件（需求 14）。

    注意：Python 进程在启动时加载样本模块，打补丁后必须重启服务进程，
    否则运行中的服务仍执行旧代码。
    """
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    before_report = str(out_dir / "report_before.json")
    after_report = str(out_dir / "report_after.json")
    before_records = str(out_dir / "records_before.jsonl")
    after_records = str(out_dir / "records_after.jsonl")

    port = args.port or LocalSampleServer._free_port()

    # 1) 原始（含漏洞）服务：修复前扫描
    server = LocalSampleServer(port)
    server.start()
    try:
        lab_dir = out_dir / "lab"
        generate_lab(str(lab_dir), port)
        targets_path = str(lab_dir / "targets.json")
        policy_path = str(lab_dir / "policy.json")
        print("== 修复前扫描 ==", file=sys.stderr)
        _run_scan(targets_path, policy_path, before_report, before_records, None)
    finally:
        server.stop()

    # 2) 应用补丁（此时无服务进程占用模块）
    print("== 应用补丁: %s ==" % args.patch, file=sys.stderr)
    changed = apply_patch(args.patch, str(PROJECT_ROOT))
    for path in changed:
        print("patched:", path, file=sys.stderr)
    try:
        # 3) 重启服务，加载修复后代码，重新扫描
        patched_server = LocalSampleServer(port)
        patched_server.start()
        try:
            print("== 修复后重新扫描 ==", file=sys.stderr)
            _run_scan(targets_path, policy_path, after_report, after_records, None)
        finally:
            patched_server.stop()
    finally:
        if not args.keep_patched:
            restored = restore_backups(args.patch, str(PROJECT_ROOT))
            for path in restored:
                print("restored:", path, file=sys.stderr)

    result = compare_reports(before_report, after_report)
    diff_path = str(out_dir / "comparison.json")
    Path(diff_path).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n对比结果已写入: %s" % diff_path, file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vuln_scanner",
        description="本地授权 Web 漏洞扫描器（SQL 注入/XSS/目录遍历，仅标准库）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="启动漏洞样本服务")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.set_defaults(func=cmd_serve)

    p_lab = sub.add_parser("lab", help="生成实验室输入文件")
    p_lab.add_argument("--lab-dir", default="lab")
    p_lab.add_argument("--port", type=int, default=8765)
    p_lab.set_defaults(func=cmd_lab)

    p_scan = sub.add_parser("scan", help="执行扫描")
    p_scan.add_argument("--targets", required=True)
    p_scan.add_argument("--policy", required=True)
    p_scan.add_argument("--report", required=True)
    p_scan.add_argument("--records", required=True)
    p_scan.add_argument("--cancel-file", default=None,
                        help="该文件出现即触发取消（用于需求 9 演示）")
    p_scan.set_defaults(func=cmd_scan)

    p_cmp = sub.add_parser("compare", help="对比前后两份报告")
    p_cmp.add_argument("--before", required=True)
    p_cmp.add_argument("--after", required=True)
    p_cmp.add_argument("--output", default=None)
    p_cmp.set_defaults(func=cmd_compare)

    p_query = sub.add_parser("query", help="查询 JSONL 漏洞明细")
    p_query.add_argument("--records", required=True)
    p_query.add_argument("--url-prefix", default=None)
    p_query.add_argument("--severity", default=None,
                         choices=["high", "medium", "low"])
    p_query.set_defaults(func=cmd_query)

    p_verify = sub.add_parser("verify", help="应用补丁后复扫并对比")
    p_verify.add_argument("--patch", default=DEFAULT_PATCH)
    p_verify.add_argument("--output-dir", default="runs/verify")
    p_verify.add_argument("--port", type=int, default=None)
    p_verify.add_argument("--keep-patched", action="store_true",
                          help="保留补丁后的样本文件（默认自动恢复）")
    p_verify.set_defaults(func=cmd_verify)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print("输入校验失败，扫描未启动：", file=sys.stderr)
        for problem in exc.problems:
            print("  - %s" % problem, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
