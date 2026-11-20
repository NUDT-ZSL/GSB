"""Acceptance demo: exercises every business requirement on local
deterministic samples and writes a verification report."""

import json
import shutil
import threading
import time
from pathlib import Path

from .bridge import BridgeResource
from .canon import canonical_bytes, canonical_text, checksum
from .engine import CrashSimulated, Engine
from .errors import XError
from .samplegen import SEED, generate_all, generate_bad_samples
from .store import BuildLock, StateStore
from .validate import Verifier


def _tweak_resource(src, dst, **overrides):
    data = json.loads(Path(src).read_text(encoding="utf-8"))
    params = data["scoring_weights"]["params"]
    params.update(overrides)
    data["scoring_weights"]["checksum"] = checksum(params)
    Path(dst).write_text(canonical_text(data), encoding="utf-8")
    return dst


class _Recorder:
    def __init__(self):
        self.checks = []
        self.errors = []

    def check(self, name, ok, **details):
        self.checks.append({"name": name, "ok": bool(ok), "details": details})

    def expect_error(self, name, code, func):
        try:
            func()
        except XError as exc:
            self.errors.append(exc.to_dict())
            self.check(name, exc.code == code,
                       expected=code, got=exc.code, context=exc.context)
            return exc
        self.check(name, False, expected=code, got="no error raised")
        return None


