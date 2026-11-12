"""End-to-end acceptance tests for the macroanalyzer CLI.

Runs the real CLI as subprocesses against the deterministic built-in
samples and checks every acceptance item (1-10). Exits non-zero on failure.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RESULTS = []


def check(item, name, cond, detail=""):
    RESULTS.append((item, name, bool(cond), detail))
    print("[%s] item %s: %s %s" % ("PASS" if cond else "FAIL", item, name,
                                   ("-- " + detail) if detail and not cond
                                   else ""))


def run_cli(*cli_args):
    return subprocess.run(
        [sys.executable, "-m", "macroanalyzer", *cli_args],
        cwd=REPO_ROOT, capture_output=True, text=True)


def load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main():
    work = tempfile.mkdtemp(prefix="macroanalyzer_accept_")
    try:
        samples = os.path.join(work, "samples")
        res = run_cli("gensamples", "--out", samples)
        assert res.returncode == 0, res.stderr
        S = lambda name: os.path.join(samples, name)  # noqa: E731

        # ---------------------------------------------------------- item 1
        out1 = os.path.join(work, "r1.json")
        res = run_cli("analyze", S("ooxml_macro.docm"), S("ooxml_nomacro.docx"),
                      S("ole_macro.doc"), S("corrupt.bin"), "--out", out1)
        rep = load(out1)
        docs = rep["documents"]
        check(1, "formats", [d["format"] for d in docs]
              == ["ooxml", "ooxml", "ole", "unknown"],
              str([d["format"] for d in docs]))
        check(1, "no_macro status", docs[1]["status"] == "no_macro"
              and docs[2]["status"] != "no_macro")
        check(1, "corrupt parse_error", docs[3]["status"] == "parse_error")
        check(1, "exit code 2", res.returncode == 2,
              "exit=%d" % res.returncode)
        # single-doc mode semantics identical
        out1s = os.path.join(work, "r1s.json")
        res_s = run_cli("analyze", S("ooxml_nomacro.docx"), "--out", out1s)
        rep_s = load(out1s)
        check(1, "single-doc mode consistent",
              rep_s["documents"][0]["format"] == "ooxml"
              and rep_s["documents"][0]["status"] == "no_macro"
              and set(rep_s["documents"][0]) == set(docs[0])
              and res_s.returncode == 0)

        # ---------------------------------------------------------- item 2
        out2 = os.path.join(work, "r2.json")
        res = run_cli("analyze", S("truncated_modules.docm"), "--out", out2)
        rep = load(out2)
        doc = rep["documents"][0]
        check(2, "exit 0", res.returncode == 0, "exit=%d" % res.returncode)
        check(2, "3 modules", len(doc["modules"]) == 3,
              str(len(doc["modules"])))
        by_name = {m["name"]: m for m in doc["modules"]}
        check(2, "modules 1&3 normalized",
              by_name["ModA"]["normalized"] and by_name["ModC"]["normalized"])
        check(2, "module 2 decode_error",
              by_name["ModB"]["status"] == "decode_error"
              and by_name["ModB"]["errors"],
              str(by_name.get("ModB")))
        check(2, "doc status analyzed", doc["status"] == "analyzed")

        # ---------------------------------------------------------- item 3
        out3 = os.path.join(work, "r3.json")
        res = run_cli("analyze", S("ooxml_macro.docm"), "--out", out3)
        doc = load(out3)["documents"][0]
        types = [f["type"] for f in doc["features"]]
        check(3, "auto_exec present", "auto_exec" in types)
        check(3, "process_create present", "process_create" in types)
        proc = next(f for f in doc["features"]
                    if f["type"] == "process_create")
        check(3, "process_create evidence",
              proc["evidence"]["module"] == "ThisDocument"
              and isinstance(proc["evidence"]["statement"], int)
              and proc["evidence"]["statement"] > 0,
              str(proc))
        concat = [f for f in doc["features"]
                  if f["type"] == "obfuscation_string_concat"]
        check(3, "concat flagged after folding", len(concat) >= 1
              and any("AB" in (f.get("detail") or "") for f in concat),
              str(concat))
        # the plain inert string statement must not be flagged
        note_stmt = next(m for m in doc["modules"]
                         if m["name"] == "ThisDocument")
        note_idx = next(i + 1 for i, s in enumerate(note_stmt["normalized"])
                        if "inert inside a string" in s)
        bad = [f for f in doc["features"]
               if f["evidence"]["statement"] == note_idx
               and f["evidence"]["module"] == "ThisDocument"]
        check(3, "no false positive from plain string", not bad, str(bad))

        # ---------------------------------------------------------- item 4
        out4b = os.path.join(work, "r4b.json")
        res = run_cli("analyze", S("ooxml_macro.docm"),
                      "--rules", S("rules_broken.json"), "--out", out4b)
        rep = load(out4b)
        check(4, "broken ruleset exit 3", res.returncode == 3,
              "exit=%d" % res.returncode)
        check(4, "rules_load_error reported",
              bool(rep["rules_load_error"])
              and rep["used_ruleset"] == "builtin")
        check(4, "features still extracted",
              len(rep["documents"][0]["features"]) > 0
              and rep["documents"][0]["matched_rules"] is not None)
        out4v = os.path.join(work, "r4v.json")
        res = run_cli("analyze", S("ooxml_macro.docm"),
                      "--rules", S("rules_valid.json"), "--out", out4v)
        rep = load(out4v)
        doc = rep["documents"][0]
        hits = doc["matched_rules"]
        sev_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
        check(4, "valid ruleset exit 0", res.returncode == 0,
              "exit=%d" % res.returncode)
        check(4, "exactly 2 hits", len(hits) == 2, str(hits))
        check(4, "confidence in (0,1)",
              0.0 < doc["confidence"] < 1.0, str(doc["confidence"]))
        check(4, "hits sorted by severity desc",
              [sev_rank[h["severity"]] for h in hits]
              == sorted([sev_rank[h["severity"]] for h in hits],
                        reverse=True))
        check(4, "used_ruleset from file",
              rep["used_ruleset"] == "local-rules-1.0")

        # ---------------------------------------------------------- item 5
        out5 = os.path.join(work, "r5.json")
        inputs5 = [S("oversize.docm"), S("corrupt.bin"), S("ooxml_macro.docm"),
                   S("ole_macro.doc"), S("truncated_modules.docm"),
                   S("dup_different.docm")]
        res = run_cli("analyze", *inputs5, "--out", out5,
                      "--jobs", "2", "--max-bytes", str(1024 * 1024))
        rep = load(out5)
        docs = rep["documents"]
        check(5, "order preserved",
              [os.path.basename(d["path"]) for d in docs]
              == [os.path.basename(p) for p in inputs5],
              str([d["path"] for d in docs]))
        check(5, "size_exceeded", docs[0]["status"] == "size_exceeded")
        check(5, "parse_error", docs[1]["status"] == "parse_error")
        check(5, "others analyzed",
              all(d["status"] == "analyzed" for d in docs[2:]),
              str([d["status"] for d in docs]))
        check(5, "concurrency peak <= 2",
              rep["concurrency"]["peak"] <= 2
              and rep["concurrency"]["max_workers"] == 2,
              str(rep["concurrency"]))

        # ---------------------------------------------------------- item 6
        out6 = os.path.join(work, "r6.json")
        res = run_cli("analyze", S("slow_timeout.docm"), S("ooxml_macro.docm"),
                      S("ooxml_nomacro.docx"), "--out", out6,
                      "--timeout-ms", "200", "--jobs", "2")
        rep = load(out6)
        docs = rep["documents"]
        slow = docs[0]
        check(6, "timeout status", slow["status"] == "timeout",
              slow["status"])
        check(6, "truncated flag", slow["truncated"] is True)
        check(6, "partial modules kept", len(slow["modules"]) > 0,
              str(len(slow["modules"])))
        check(6, "timeout error recorded",
              any("timeout" in e.lower() for e in slow["errors"]),
              str(slow["errors"]))
        check(6, "other docs unaffected",
              docs[1]["status"] == "analyzed"
              and docs[2]["status"] == "no_macro",
              str([d["status"] for d in docs[1:]]))
        check(6, "exit code 2", res.returncode == 2,
              "exit=%d" % res.returncode)

        # ---------------------------------------------------------- item 7
        out7 = os.path.join(work, "r7.json")
        res = run_cli("analyze", S("dup_a.docm"), S("dup_b.docm"),
                      S("dup_c.docm"), S("dup_different.docm"), "--out", out7)
        rep = load(out7)
        docs = rep["documents"]
        check(7, "exit 0", res.returncode == 0, "exit=%d" % res.returncode)
        check(7, "unique payloads = 2", rep["aggregate"]["unique_payloads"] == 2
              and rep["aggregate"]["duplicates"] == 2,
              str(rep["aggregate"]))
        check(7, "duplicate_of chain",
              docs[1]["duplicate_of"] == docs[0]["id"]
              and docs[2]["duplicate_of"] == docs[0]["id"]
              and docs[0]["duplicate_of"] is None
              and docs[3]["duplicate_of"] is None,
              str([d["duplicate_of"] for d in docs]))
        fps = [g["fingerprint"] for g in rep["aggregate"]["by_fingerprint"]]
        check(7, "groups sorted by fingerprint", fps == sorted(fps))
        # hits consistent with per-document results
        recount = {}
        for d in docs:
            for h in d["matched_rules"]:
                recount[h["id"]] = recount.get(h["id"], 0) + 1
        check(7, "aggregate hits consistent",
              rep["aggregate"]["rule_hits"] == recount,
              "%s vs %s" % (rep["aggregate"]["rule_hits"], recount))
        check(7, "canonical is first doc",
              all(g["canonical"] == g["members"][0]
                  for g in rep["groups"]))

        # ---------------------------------------------------------- item 8
        check(8, "schema_version is string",
              isinstance(rep["schema_version"], str))
        out8 = os.path.join(work, "r8.json")
        res = run_cli("merge", S("legacy_report.json"), "--out", out8)
        merged = load(out8)
        check(8, "merge exit 0", res.returncode == 0,
              "exit=%d" % res.returncode)
        mdoc = merged["documents"][0]
        check(8, "defaults filled",
              mdoc["truncated"] is False and mdoc["modules"] == []
              and isinstance(mdoc["features"][0]["evidence"], dict))
        check(8, "warnings list missing fields",
              any("evidence" in w for w in merged["warnings"])
              and any("truncated" in w for w in merged["warnings"]),
              str(merged["warnings"]))
        # single vs batch field consistency
        check(8, "schema consistent across modes",
              set(load(out1s)["documents"][0].keys())
              == set(load(out1)["documents"][0].keys()))

        # ---------------------------------------------------------- item 9
        out9ok = os.path.join(work, "r9ok.json")
        res_ok = run_cli("analyze", S("ooxml_macro.docm"), "--out", out9ok)
        out9bad = os.path.join(work, "r9bad.json")
        res_bad = run_cli("analyze", S("corrupt.bin"), "--out", out9bad)
        out9rules = os.path.join(work, "r9rules.json")
        res_rules = run_cli("analyze", S("ooxml_macro.docm"),
                            "--rules", S("rules_broken.json"), "--out",
                            out9rules)
        out9args = os.path.join(work, "r9args.json")
        res_args = run_cli("analyze", S("ooxml_macro.docm"),
                           "--jobs", "0", "--out", out9args)
        check(9, "exit 0 all-success", res_ok.returncode == 0)
        check(9, "exit 2 partial", res_bad.returncode == 2
              and os.path.isfile(out9bad))
        check(9, "exit 3 rules", res_rules.returncode == 3
              and os.path.isfile(out9rules))
        check(9, "exit 4 bad args", res_args.returncode == 4
              and res_args.stderr.strip()
              and not os.path.exists(out9args),
              "exit=%d stderr=%r" % (res_args.returncode, res_args.stderr))
        res_args2 = run_cli("analyze", S("does_not_exist.docm"),
                            "--out", out9args)
        check(9, "exit 4 missing input", res_args2.returncode == 4
              and not os.path.exists(out9args))

        # --------------------------------------------------------- item 10
        res = run_cli("verify", "--report", out9ok)
        check(10, "verify OK exit 0", res.returncode == 0
              and "OK" in res.stdout, res.stdout + res.stderr)
        rep = load(out9ok)
        doc = rep["documents"][0]
        check(10, "analysis_ms non-negative number",
              isinstance(doc["analysis_ms"], (int, float))
              and doc["analysis_ms"] >= 0)
        check(10, "ruleset fingerprint present",
              isinstance(rep["used_ruleset_fingerprint"], str)
              and len(rep["used_ruleset_fingerprint"]) == 64)
        # tamper with the document, verify must fail
        with open(S("ooxml_macro.docm"), "ab") as fh:
            fh.write(b"\x00")
        res = run_cli("verify", "--report", out9ok)
        check(10, "tamper detected exit 2", res.returncode == 2,
              "exit=%d" % res.returncode)
        check(10, "mismatch names input_sha256 and doc id",
              "input_sha256" in res.stdout and doc["id"] in res.stdout,
              res.stdout)
    finally:
        failed = [r for r in RESULTS if not r[2]]
        print("\n%d checks, %d failed" % (len(RESULTS), len(failed)))
        print("workdir: %s" % work)
        if os.environ.get("KEEP_WORK") != "1" and not failed:
            shutil.rmtree(work, ignore_errors=True)
    return 1 if any(not r[2] for r in RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
