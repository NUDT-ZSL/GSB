#!/usr/bin/env python3
"""End-to-end demo: build a throw-away workspace, run every acceptance case.

The script prints a step-by-step log with the real command, the real exit
code and the real report excerpt, so the output can be pasted into a review
as evidence.  Nothing is written outside a temporary directory, except
``demo_report.json`` when ``--keep`` is used.

Usage::

    python examples/run_demo.py
    python examples/run_demo.py --keep demo_report.json

Platform note: the unreadable-file case needs ACL support (Windows) or
``chmod 000`` (POSIX non-root).  Where the platform cannot enforce a read
denial the case is reported as SKIPPED rather than faked.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNER = REPO_ROOT / "scan_md.py"
SAMPLES_DIR = REPO_ROOT / "examples" / "samples"
CONFIG = REPO_ROOT / "rules.json"


def run(args: list[str], *, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SCANNER), *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def show(result: subprocess.CompletedProcess[str], report: Path | None = None) -> None:
    print(f"$ {' '.join(str(a) for a in result.args[2:])}")
    print(f"exit code: {result.returncode}")
    if result.stdout.strip():
        print(result.stdout.rstrip())
    if result.stderr.strip():
        print("stderr: " + result.stderr.strip())
    if report and report.exists():
        rows = json.loads(report.read_text(encoding="utf-8"))
        print(f"report ({len(rows)} entries):")
        for row in rows:
            print("  " + json.dumps(row, ensure_ascii=False))
    elif report:
        print(f"report {report.name}: NOT CREATED")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", metavar="REPORT",
                        help="copy the main scan report to this path before exiting")
    args = parser.parse_args()

    tmp_root = Path(tempfile.mkdtemp(prefix="mdscan-demo-"))
    print(f"demo workspace: {tmp_root}")
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    try:
        # ---- workspace: committed samples + one permission-locked copy ------
        corpus = tmp_root / "corpus"
        shutil.copytree(SAMPLES_DIR, corpus)
        locked = corpus / "unreadable.md"
        locked.write_text("# 无权限样本\n\n用于验证读取失败不影响其他文件。\n",
                          encoding="utf-8", newline="\n")
        lock_ok = False
        if os.name == "nt":
            user = os.environ.get("USERNAME", "")
            proc = subprocess.run(["icacls", str(locked), "/deny", f"{user}:(R)"],
                                  capture_output=True, text=True)
            lock_ok = proc.returncode == 0
        else:
            os.chmod(locked, 0o000)
            lock_ok = os.geteuid() != 0
        if lock_ok:
            try:
                locked.read_bytes()
                lock_ok = False  # the denial did not actually take effect
            except PermissionError:
                pass

        report = tmp_root / "report.json"

        banner("A. 主扫描：--dir / --config / --output（含不可读文件）")
        result = run(["--dir", str(corpus), "--config", str(CONFIG), "--output", str(report)])
        show(result, report)
        if result.returncode == 0:
            rows = json.loads(report.read_text(encoding="utf-8"))
            names = [row["file"] for row in rows]
            check("1  仅 .md 参与扫描（notes.txt 不在报告）", "notes.txt" not in names)
            check("7  B.md 排在 a.md 之前", names.index("B.md") < names.index("a.md"))
            check("3/4 大小写与正则规则命中",
                  any(r["file"] == "case-insensitive.md" and r["matched_rules"] for r in rows),
                  "case-insensitive.md")
            check("5  注释/零宽字符/Tag 夹带均命中",
                  all(next(r for r in rows if r["file"] == n)["suspicious"]
                      for n in ("hidden-comment.md", "zero-width.md", "unicode-tags.md")))
            check("6  正常文件 error 为 null 且 matched_rules 为空",
                  next(r for r in rows if r["file"] == "a.md")["error"] is None
                  and next(r for r in rows if r["file"] == "a.md")["matched_rules"] == [])
            if lock_ok:
                locked_row = next(r for r in rows if r["file"] == "unreadable.md")
                check("10 不可读文件记录 error 且其余文件完成",
                      bool(locked_row["error"])
                      and any(r["file"] == "a.md" and r["error"] is None for r in rows))
            else:
                check("10 不可读文件（平台无法强制拒绝读取）", True, "SKIPPED")
            summary = f"{len(rows)} files, " \
                      f"{sum(r['suspicious'] for r in rows)} suspicious"
            stdout_ok = f"scanned {len(rows)} markdown file(s)" in result.stdout
            check("11 控制台摘要与报告一致", stdout_ok, summary)
        else:
            check("A 主扫描", False, result.stderr.strip())

        # ---- 8: bad config ------------------------------------------------
        banner("B. 配置缺失（要求 8）")
        bad_config = tmp_root / "missing.json"
        result = run(["--dir", str(corpus), "--config", str(bad_config),
                      "--output", str(tmp_root / "r1.json")])
        show(result)
        check("8a 配置缺失 -> 非零退出",
              result.returncode != 0 and not (tmp_root / "r1.json").exists(),
              f"exit {result.returncode}")

        invalid = tmp_root / "invalid.json"
        invalid.write_text('{"rules": [', encoding="utf-8")
        result = run(["--dir", str(corpus), "--config", str(invalid),
                      "--output", str(tmp_root / "r2.json")])
        show(result)
        check("8b JSON 非法 -> 非零退出",
              result.returncode != 0 and not (tmp_root / "r2.json").exists(),
              f"exit {result.returncode}")

        # ---- 9: missing directory ------------------------------------------
        banner("C. 目标目录不存在（要求 9）")
        result = run(["--dir", str(tmp_root / "does-not-exist"), "--config", str(CONFIG),
                      "--output", str(tmp_root / "r3.json")])
        show(result)
        check("9  目录不存在 -> 非零退出且不生成报告",
              result.returncode != 0 and not (tmp_root / "r3.json").exists(),
              f"exit {result.returncode}")

        # ---- 1: extension filter in isolation ------------------------------
        banner("D. 混合扩展名（要求 1）")
        mixed = tmp_root / "mixed"
        mixed.mkdir()
        (mixed / "a.md").write_text("普通文档\n", encoding="utf-8")
        (mixed / "b.txt").write_text("eval(x)\n", encoding="utf-8")
        result = run(["--dir", str(mixed), "--config", str(CONFIG),
                      "--output", str(tmp_root / "r4.json")])
        show(result, tmp_root / "r4.json")
        rows = json.loads((tmp_root / "r4.json").read_text(encoding="utf-8")) if result.returncode == 0 else []
        check("1  a.md 收录、b.txt 不收录", [r["file"] for r in rows] == ["a.md"])

        # ---- restitution ---------------------------------------------------
        if lock_ok and os.name == "nt":
            subprocess.run(["icacls", str(locked), "/remove:d", os.environ.get("USERNAME", "")],
                           capture_output=True, text=True)
        if lock_ok and os.name != "nt":
            os.chmod(locked, 0o644)
    finally:
        pass

    banner("验证汇总")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    failed = [name for name, ok, _ in checks if not ok]
    print()
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed")

    if args.keep and report.exists():
        shutil.copyfile(report, args.keep)
        print(f"main report copied to {args.keep}")

    if not args.keep:
        shutil.rmtree(tmp_root, ignore_errors=True)
        print(f"demo workspace removed: {tmp_root}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