def run_demo(out_dir="artifacts"):
    out = Path(out_dir)
    demo = out / "demo"
    shutil.rmtree(demo, ignore_errors=True)
    data = demo / "data"
    demo.mkdir(parents=True, exist_ok=True)
    rec = _Recorder()

    generate_all(data)
    generate_bad_samples(demo)
    docs = str(data / "documents.jsonl")
    queries = str(data / "queries.jsonl")
    res_v1 = str(data / "bridge_v1.json")
    res_v2 = str(data / "bridge_v2.json")
    res_v3 = str(data / "bridge_v3.json")

    # 1. deterministic expansion ordering
    resource = BridgeResource.load(res_v1)
    cands = resource.expand_term("en", "c0000_en", "de")
    ordering_ok = all(
        (cands[i]["confidence"] > cands[i + 1]["confidence"]) or
        (cands[i]["confidence"] == cands[i + 1]["confidence"] and
         cands[i]["candidate"] <= cands[i + 1]["candidate"])
        for i in range(len(cands) - 1))
    exp_a = canonical_bytes(resource.expand_query(
        "en", ["c0000_en", "c0007_en"], ["de", "fr"])[0])
    exp_b = canonical_bytes(resource.expand_query(
        "en", ["c0000_en", "c0007_en"], ["de", "fr"])[0])
    rec.check("expansion_ordering_and_reproducibility",
              ordering_ok and exp_a == exp_b and len(cands) >= 1,
              candidates=cands)

    # 2. full build v1 + query determinism
    state_basic = demo / "state_basic"
    engine = Engine(state_basic)
    report_v1 = engine.build(res_v1, docs)
    rec.check("build_v1_all_partitions",
              sorted(report_v1["rebuilt"]) == ["de", "en", "fr", "zh"],
              rebuilt=report_v1["rebuilt"])
    results_v1 = demo / "results_v1.jsonl"
    engine.run_query_file(queries, str(results_v1))
    results_v1_repeat = demo / "results_v1_repeat.jsonl"
    Engine(state_basic).run_query_file(queries, str(results_v1_repeat))
    rec.check("query_determinism_bytes",
              results_v1.read_bytes() == results_v1_repeat.read_bytes())

    state_basic2 = demo / "state_basic2"
    Engine(state_basic2).build(res_v1, docs)
    snap_a = sorted((state_basic / "partitions").rglob("v*.json"))
    snap_b = sorted((state_basic2 / "partitions").rglob("v*.json"))
    rec.check("independent_rebuild_byte_identical",
              [p.name for p in snap_a] == [p.name for p in snap_b] and
              all(a.read_bytes() == b.read_bytes()
                  for a, b in zip(snap_a, snap_b)))

    # 3. incremental rebuild on resource v2 (only de affected)
    report_v2 = Engine(state_basic).build(res_v2, docs)
    rec.check("incremental_rebuild_v2_only_de",
              report_v2["rebuilt"] == ["de"] and
              sorted(report_v2["reused"]) == ["en", "fr", "zh"],
              affected=report_v2["affected"], reused=report_v2["reused"])
    results_v2 = demo / "results_v2.jsonl"
    Engine(state_basic).run_query_file(queries, str(results_v2))

    # 4. forced termination + partition-level recovery
    state_recovery = demo / "state_recovery"
    Engine(state_recovery).build(res_v1, docs)
    Engine(state_recovery).build(res_v2, docs)

    def crash_after_one(lang, count):
        if count >= 1:
            raise CrashSimulated(f"killed after committing {lang}")

    try:
        Engine(state_recovery).build(res_v3, docs, crash_hook=crash_after_one)
        rec.check("crash_simulation", False, note="build did not crash")
    except CrashSimulated:
        rec.check("crash_simulation", True)
    recovery_report = Engine(state_recovery).build(res_v3, docs)
    rec.check("recovery_report_reuse_vs_rebuild",
              recovery_report["rebuilt"] == ["fr"] and
              "en" in recovery_report["reused"] and
              "de" in recovery_report["reused"],
              reused=recovery_report["reused"],
              rebuilt=recovery_report["rebuilt"])

    state_fresh_v3 = demo / "state_fresh_v3"
    Engine(state_fresh_v3).build(res_v1, docs)
    Engine(state_fresh_v3).build(res_v2, docs)
    Engine(state_fresh_v3).build(res_v3, docs)
    res_recovery = demo / "results_recovery_v3.jsonl"
    res_fresh = demo / "results_fresh_v3.jsonl"
    Engine(state_recovery).run_query_file(queries, str(res_recovery))
    Engine(state_fresh_v3).run_query_file(queries, str(res_fresh))
    rec.check("recovery_results_byte_identical",
              res_recovery.read_bytes() == res_fresh.read_bytes())

    # 5. resource version changed during the interruption
    state_recovery2 = demo / "state_recovery2"
    Engine(state_recovery2).build(res_v1, docs)
    try:
        Engine(state_recovery2).build(res_v2, docs,
                                      crash_hook=crash_after_one)
    except CrashSimulated:
        pass
    report2 = Engine(state_recovery2).build(res_v3, docs)
    rec.check("recovery_with_newer_resource",
              sorted(report2["rebuilt"]) == ["en", "fr"] and
              sorted(report2["reused"]) == ["de", "zh"],
              reused=report2["reused"], rebuilt=report2["rebuilt"])
    fresh_rank = _ranking_tuples(Engine(state_fresh_v3), queries)
    rec2_rank = _ranking_tuples(Engine(state_recovery2), queries)
    rec.check("recovery2_rankings_equal_fresh", fresh_rank == rec2_rank)

    # 6. error codes
    rec.expect_error("E_DUP_DOC", "E_DUP_DOC",
                     lambda: Engine(demo / "state_errors_dup").build(
                         res_v1, str(demo / "bad_duplicate_docs.jsonl")))
    rec.expect_error("E_TERM_INVALID", "E_TERM_INVALID",
                     lambda: Engine(demo / "state_errors_term").build(
                         res_v1, str(demo / "bad_empty_term.jsonl")))
    rec.expect_error("E_CONFIDENCE_RANGE", "E_CONFIDENCE_RANGE",
                     lambda: Engine(demo / "state_errors_conf").build(
                         str(demo / "bad_confidence.json"), docs))
    rec.expect_error("E_BRIDGE_LANG", "E_BRIDGE_LANG",
                     lambda: Engine(demo / "state_errors_lang").build(
                         str(demo / "bad_bridge_lang.json"), docs))
    rec.expect_error("E_CONFIG_INVALID_weights", "E_CONFIG_INVALID",
                     lambda: Engine(demo / "state_errors_weights").build(
                         str(demo / "bad_weights.json"), docs))
    rec.expect_error("E_CONFIG_INVALID_malformed", "E_CONFIG_INVALID",
                     lambda: Engine(demo / "state_errors_json").build(
                         res_v1, str(demo / "malformed.jsonl")))
    rec.expect_error("E_CONFIG_INVALID_top_k", "E_CONFIG_INVALID",
                     lambda: Engine(state_basic).run_query_file(
                         str(demo / "bad_top_k.jsonl")))

    # 7. snapshot corruption
    state_corrupt = demo / "state_corrupt"
    shutil.copytree(state_basic, state_corrupt)
    victim = state_corrupt / "partitions" / "de" / "v000002.json"
    raw = victim.read_text(encoding="utf-8")
    victim.write_text(raw.replace('"doc_count"', '"doc_countX"', 1),
                      encoding="utf-8")
    rec.expect_error("E_SNAPSHOT_CORRUPT", "E_SNAPSHOT_CORRUPT",
                     lambda: Engine(state_corrupt).run_query_file(queries))

    # 8. concurrency: build busy + consistent query view
    state_conc = demo / "state_conc"
    Engine(state_conc).build(res_v1, docs)
    lock = BuildLock(state_conc)
    lock.acquire("demo-hold")
    rec.expect_error("E_BUILD_BUSY", "E_BUILD_BUSY",
                     lambda: Engine(state_conc).build(res_v2, docs))
    lock.release()

    pre = canonical_bytes(Engine(state_conc).run_query_file(queries))
    during = {}

    def slow_build():
        def pause(lang, count):
            time.sleep(0.4)
        Engine(state_conc).build(res_v2, docs, crash_hook=pause)

    thread = threading.Thread(target=slow_build)
    thread.start()
    time.sleep(0.1)
    during["bytes"] = canonical_bytes(
        Engine(state_conc).run_query_file(queries, workers=4))
    thread.join()
    rec.check("query_consistent_during_build", during["bytes"] == pre)
    post = Engine(state_conc).run_query_file(queries)
    rec.check("post_build_uses_new_bridge",
              all(r["bridge"]["resource_version"] == 2 for r in post))

    # 9. resource limits
    tiny_postings = str(demo / "res_tiny_postings.json")
    _tweak_resource(res_v1, tiny_postings, max_postings_per_partition=10)
    rec.expect_error("E_RESOURCE_LIMIT_postings", "E_RESOURCE_LIMIT",
                     lambda: Engine(demo / "state_limit_postings").build(
                         tiny_postings, docs))

    tiny_expand = str(demo / "res_tiny_expand.json")
    _tweak_resource(res_v1, tiny_expand, max_expansion_candidates=2)
    state_lim = demo / "state_limits"
    Engine(state_lim).build(tiny_expand, docs)
    lim_results = Engine(state_lim).run_query_file(queries)
    rec.check("degrade_expansion_truncated",
              any(any(d["category"] == "EXPANSION_TRUNCATED"
                      for d in r["degraded"]) for r in lim_results))

    tiny_score = str(demo / "res_tiny_score.json")
    _tweak_resource(res_v1, tiny_score, max_scoring_entries=5)
    state_lim2 = demo / "state_limits2"
    Engine(state_lim2).build(tiny_score, docs)
    lim2 = Engine(state_lim2).run_query_file(queries)
    rec.check("degrade_scoring_candidates_dropped",
              any(any(d["category"] == "SCORING_CANDIDATES_DROPPED"
                      for d in r["degraded"]) for r in lim2))

    tiny_score_err = str(demo / "res_tiny_score_err.json")
    _tweak_resource(res_v1, tiny_score_err, max_scoring_entries=5,
                    resource_policy="error")
    state_lim3 = demo / "state_limits3"
    Engine(state_lim3).build(tiny_score_err, docs)
    rec.expect_error("E_RESOURCE_LIMIT_scoring", "E_RESOURCE_LIMIT",
                     lambda: Engine(state_lim3).run_query_file(queries))

    # 10. verifier + selfcheck
    for name, state, res_file in (
            ("state_basic", state_basic, results_v2),
            ("state_fresh_v3", state_fresh_v3, res_fresh),
            ("state_recovery", state_recovery, res_recovery)):
        verifier = Verifier(state)
        try:
            p = verifier.verify_partitions(docs)
            r = verifier.verify_results(queries, str(res_file))
            s = verifier.selfcheck(docs)
            rec.check(f"verify_{name}", p["ok"] and r["ok"] and s["ok"],
                      partitions=p["partitions"],
                      queries_checked=r["queries_checked"])
        except XError as exc:
            rec.check(f"verify_{name}", False, error=exc.to_dict())

    # forged inconsistency: claim partition en still sits on bridge v1
    state_incon = demo / "state_inconsistent"
    shutil.copytree(state_fresh_v3, state_incon)
    store = StateStore(state_incon)
    current = store.load_current()
    current["partitions"]["en"]["bridge_resource_version"] = 1
    current["checksum"] = checksum(
        {k: v for k, v in current.items() if k != "checksum"})
    (state_incon / "CURRENT.json").write_text(canonical_text(current),
                                              encoding="utf-8")
    store.append_commit({"kind": "batch", "bridge": current["bridge"],
                         "partitions": current["partitions"]})

    def _selfcheck_incon():
        Verifier(state_incon).selfcheck(docs)

    exc = rec.expect_error("E_INCONSISTENT", "E_INCONSISTENT",
                           _selfcheck_incon)
    if exc is not None:
        rec.check("E_INCONSISTENT_category",
                  exc.context.get("category") == "REBUILD_SET_MISMATCH",
                  context=exc.context)

    passed = sum(1 for c in rec.checks if c["ok"])
    report = {
        "suite": "xretrieval acceptance",
        "seed": SEED,
        "checks": rec.checks,
        "errors_observed": rec.errors,
        "summary": {"total": len(rec.checks), "passed": passed,
                    "failed": len(rec.checks) - passed},
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "verification_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")
    return report


def _ranking_tuples(engine, queries_path):
    results = engine.run_query_file(queries_path)
    return [(r["query_id"], [(h["doc_id"], h["score"]) for h in r["results"]])
            for r in results]
