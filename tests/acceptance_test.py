"""端到端验收测试：覆盖任务书 15 条验收点，仅使用标准库。

运行方式（项目根目录）：
    python tests/acceptance_test.py

脚本会自行启动/停止本地样本服务，所有产物落在 runs/acceptance/ 下便于回看。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vuln_scanner.cli import LocalSampleServer, main as cli_main  # noqa: E402
from vuln_scanner.config import (  # noqa: E402
    ConfigError, load_targets, load_policy, EndpointSpec, TargetSpec,
    DEFAULT_ENDPOINTS, ALL_CHECKS, CHECK_SQLI, CHECK_XSS, CHECK_TRAVERSAL,
)
from vuln_scanner.scanner import scan, write_report  # noqa: E402
from vuln_scanner.records import query_records  # noqa: E402
from vuln_scanner.report_compare import compare_reports  # noqa: E402
from vuln_scanner.patch import apply_patch, restore_backups  # noqa: E402

WORK = ROOT / "runs" / "acceptance"
SEV_RANK = {"high": 0, "medium": 1, "low": 2}

results: list[tuple[str, bool, str]] = []


def check(req: str, condition: bool, detail: str = "") -> None:
    results.append((req, bool(condition), detail))
    print(("  [PASS] " if condition else "  [FAIL] ") + req +
          ((" — " + detail) if detail else ""))


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def endpoints_json(endpoints):
    return [{"path": e.path, "checks": list(e.checks), "parameter": e.parameter,
             "placeholder": e.placeholder} for e in endpoints]


def main() -> int:
    import shutil
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    server = LocalSampleServer()
    server.start()
    base = "http://127.0.0.1:%d" % server.port
    try:
        _run_all(base, server)
    finally:
        server.stop()

    print("\n================ 验收汇总 ================")
    failed = [r for r in results if not r[1]]
    for req, ok, detail in results:
        print("%-6s %s %s" % ("PASS" if ok else "FAIL", req, detail))
    print("共 %d 项，失败 %d 项" % (len(results), len(failed)))
    return 1 if failed else 0


def _run_all(base: str, server: LocalSampleServer) -> None:
    # ----------------------------------------------- 需求 1：合法输入正常启动
    good_targets = WORK / "targets.json"
    write_json(good_targets, {"targets": [
        {"name": "sample", "base_url": base,
         "endpoints": endpoints_json(DEFAULT_ENDPOINTS)}]})
    good_policy = WORK / "policy.json"
    write_json(good_policy, {"timeout_seconds": 2.0,
                             "enabled_checks": list(ALL_CHECKS)})
    try:
        load_targets(str(good_targets))
        load_policy(str(good_policy))
        check("需求1", True, "合法目标清单与策略可加载，扫描正常启动")
    except ConfigError as exc:
        check("需求1", False, str(exc))

    # 缺少必需字段 -> 退出码 1 并说明缺失项
    miss_targets = WORK / "miss_targets.json"
    write_json(miss_targets, {"targets": [{"name": "x"}]})
    miss_policy = WORK / "miss_policy.json"
    write_json(miss_policy, {"timeout_seconds": 1.0})
    code_t = cli_main(["scan", "--targets", str(miss_targets),
                       "--policy", str(good_policy),
                       "--report", str(WORK / "x.json"),
                       "--records", str(WORK / "x.jsonl")])
    code_p = cli_main(["scan", "--targets", str(good_targets),
                       "--policy", str(miss_policy),
                       "--report", str(WORK / "x.json"),
                       "--records", str(WORK / "x.jsonl")])
    check("需求1-缺字段", code_t == 1 and code_p == 1,
          "缺失 base_url / enabled_checks 均以退出码 1 结束")

    # ----------------------------------------------- 需求 10：非法 URL 一次性列出
    bad_url = WORK / "bad_url.json"
    write_json(bad_url, {"targets": [
        {"name": "a", "base_url": "ftp://127.0.0.1/"},
        {"name": "b", "base_url": "127.0.0.1/no-scheme"},
        {"name": "c", "base_url": "javascript:alert(1)"}]})
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        code_bad = cli_main(["scan", "--targets", str(bad_url),
                             "--policy", str(good_policy),
                             "--report", str(WORK / "x.json"),
                             "--records", str(WORK / "x.jsonl")])
    listed = all(token in buf.getvalue()
                 for token in ("ftp://127.0.0.1/", "127.0.0.1/no-scheme",
                               "javascript:alert(1)"))
    check("需求10", code_bad == 1 and listed,
          "3 个非法 URL 全部列出且退出码为 1")

    # 目标上限 10
    over = WORK / "over.json"
    write_json(over, {"targets": [
        {"name": str(i), "base_url": base} for i in range(11)]})
    try:
        load_targets(str(over))
        check("需求1-上限", False, "11 个目标应被拒绝")
    except ConfigError:
        check("需求1-上限", True, "超过 10 个目标被拒绝")

    # ----------------------------------------------- 需求 4：不可达目标不污染结果
    mixed = WORK / "mixed.json"
    eps = endpoints_json(DEFAULT_ENDPOINTS)
    write_json(mixed, {"targets": [
        {"name": "normal", "base_url": base, "endpoints": eps},
        {"name": "dead", "base_url": "http://127.0.0.1:9",
         "endpoints": eps}]})
    policy1 = WORK / "policy1.json"
    write_json(policy1, {"timeout_seconds": 1.0,
                         "enabled_checks": list(ALL_CHECKS)})
    report4 = scan(load_targets(str(mixed)), load_policy(str(policy1)),
                   record_path=str(WORK / "r4.jsonl"))
    t_norm = next(t for t in report4["targets"] if t["name"] == "normal")
    t_dead = next(t for t in report4["targets"] if t["name"] == "dead")
    check("需求4",
          t_norm["scan_completed"] is True and len(t_norm["findings"]) == 6
          and t_dead["scan_completed"] is False
          and t_dead["scan_status"] in ("timeout", "unreachable")
          and len(t_dead["findings"]) == 0,
          "正常目标 6 条结果完整；不可达目标标注 %s 且无漏洞"
          % t_dead["scan_status"])

    findings = t_norm["findings"]

    # ----------------------------------------------- 需求 2：严重性降序
    sevs = [SEV_RANK[f["severity"]] for f in findings]
    check("需求2", sevs == sorted(sevs),
          "顺序: %s" % [f["severity"] for f in findings])

    # ----------------------------------------------- 需求 5：字段齐全/参数 null/建议非空
    field_ok = all({"type", "url", "parameter", "severity",
                    "remediation"} <= set(f) for f in findings)
    null_ok = any(f["parameter"] is None for f in findings)
    rem_ok = all(f["remediation"].strip() for f in findings)
    check("需求5", field_ok and null_ok and rem_ok,
          "字段齐全；路径型注入点 parameter=null；修复建议均非空")

    # ----------------------------------------------- 需求 3：报告结构与完成标记
    structure_ok = {"targets", "summary"} <= set(report4) and all(
        "scan_completed" in t for t in report4["targets"])
    check("需求3", structure_ok, "报告含 targets/summary，每目标标明是否完成")

    # ----------------------------------------------- 需求 11：摘要统计
    s = report4["summary"]
    sev_counts = [item["count"] for item in s["vulnerabilities_by_severity"]]
    sev_keys = [item["severity"] for item in s["vulnerabilities_by_severity"]]
    check("需求11",
          sev_keys == ["high", "medium", "low"] and sev_counts == [1, 3, 2]
          and s["completed_targets"] == 1 and s["incomplete_targets"] == 1,
          "高中低=%s，完成/未完成=%d/%d"
          % (sev_counts, s["completed_targets"], s["incomplete_targets"]))

    # ----------------------------------------------- 需求 8：500 警告保留且不影响其余
    warnings = t_norm["warnings"]
    warn_ok = any(w["message"].startswith("目标路径返回异常 HTTP 状态 500")
                  for w in warnings) and len(findings) == 6
    check("需求8", warn_ok, "/error 的 500 警告保留，其余路径 6 条结果照常存在")

    # ----------------------------------------------- 需求 7：策略排除项标注且无条目
    no_sqli = WORK / "policy_no_sqli.json"
    write_json(no_sqli, {"timeout_seconds": 2.0,
                         "enabled_checks": [CHECK_XSS, CHECK_TRAVERSAL]})
    report7 = scan(load_targets(str(good_targets)),
                   load_policy(str(no_sqli)),
                   record_path=str(WORK / "r7.jsonl"))
    t7 = report7["targets"][0]
    skip_ok = any(sc["check"] == CHECK_SQLI for sc in t7["skipped_checks"])
    no_sqli_finding = all(f["type"] != CHECK_SQLI for f in t7["findings"])
    check("需求7", skip_ok and no_sqli_finding,
          "SQL 注入被标注为未检测，且漏洞列表中无 SQL 注入条目")

    # ----------------------------------------------- 需求 12：JSONL 逐行可解析且字段一致
    lines = [json.loads(line) for line in
             (WORK / "r4.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip()]
    keys_ok = all({"target_url", "vulnerability_type", "parameter",
                   "severity"} <= set(row) for row in lines)
    report_pairs = {(f["type"], f["parameter"], f["severity"])
                    for f in findings}
    jsonl_pairs = {(row["vulnerability_type"], row["parameter"],
                    row["severity"]) for row in lines}
    check("需求12", keys_ok and jsonl_pairs == report_pairs,
          "JSONL 每行可解析，字段/条目与报告一致（%d 行）" % len(lines))

    # ----------------------------------------------- 需求 13：前缀+严重性查询
    only_high = query_records(str(WORK / "r4.jsonl"), severity="high")
    prefix_hit = query_records(str(WORK / "r4.jsonl"), url_prefix=base)
    prefix_miss = query_records(str(WORK / "r4.jsonl"),
                                url_prefix="http://127.0.0.1:9")
    order_ok = [SEV_RANK[r["severity"]] for r in prefix_hit] == sorted(
        SEV_RANK[r["severity"]] for r in prefix_hit)
    check("需求13",
          len(only_high) == 1 and only_high[0]["severity"] == "high"
          and len(prefix_hit) == 6 and prefix_miss == [] and order_ok,
          "严重性过滤 1 条 high；前缀命中 6 条、不匹配前缀 0 条；降序正确")

    # ----------------------------------------------- 需求 15：去重
    dup_endpoints = [
        EndpointSpec("/login", (CHECK_SQLI,), "username"),
        EndpointSpec("/login", (CHECK_SQLI,), "username"),  # 完全重复
        EndpointSpec("/search", (CHECK_XSS,), "q"),
        EndpointSpec("/search", (CHECK_XSS,), "lang"),      # 同 URL 不同参数
    ]
    dup_targets = WORK / "dup.json"
    write_json(dup_targets, {"targets": [
        {"name": "dup", "base_url": base,
         "endpoints": endpoints_json(dup_endpoints)}]})
    report15 = scan(load_targets(str(dup_targets)),
                    load_policy(str(good_policy)),
                    record_path=str(WORK / "r15.jsonl"))
    f15 = report15["targets"][0]["findings"]
    sqli_same = [f for f in f15 if f["type"] == CHECK_SQLI]
    xss_diff = [f for f in f15 if f["type"] == CHECK_XSS]
    check("需求15", len(sqli_same) == 1 and len(xss_diff) == 2,
          "同类型同参数重复命中保留 1 条；参数不同保留 2 条")

    # ----------------------------------------------- 需求 6：报告对比
    before_path = WORK / "before.json"
    after_path = WORK / "after.json"
    write_json(before_path, {"targets": [{"findings": [
        _fake_finding(CHECK_SQLI, base + "/login", "username", "high"),
        _fake_finding(CHECK_XSS, base + "/search", "q", "medium"),
        _fake_finding(CHECK_TRAVERSAL, base + "/file", "name", "low"),
    ]}]})
    write_json(after_path, {"targets": [{"findings": [
        # 中、低未变化；高已修复；新增一个中危
        _fake_finding(CHECK_XSS, base + "/search", "q", "medium"),
        _fake_finding(CHECK_TRAVERSAL, base + "/file", "name", "low"),
        _fake_finding(CHECK_XSS, base + "/comment", "text", "medium"),
    ]}]})
    cmp_result = compare_reports(str(before_path), str(after_path))
    order_fixed = [SEV_RANK[f["severity"]] for f in cmp_result["fixed"]]
    order_added = [SEV_RANK[f["severity"]] for f in cmp_result["added"]]
    check("需求6",
          cmp_result["counts"] == {"added": 1, "fixed": 1, "unchanged": 2}
          and order_fixed == sorted(order_fixed)
          and order_added == sorted(order_added),
          "新增 1/已修复 1/未变化 2，明细均按严重性降序")

    # ----------------------------------------------- 需求 9：中途取消
    slow_first = [EndpointSpec("/slow", (CHECK_TRAVERSAL,), "delay")] \
        + list(DEFAULT_ENDPOINTS)
    cancel_targets = WORK / "cancel.json"
    write_json(cancel_targets, {"targets": [
        {"name": "T1-fast", "base_url": base,
         "endpoints": endpoints_json(DEFAULT_ENDPOINTS)},
        {"name": "T2-slow", "base_url": base,
         "endpoints": endpoints_json(slow_first)},
        {"name": "T3-pending", "base_url": base, "endpoints": eps},
        {"name": "T4-pending", "base_url": base, "endpoints": eps},
    ]})
    cancel_policy = WORK / "policy_cancel.json"
    write_json(cancel_policy, {"timeout_seconds": 5.0,
                               "enabled_checks": list(ALL_CHECKS)})
    cancel_file = WORK / "CANCEL.signal"
    records9 = WORK / "r9.jsonl"

    def worker():
        report9 = scan(load_targets(str(cancel_targets)),
                       load_policy(str(cancel_policy)),
                       record_path=str(records9),
                       cancel_file=str(cancel_file))
        write_report(report9, str(WORK / "report9.json"))

    thread = threading.Thread(target=worker)
    thread.start()
    # T1 的 6 条命中全部落盘后（T2 正处于 /slow 3 秒窗口内）触发取消
    deadline = time.time() + 15
    while time.time() < deadline:
        if records9.exists() and len(records9.read_text(
                encoding="utf-8").splitlines()) >= 6:
            break
        time.sleep(0.05)
    cancel_file.write_text("cancel", encoding="utf-8")
    thread.join(timeout=30)
    report9 = json.loads((WORK / "report9.json").read_text(encoding="utf-8"))
    r9 = {t["name"]: t for t in report9["targets"]}
    check("需求9",
          report9["cancelled"] is True
          and r9["T1-fast"]["scan_completed"] is True
          and len(r9["T1-fast"]["findings"]) == 6
          and r9["T2-slow"]["scan_completed"] is False
          and r9["T3-pending"]["scan_status"] == "not_scanned"
          and r9["T4-pending"]["scan_status"] == "not_scanned",
          "T1 已完成结果保留；T2 标注取消；T3/T4 标注未扫描")

    # ----------------------------------------------- 需求 14：补丁修复验证
    # 释放端口，补丁后以独立进程重新启动加载修复后代码
    server.stop()
    from urllib.parse import urlparse
    port = urlparse(base).port
    patch_path = str(ROOT / "patches" / "fix_vulnerabilities.patch")
    single = WORK / "single.json"
    write_json(single, {"targets": [
        {"name": "sample", "base_url": base,
         "endpoints": endpoints_json(DEFAULT_ENDPOINTS)}]})
    apply_patch(patch_path, str(ROOT))
    try:
        patched_server = LocalSampleServer(port)
        patched_server.start()
        try:
            after = scan(load_targets(str(single)),
                         load_policy(str(good_policy)),
                         record_path=str(WORK / "r14_after.jsonl"))
        finally:
            patched_server.stop()
    finally:
        restore_backups(patch_path, str(ROOT))
    fixed_server = LocalSampleServer(port)
    fixed_server.start()
    try:
        restored = scan(load_targets(str(single)),
                        load_policy(str(good_policy)),
                        record_path=str(WORK / "r14_restored.jsonl"))
    finally:
        fixed_server.stop()
    check("需求14",
          len(after["targets"][0]["findings"]) == 0
          and len(restored["targets"][0]["findings"]) == 6,
          "补丁后复扫 0 条漏洞；恢复备份后复扫重新得到 6 条")


def _fake_finding(check: str, url: str, parameter, severity: str) -> dict:
    return {"type": check, "type_label": check, "url": url,
            "parameter": parameter, "severity": severity,
            "remediation": "test remediation"}


if __name__ == "__main__":
    raise SystemExit(main())
