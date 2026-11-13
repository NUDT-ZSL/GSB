"""一键演示编排：生成样本 -> 启动两个本地服务 -> 验收检查 -> 检测 -> 对比报告。

仅使用 Python 标准库，只访问 127.0.0.1，不访问任何外部网络。
用法: python run_all.py
"""
import json
import os
import threading

from common import (
    BASE, REPORTS_DIR, SECRET_MARKER, TEXT_SAMPLES,
    generate_samples, outside_relative_name,
)
from detector import detect, fetch, write_report
from hardened_server import make_server as make_hardened
from vulnerable_server import make_server as make_vulnerable

VULN_BASE = "http://127.0.0.1:18081"
HARD_BASE = "http://127.0.0.1:18082"

results = []  # (编号, 描述, 是否通过, 说明)


def check(no, desc, ok, detail=""):
    results.append((no, desc, ok, detail))
    print("[%s] 验收%d %s %s" % ("PASS" if ok else "FAIL", no, desc, ("- " + detail) if detail else ""))


def main():
    print("== 1. 生成确定性样本 ==")
    info = generate_samples()
    print("uploads:", ", ".join(info["uploads"]))
    print("目录外样本:", info["outside_file"])

    print("\n== 2. 启动本地服务（未加固 :18081 / 加固 :18082）==")
    vuln_srv = make_vulnerable(18081)
    hard_srv = make_hardened(18082)
    for srv in (vuln_srv, hard_srv):
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    try:
        traversal = outside_relative_name()

        # 验收1：已有文件 200 且正文与样本一致
        st, body = fetch(VULN_BASE, "hello.txt")
        check(1, "已有文件返回200且正文一致",
              st == 200 and body == TEXT_SAMPLES["hello.txt"], "status=%d" % st)

        # 验收2：不存在的文档名返回 404 且说明未找到
        st, body = fetch(VULN_BASE, "no_such_doc.txt")
        check(2, "不存在文档名返回404并说明未找到",
              st == 404 and "not found" in body.lower(), "status=%d body=%r" % (st, body.strip()))

        # 验收3：未加固版本可读到目录外样本
        st, body = fetch(VULN_BASE, traversal)
        check(3, "未加固服务可越权读到目录外样本",
              st == 200 and SECRET_MARKER in body, "status=%d" % st)

        # 验收4：加固版本拒绝穿越且不回传目录外内容
        st, body = fetch(HARD_BASE, traversal)
        check(4, "加固服务拒绝穿越(4xx)且不回传目录外内容",
              400 <= st < 500 and SECRET_MARKER not in body, "status=%d" % st)

        # 验收5：拒绝响应不泄露目录外内容与本机绝对路径
        leaked_path = BASE in body or BASE.replace("\\", "/") in body
        check(5, "拒绝响应不泄露目录外内容与绝对路径",
              SECRET_MARKER not in body and not leaked_path, "body=%r" % body.strip())

        # 验收7：空名与超长名返回 4xx 且服务不崩溃
        st_empty, _ = fetch(HARD_BASE, "")
        st_long, _ = fetch(HARD_BASE, "a" * 5000)
        st_after, body_after = fetch(HARD_BASE, "hello.txt")
        check(7, "空名/超长名返回4xx且随后正常请求仍可用",
              400 <= st_empty < 500 and 400 <= st_long < 500
              and st_after == 200 and body_after == TEXT_SAMPLES["hello.txt"],
              "empty=%d long=%d after=%d" % (st_empty, st_long, st_after))

        # 验收8：非 UTF-8 文件返回明确错误，服务保持运行
        for label, base in (("未加固", VULN_BASE), ("加固", HARD_BASE)):
            st, body = fetch(base, "binary.bin")
            st2, _ = fetch(base, "hello.txt")
            check(8, "%s服务: 二进制样本返回明确错误且进程保持运行" % label,
                  400 <= st < 500 and "UTF-8" in body and st2 == 200,
                  "status=%d body=%r" % (st, body.strip()))

        print("\n== 3. 运行检测入口并写报告 ==")
        vuln_report = detect(VULN_BASE)
        hard_report = detect(HARD_BASE)
        vuln_path = os.path.join(REPORTS_DIR, "vulnerable_report.json")
        hard_path = os.path.join(REPORTS_DIR, "hardened_report.json")
        write_report(vuln_report, vuln_path)
        write_report(hard_report, hard_path)
        print("未加固检测报告 ->", vuln_path, "结论:", vuln_report["conclusion"])
        print("加固检测报告   ->", hard_path, "结论:", hard_report["conclusion"])

        # 验收6：两次检测结论可区分
        check(6, "检测结论: 未加固=VULNERABLE, 加固=NOT_VULNERABLE",
              vuln_report["conclusion"] == "VULNERABLE"
              and hard_report["conclusion"] == "NOT_VULNERABLE")

        # 验收9：报告可解析且含固定字段 target/conclusion/evidence
        ok9 = True
        for p in (vuln_path, hard_path):
            with open(p, encoding="utf-8") as f:
                r = json.load(f)
            if list(r.keys()) != ["target", "conclusion", "evidence"]:
                ok9 = False
            for ev in r["evidence"]:
                if list(ev.keys()) != ["check", "request", "status", "finding"]:
                    ok9 = False
        check(9, "报告JSON可解析且含固定字段(target/conclusion/evidence)", ok9)

        # 验收10：对比报告，未加固在前、加固在后，字段顺序一致
        comparison = {
            "comparison": [
                {"target": vuln_report["target"],
                 "conclusion": vuln_report["conclusion"],
                 "evidence": vuln_report["evidence"]},
                {"target": hard_report["target"],
                 "conclusion": hard_report["conclusion"],
                 "evidence": hard_report["evidence"]},
            ]
        }
        comp_path = os.path.join(REPORTS_DIR, "comparison_report.json")
        write_report(comparison, comp_path)
        with open(comp_path, encoding="utf-8") as f:
            comp = json.load(f)
        entries = comp["comparison"]
        ok10 = (len(entries) == 2
                and entries[0]["target"] == VULN_BASE
                and entries[1]["target"] == HARD_BASE
                and all(list(e.keys()) == ["target", "conclusion", "evidence"] for e in entries))
        check(10, "对比报告: 未加固在前加固在后, 字段顺序一致", ok10,
              "-> " + comp_path)
    finally:
        vuln_srv.shutdown()
        hard_srv.shutdown()

    print("\n== 结果汇总 ==")
    failed = [r for r in results if not r[2]]
    for no, desc, ok, _ in results:
        print("  验收%-2d %s %s" % (no, "PASS" if ok else "FAIL", desc))
    print("通过 %d/%d" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
