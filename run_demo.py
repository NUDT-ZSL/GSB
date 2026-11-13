"""端到端可复现演示脚本（仅标准库）。

自动完成全部验收场景：
  0. 生成确定性样本（基目录、基目录外标记、测试路径、损坏 JSON）
  1. 启动 vulnerable / fixed 两个本地模拟服务（线程内运行，固定端口）
  2. 命令行参数来源扫描 vulnerable 服务 -> 高风险穿越路径
  3. JSON 文件来源扫描 fixed 服务 -> 越界路径低风险，含状态码与原因
  4. 原始查询串日志复核：百分号编码与输入逐字一致
  5. 中途关停服务（drop-after）-> 剩余路径 unknown，已完成判定不受影响
  6. 损坏 / 缺失 JSON -> 可读错误、非零退出、不产生报告
  7. 排序与计数一致性校验（高->低->未知，同层字母序，明细与汇总相等）
  8. 默认 report.json 与指定路径输出两次，JSON 均可解析且字节一致；
     并对同一目标重复扫描，验证报告顺序与内容稳定可复现

用法：  python -I run_demo.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

# Windows 中文控制台默认 GBK，统一强制 UTF-8 输出，避免报告与错误信息乱码。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathscan import generate_samples, mock_server  # noqa: E402
from pathscan.scanner import main as scanner_main  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.join(ROOT, "workspace")
DEMO_OUT = os.path.join(WORKSPACE, "demo_out")
PY = sys.executable


def section(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def run_cli(*cli_args: str, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    """以真实子进程方式运行扫描器 CLI。"""
    # 子进程可能在其他工作目录运行（如默认输出验证），需显式指定包搜索路径；
    # -I 模式下 PYTHONPATH 仍然生效，且不会加载 cwd 中可疑的同名模块。
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               PYTHONPATH=ROOT)
    proc = subprocess.run(
        [PY, "-m", "pathscan.scanner", *cli_args],
        cwd=cwd or ROOT, capture_output=True, text=True,
        encoding="utf-8", env=env,
    )  # 注意：不加 -I，以便 PYTHONPATH 在非项目目录下仍可定位 pathscan 包
    if proc.stdout.strip():
        print("  " + proc.stdout.strip().replace("\n", "\n  "))
    if proc.stderr.strip():
        print("  [stderr] " + proc.stderr.strip())
    return proc


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def assert_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label} 校验失败: 期望 {expected!r}，实际 {actual!r}")
    print(f"  [OK] {label}: {actual!r}")


def start_server(port: int, base_dir: str, mode: str,
                 log_path: str | None = None,
                 drop_after: int | None = None) -> tuple[object, threading.Thread]:
    httpd = mock_server.make_server(
        "127.0.0.1", port, base_dir, mode,
        log_path=log_path, drop_after=drop_after,
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


def main() -> int:
    os.makedirs(DEMO_OUT, exist_ok=True)

    # ------------------------------------------------------------------ #
    section("步骤 0：生成确定性样本")
    artifacts = generate_samples.generate(WORKSPACE)
    base_dir = artifacts["base_dir"]
    marker_path = artifacts["marker_path"]
    samples_path = artifacts["samples_path"]
    corrupt_path = artifacts["corrupt_path"]
    sample_paths = generate_samples.SAMPLE_PATHS
    for key, value in artifacts.items():
        print(f"  {key}: {value}")

    log_vuln = os.path.join(DEMO_OUT, "raw_query_vuln.log")
    log_fixed = os.path.join(DEMO_OUT, "raw_query_fixed.log")
    open(log_vuln, "w", encoding="utf-8").close()
    open(log_fixed, "w", encoding="utf-8").close()

    # ------------------------------------------------------------------ #
    section("步骤 1：启动 vulnerable(8081) 与 fixed(8082) 模拟服务")
    vuln, _ = start_server(8081, base_dir, "vulnerable", log_path=log_vuln)
    fixed, _ = start_server(8082, base_dir, "fixed", log_path=log_fixed)
    print("  vulnerable -> http://127.0.0.1:8081/download")
    print("  fixed      -> http://127.0.0.1:8082/download")

    cli_paths: list[str] = []
    for p in sample_paths:
        cli_paths += ["-p", p]

    try:
        # -------------------------------------------------------------- #
        section("步骤 2：命令行参数来源，扫描 vulnerable 服务（验收 1、3）")
        report_vuln_path = os.path.join(DEMO_OUT, "report_vuln_cli.json")
        proc = run_cli(
            "-u", "http://127.0.0.1:8081", "-e", "/download",
            *cli_paths, "-m", marker_path, "-o", report_vuln_path,
        )
        assert_equal(proc.returncode, 0, "退出码")
        report_vuln = load_json(report_vuln_path)
        high_v = [d for d in report_vuln["details"] if d["verdict"] == "high"]
        print(f"  高风险路径: {[d['test_path'] for d in high_v]}")
        for rec in high_v:
            assert_equal(rec["evidence"]["type"], "marker_content_match",
                         f"证据类型({rec['test_path']})")
            assert_equal(rec["evidence"]["marker_file_name"], "SECRET_MARKER.txt",
                         f"证据含命中标记文件名({rec['test_path']})")
        assert len(high_v) >= 3, "穿越路径应至少 3 条命中高风险"
        assert all(d["verdict"] != "unknown" for d in report_vuln["details"])

        # -------------------------------------------------------------- #
        section("步骤 3：JSON 文件来源，扫描 fixed 服务（验收 1、4）")
        report_fixed_path = os.path.join(DEMO_OUT, "report_fixed_json.json")
        proc = run_cli(
            "-u", "http://127.0.0.1:8082",
            "-f", samples_path,
            "-m", marker_path, "-o", report_fixed_path,
        )
        assert_equal(proc.returncode, 0, "退出码")
        report_fixed = load_json(report_fixed_path)
        traversals = [
            d for d in report_fixed["details"]
            if "SECRET_MARKER" in d["test_path"]
        ]
        # 单次解码即可构成穿越的路径：必须被 403 明确拒绝。
        single_decode = [
            d for d in traversals if "%252f" not in d["test_path"]
        ]
        # 双重编码路径（%252f）：服务端只解码一次，不构成穿越，落到 404，
        # 同样是低风险 —— 它验证了“未越界即不暴露标记内容”。
        double_encoded = [
            d for d in traversals if "%252f" in d["test_path"]
        ]
        for rec in single_decode:
            assert_equal(rec["verdict"], "low", f"越界判定({rec['test_path']})")
            assert_equal(rec["http_status"], 403, f"状态码({rec['test_path']})")
            assert "outside base directory" in rec["reason"], (
                f"原因文本缺失({rec['test_path']}): {rec['reason']}")
            print(f"  [OK] {rec['test_path']} -> 403 / {rec['reason']}")
        for rec in double_encoded:
            assert_equal(rec["verdict"], "low", f"双重编码判定({rec['test_path']})")
            assert_equal(rec["http_status"], 404, f"状态码({rec['test_path']})")
            print(f"  [OK] {rec['test_path']} -> 404（未二次解码，不构成穿越，无标记泄露）")
        assert_equal(report_fixed["summary"]["high"], 0, "修复后高风险数")

        # -------------------------------------------------------------- #
        section("步骤 4：原始查询串复核（验收 2：百分号编码逐字一致）")
        for log_path, label in ((log_vuln, "vuln"), (log_fixed, "fixed")):
            with open(log_path, "r", encoding="utf-8") as fh:
                logged = [line.rstrip("\n").split("\t")[-1] for line in fh]
            for p in sample_paths:
                needle = f"filename={p}"
                assert_equal(
                    any(needle in entry for entry in logged), True,
                    f"[{label}] 日志逐字包含 {needle!r}")
        print("  [OK] 百分号编码路径在服务端日志中与输入逐字一致，无二次编码")

        # -------------------------------------------------------------- #
        section("步骤 7（前置）：排序与计数一致性（验收 7、8）")
        summary = report_vuln["summary"]
        details = report_vuln["details"]
        counts = {"high": 0, "low": 0, "unknown": 0}
        for rec in details:
            counts[rec["verdict"]] += 1
        assert_equal(counts["high"], summary["high"], "high 明细数=汇总数")
        assert_equal(counts["low"], summary["low"], "low 明细数=汇总数")
        assert_equal(counts["unknown"], summary["unknown"], "unknown 明细数=汇总数")
        assert_equal(summary["total"], len(details), "total=明细总数")

        ranks = [{"high": 0, "low": 1, "unknown": 2}[d["verdict"]] for d in details]
        assert ranks == sorted(ranks), "层级顺序错误"
        for verdict in ("high", "low", "unknown"):
            group = [d["test_path"] for d in details if d["verdict"] == verdict]
            assert group == sorted(group), f"{verdict} 组内字母序错误"
        print("  [OK] 明细按 高风险->低风险->未知 排列，同层按测试路径字母序")
    finally:
        vuln.shutdown()
        vuln.server_close()
        fixed.shutdown()
        fixed.server_close()

    # ------------------------------------------------------------------ #
    section("步骤 5：中途关停服务 -> unknown，其余判定不受影响（验收 5）")
    # drop-after=2：第 1 个请求完整响应（闸门在其处理期间放下），
    # 第 2 个起的新连接被服务端立即关闭，扫描器判定 unknown。
    drop, _ = start_server(8091, base_dir, "fixed", drop_after=2)
    drop_paths = [
        "docs/readme.txt",
        "docs/notes.txt",
        "../outside/SECRET_MARKER.txt",
        "..%2foutside%2fSECRET_MARKER.txt",
        "missing/no_such_file.txt",
    ]
    cli_drop: list[str] = []
    for p in drop_paths:
        cli_drop += ["-p", p]
    report_drop_path = os.path.join(DEMO_OUT, "report_drop.json")
    # 扫描器按顺序探测：第 1 条完整响应；其处理期间连接闸门已放下，
    # 第 2 条起的新连接被服务端立即关闭 -> unknown。
    proc = run_cli(
        "-u", "http://127.0.0.1:8091",
        *cli_drop, "-m", marker_path,
        "-o", report_drop_path, "-t", "3",
    )
    # 等待后台 shutdown 线程结束，再回收服务对象。
    import time as _time
    _time.sleep(0.3)
    drop.shutdown()
    drop.server_close()
    assert_equal(proc.returncode, 0, "退出码（含 unknown 仍正常出报告）")
    report_drop = load_json(report_drop_path)
    verdicts = [d["verdict"] for d in report_drop["details"]]
    print(f"  各路径判定: {list(zip(drop_paths, verdicts))}")
    by_path = {d["test_path"]: d for d in report_drop["details"]}
    # 第一条响应在闸门放下前已完整返回，判定必须保留。
    assert_equal(by_path["docs/readme.txt"]["verdict"], "low",
                 "已完成路径 readme 判定")
    assert_equal(by_path["docs/readme.txt"]["http_status"], 200,
                 "已完成路径 readme 状态码")
    # 闸门之后的所有路径必须是 unknown，且数量占大多数。
    assert verdicts.count("unknown") >= 3, "关停后的剩余路径应为 unknown"
    assert "high" not in verdicts, "服务关停后不得凭空产生高风险"
    for rec in report_drop["details"]:
        if rec["verdict"] == "unknown":
            assert rec["error"], "unknown 明细必须记录网络错误原因"
            assert rec["http_status"] is None
    print("  [OK] 已完成路径判定保留，失败路径为 unknown 且记录错误原因，未并入安全")

    # ------------------------------------------------------------------ #
    section("步骤 6：损坏 / 缺失 JSON 的错误处理（验收 6）")
    no_report = os.path.join(DEMO_OUT, "must_not_exist.json")
    if os.path.exists(no_report):
        os.unlink(no_report)
    proc = run_cli(
        "-u", "http://127.0.0.1:8082", "-f", corrupt_path,
        "-m", marker_path, "-o", no_report,
    )
    assert proc.returncode != 0, "损坏 JSON 必须非零退出"
    assert not os.path.exists(no_report), "损坏 JSON 时不得生成报告"
    print(f"  [OK] 损坏 JSON: 退出码 {proc.returncode}，无报告产生")

    proc = run_cli(
        "-u", "http://127.0.0.1:8082",
        "-f", os.path.join(DEMO_OUT, "missing_xxx.json"),
        "-m", marker_path, "-o", no_report,
    )
    assert proc.returncode != 0 and not os.path.exists(no_report)
    print(f"  [OK] 缺失 JSON: 退出码 {proc.returncode}，无报告产生")

    # 进程内再验证一次：无任何路径来源时也非零退出、不出报告。
    rc = scanner_main(["-u", "http://127.0.0.1:8082", "-m", marker_path,
                       "-o", no_report])
    assert rc != 0 and not os.path.exists(no_report)
    print(f"  [OK] 未提供路径: 退出码 {rc}，无报告产生")

    # ------------------------------------------------------------------ #
    section("步骤 8：默认输出、指定路径与重复运行一致性（验收 8、9）")
    vuln2, _ = start_server(8081, base_dir, "vulnerable")
    default_dir = os.path.join(DEMO_OUT, "default_cwd")
    os.makedirs(default_dir, exist_ok=True)
    custom_path = os.path.join(DEMO_OUT, "custom_report.json")
    default_path = os.path.join(default_dir, "report.json")
    repeat_path = os.path.join(DEMO_OUT, "report_vuln_repeat.json")
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                   PYTHONPATH=ROOT)
        proc = subprocess.run(
            [PY, "-m", "pathscan.scanner",
             "-u", "http://127.0.0.1:8081", *cli_paths, "-m", marker_path],
            cwd=default_dir, capture_output=True, text=True,
            encoding="utf-8", env=env)
        assert_equal(proc.returncode, 0, "默认输出退出码")
        proc = run_cli(
            "-u", "http://127.0.0.1:8081", *cli_paths,
            "-m", marker_path, "-o", custom_path)
        assert_equal(proc.returncode, 0, "指定路径退出码")
        proc = run_cli(
            "-u", "http://127.0.0.1:8081", *cli_paths,
            "-m", marker_path, "-o", repeat_path)
        assert_equal(proc.returncode, 0, "重复运行退出码")
    finally:
        vuln2.shutdown()
        vuln2.server_close()

    b_default = open(default_path, "rb").read()
    b_custom = open(custom_path, "rb").read()
    b_repeat = open(repeat_path, "rb").read()
    b_first = open(report_vuln_path, "rb").read()
    json.loads(b_default.decode("utf-8"))   # 合法 UTF-8 JSON
    json.loads(b_custom.decode("utf-8"))
    assert b_default == b_custom, "默认与指定路径输出内容不一致"
    assert b_repeat == b_first, "重复运行报告不一致（顺序/内容应稳定）"
    print("  [OK] 默认 report.json 与指定路径输出均可被 json 解析且字节一致")
    print("  [OK] 对同一目标重复运行，报告字节级一致（顺序稳定可复核）")

    section("全部验收场景通过 ✔")
    print(f"演示产物目录: {DEMO_OUT}")
    print(json.dumps({
        "vuln_summary": report_vuln["summary"],
        "fixed_summary": report_fixed["summary"],
        "drop_summary": report_drop["summary"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
