"""九项验收驱动：实际运行全部验收并输出 PASS/FAIL 与报告文件。

用法： python acceptance.py
退出码：全部通过为 0，任一失败为 1。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from framefuzz import codec, corpus as corpus_mod, dedup as dedup_mod
from framefuzz import diff as diff_mod, report as report_mod, runner

REPORTS = os.path.join(ROOT, "reports")
CHECKS: list[dict] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append({"name": name, "passed": bool(cond), "detail": detail})
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))


def sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def fresh_dir(path: str) -> str:
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path)
    return path


def a1():
    """验收1：五类畸形帧 -> 固定业务码，进程退出码 0，无异常栈。"""
    d = fresh_dir(os.path.join(REPORTS, "a1"))
    valid = codec.encode(b"hello")
    malformed = {
        "ERR_MAGIC": b"\x00\x01" + valid[2:],
        "ERR_VERSION": valid[:2] + b"\x7F" + valid[3:],
        "ERR_LENGTH_LIMIT": valid[:3] + (codec.MAX_PAYLOAD + 1).to_bytes(2, "big") + valid[5:],
        "ERR_LENGTH_MISMATCH": valid[:3] + (6).to_bytes(2, "big") + valid[5:],
        "ERR_CHECKSUM": valid[:-1] + bytes([valid[-1] ^ 0xFF]),
    }
    expected = {name: codec.ERROR_CODES[name] for name in malformed}
    ok, details, encode_cases = True, [], []
    for name, frame in malformed.items():
        code, _ = codec.decode(frame)
        fp = os.path.join(d, name + ".bin")
        with open(fp, "wb") as f:
            f.write(frame)
        r = subprocess.run([sys.executable, "-m", "framefuzz.codec", fp],
                           cwd=ROOT, capture_output=True, text=True)
        good = (code == expected[name] and r.returncode == 0
                and r.stderr == "" and f"code={expected[name]}" in r.stdout)
        ok = ok and good
        details.append(f"{name}={code}")
        encode_cases.append({"name": name, "code": code})
    check("A1 五类畸形帧返回固定业务码、退出码0、无异常栈", ok, "; ".join(details))
    return encode_cases


def a2():
    """验收2：seed=42 两次各200条，字节哈希一致，三类计数>0并写入报告。"""
    c1 = corpus_mod.generate(42, 200)
    c2 = corpus_mod.generate(42, 200)
    h1, h2 = corpus_mod.fingerprint(c1), corpus_mod.fingerprint(c2)
    counts = corpus_mod.counts_by_class(c1)
    rep = {"seed": 42, "count": len(c1), "fingerprint_run1": h1,
           "fingerprint_run2": h2, "identical": h1 == h2,
           "class_counts": counts}
    with open(os.path.join(REPORTS, "a2_corpus_report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2, sort_keys=True)
    ok = (h1 == h2 and len(c1) == 200
          and all(counts.get(cls, 0) > 0 for cls in corpus_mod.MUTATION_CLASSES))
    check("A2 固定种子语料确定性生成、三类计数为正", ok,
          f"fingerprint={h1[:16]}... counts={counts}")
    return c1, h1, counts


def a3(cases):
    """验收3：缺陷模式出现长度越界崩溃；关闭后同语料零崩溃，均按业务分类记录。"""
    before = runner.run_corpus(cases, "on", fresh_dir(os.path.join(REPORTS, "a3_defect_on")),
                               concurrency=4, case_timeout=5.0)
    after = runner.run_corpus(cases, "off", fresh_dir(os.path.join(REPORTS, "a3_defect_off")),
                              concurrency=4, case_timeout=5.0)
    oob = before["crash_counts"].get(runner.CAT_LENGTH_OOB, 0)
    total_crash_off = sum(after["crash_counts"].values())
    check("A3a 缺陷开启出现长度越界崩溃(业务分类记录)", oob >= 1,
          f"length_oob={oob} crash_counts={before['crash_counts']}")
    check("A3b 缺陷关闭同语料零崩溃", total_crash_off == 0,
          f"category_counts={after['category_counts']}")
    return before, after


def a4():
    """验收4：注入超时与强制终止；前者记录超时，后者仅凭检查点续跑、无完成报告。"""
    cases = corpus_mod.generate(7, 12)
    cases.append(corpus_mod.make_marker_case("case-9000", b"SLOW",
                                             corpus_mod.CLASS_INJECTED_TIMEOUT))
    cases.append(corpus_mod.make_marker_case("case-9001", b"ABORT",
                                             corpus_mod.CLASS_INJECTED_ABORT))
    wdir = fresh_dir(os.path.join(REPORTS, "a4_recover"))
    snap = os.path.join(wdir, "snapshot.json")
    ckpt = os.path.join(wdir, "checkpoint.json")

    run1 = runner.run_corpus(cases, "on", wdir, concurrency=4, case_timeout=2.0)
    cond1 = (run1["status"] == runner.STATUS_ABORTED
             and not os.path.exists(snap)          # 不得输出完成报告
             and os.path.exists(ckpt)              # 持久化检查点存在
             and len(run1["unexecuted"]) > 0)      # 未执行清单非空
    check("A4a 强制终止：无完成报告、检查点含未执行清单", cond1,
          f"status={run1['status']} unexecuted={len(run1['unexecuted'])}")

    run2 = runner.run_corpus(cases, "on", wdir, concurrency=4, case_timeout=2.0)
    resumed = any(e["event"] == "checkpoint_resume" for e in run2["recovery_events"])
    by_id = {r["id"]: r for r in run2["results"]}
    cond2 = (run2["status"] == runner.STATUS_COMPLETED and resumed
             and by_id["case-9000"]["category"] == runner.CAT_PARSE_TIMEOUT
             and by_id["case-9001"]["category"] == runner.CAT_UNRECOVERABLE
             and os.path.exists(snap))
    check("A4b 重启仅凭检查点续跑：超时与不可恢复分类均落账", cond2,
          f"resume={resumed} slow={by_id['case-9000']['category']} "
          f"abort={by_id['case-9001']['category']}")
    return run1, run2


def a5(before, after, cases):
    """验收5：同一语料两轮差异，三类结论按用例ID升序、可复算、顺序确定。"""
    before2 = runner.run_corpus(cases, "on", fresh_dir(os.path.join(REPORTS, "a5_defect_on_rerun")),
                                concurrency=4, case_timeout=5.0)
    det = before["results"] == before2["results"]
    d1 = diff_mod.diff(before["results"], after["results"])
    d2 = diff_mod.diff(before2["results"], after["results"])
    ids_sorted = all(
        [e["id"] for e in lst] == sorted(e["id"] for e in lst)
        for lst in (d1["new_crashes"], d1["disappeared_crashes"], d1["behavior_flips"]))
    recountable = all(
        d1["counts"][k] == len(d1[k])
        for k in ("new_crashes", "disappeared_crashes", "behavior_flips"))
    ok = (det and d1 == d2 and ids_sorted and recountable
          and d1["counts"]["disappeared_crashes"] > 0
          and d1["counts"]["behavior_flips"] > 0)
    check("A5 差异三类结论升序、可复算、两轮一致", ok,
          f"counts={d1['counts']}")
    return d1


def a6():
    """验收6：并发上限4+单例时限，两个失控用例真实终止，无残留子进程。"""
    cases = corpus_mod.generate(11, 8)
    cases.append(corpus_mod.make_marker_case("case-9100", b"SLOW",
                                             corpus_mod.CLASS_INJECTED_TIMEOUT))
    cases.append(corpus_mod.make_marker_case("case-9101", b"SLOW",
                                             corpus_mod.CLASS_INJECTED_TIMEOUT))
    r = runner.run_corpus(cases, "on", fresh_dir(os.path.join(REPORTS, "a6_concurrency")),
                          concurrency=4, case_timeout=2.0)
    slows = [x for x in r["results"] if x["class"] == corpus_mod.CLASS_INJECTED_TIMEOUT]
    ok = (r["status"] == runner.STATUS_COMPLETED
          and len(slows) == 2
          and all(x["category"] == runner.CAT_PARSE_TIMEOUT for x in slows)
          and r["leftover_processes"] == []
          and r["max_concurrent_observed"] <= 4
          and "unexecuted" in r and "crash_counts" in r)  # 两列分列
    check("A6 有界并发真实终止失控用例、无残留子进程", ok,
          f"timeouts={len(slows)} leftover={r['leftover_processes']} "
          f"max_concurrent={r['max_concurrent_observed']} "
          f"crash_counts={r['crash_counts']} unexecuted={r['unexecuted']}")
    return r


def a7():
    """验收7：60%处替换不同seed语料恢复 -> 指纹不一致整轮重启，旧偏移作废。"""
    wdir = fresh_dir(os.path.join(REPORTS, "a7_snapshot"))
    cases_old = corpus_mod.generate(42, 60)
    run1 = runner.run_corpus(cases_old, "off", wdir,
                             concurrency=4, case_timeout=5.0, max_cases=36)  # 60%
    cond1 = run1["status"] == runner.STATUS_INTERRUPTED and len(run1["results"]) == 36

    cases_new = corpus_mod.generate(777, 60)  # 同ID不同字节 -> 指纹变化
    run2 = runner.run_corpus(cases_new, "off", wdir, concurrency=4, case_timeout=5.0)
    restarted = any(e["event"] == "fingerprint_mismatch_restart"
                    for e in run2["recovery_events"])
    cond2 = (run2["status"] == runner.STATUS_COMPLETED
             and restarted
             and run2["executed_this_run"] == 60          # 整轮重启而非续跑24条
             and len(run2["results"]) == 60
             and run2["fingerprint"] == corpus_mod.fingerprint(cases_new))
    check("A7 指纹变化整轮重启、旧偏移不生效", cond1 and cond2,
          f"中断于{len(run1['results'])}/60, 重启执行{run2['executed_this_run']}条, "
          f"restart_event={restarted}")
    return run1, run2


def a8():
    """验收8：内容哈希为主键去重，来源不参与主键，二次执行结果哈希不变。"""
    base = corpus_mod.generate(5, 8)
    dup = dict(base[0])
    dup.update({"id": "case-9000", "source": "fuzz-b", "round": 1})
    merged = dedup_mod.dedup(base + [dup])
    m0 = next(m for m in merged if m["id"] == "case-0000")
    srcs = sorted({base[0]["source"], "fuzz-b"})
    cond_dedup = (len(merged) == 8 and m0["sources"] == srcs)

    other = dict(base[0]); other["source"] = "totally-different-source"
    cond_key = dedup_mod.content_key(other) == dedup_mod.content_key(base[0])

    r1 = runner.run_corpus(merged, "off", fresh_dir(os.path.join(REPORTS, "a8_run1")),
                           concurrency=4, case_timeout=5.0)
    r2 = runner.run_corpus(merged, "off", fresh_dir(os.path.join(REPORTS, "a8_run2")),
                           concurrency=4, case_timeout=5.0)
    h1 = sha256_file(os.path.join(REPORTS, "a8_run1", "snapshot.json"))
    h2 = sha256_file(os.path.join(REPORTS, "a8_run2", "snapshot.json"))
    ok = cond_dedup and cond_key and h1 == h2
    check("A8 内容主键去重、来源不入键、重复执行幂等", ok,
          f"合并{len(base)+1}->{len(merged)}条 sources={m0['sources']} "
          f"snapshot_hash={h1[:16]}... 一致={h1==h2}")
    return merged


def a9(corpus_meta, encode_cases, before, diff_result, recovery_records):
    """验收9：报告三模块错误码一致；缺字段或码值冲突判失败。"""
    execute_stats = {
        "classifications": before["category_counts"],
        "crashes": before["crash_counts"],
        "total": len(before["results"]),
    }
    rep = report_mod.build_report(corpus_meta, encode_cases, execute_stats,
                                  recovery_records, diff_result)
    with open(os.path.join(REPORTS, "final_report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2, sort_keys=True)

    problems_ok = report_mod.validate_report(rep)
    codes_consistent = (rep["encode"]["error_codes"]
                        == rep["execute"]["error_codes"]
                        == rep["recover"]["error_codes"])

    bad1 = copy.deepcopy(rep)
    del bad1["execute"]["classifications"]
    bad2 = copy.deepcopy(rep)
    bad2["recover"]["error_codes"]["OK"] = 999  # 码值冲突
    cond = (problems_ok == [] and codes_consistent
            and report_mod.validate_report(bad1) != []
            and report_mod.validate_report(bad2) != [])
    check("A9 报告三模块码值一致、缺字段/码值冲突判失败", cond,
          f"正常报告问题数={len(problems_ok)} "
          f"删字段检出={len(report_mod.validate_report(bad1))} "
          f"改码值检出={len(report_mod.validate_report(bad2))}")
    return rep


def main() -> int:
    fresh_dir(REPORTS)
    print("=== framefuzz 九项验收 ===")
    encode_cases = a1()
    cases, fp, counts = a2()
    before, after = a3(cases)
    run1_a4, run2_a4 = a4()
    diff_result = a5(before, after, cases)
    a6()
    run1_a7, run2_a7 = a7()
    a8()
    recovery_records = (run1_a4["recovery_events"] + run2_a4["recovery_events"]
                        + run2_a7["recovery_events"])
    corpus_meta = {"seed": 42, "count": len(cases), "fingerprint": fp,
                   "class_counts": counts}
    a9(corpus_meta, encode_cases, before, diff_result, recovery_records)

    passed = sum(1 for c in CHECKS if c["passed"])
    summary = {"total": len(CHECKS), "passed": passed,
               "all_passed": passed == len(CHECKS), "checks": CHECKS}
    with open(os.path.join(REPORTS, "acceptance_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False, sort_keys=True)
    print(f"=== 结果: {passed}/{len(CHECKS)} 通过 -> "
          f"{os.path.join('reports', 'acceptance_summary.json')} ===")
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
