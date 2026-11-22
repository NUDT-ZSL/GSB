"""Command-line entry: chunked attention demo, verification and JSON report.

Usage:
    python -m chunked_attn.cli generate --out samples
    python -m chunked_attn.cli run --manifest samples/cases.jsonl --data-dir samples/arrays --snapshots snapshots --out outputs/report.json --with-probes
    python -m chunked_attn.cli selfcheck --manifest samples/cases.jsonl --data-dir samples/arrays --out outputs/selfcheck.json
    python -m chunked_attn.cli demo
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys

import numpy as np

from . import consistency, core, samples, stability, validation
from .api import ChunkedAttentionOperator
from .config import StabilityConfig
from .errors import (BizError, E_BLOCK_MISMATCH, E_DUPLICATE_CASE_ID,
                     E_INCONSISTENT, E_LATENCY_BUDGET, E_SNAPSHOT_CORRUPT,
                     E_SNAPSHOT_ROLLBACK, E_STABILITY_VIOLATION)
from .planner import plan_blocks
from .ready import BlockReadySession
from .selfcheck import self_check
from .snapshot import SnapshotStore


def _checksum_f64(arr):
    import hashlib
    return hashlib.sha256(
        np.ascontiguousarray(arr, dtype=np.float64).tobytes()).hexdigest()


def _norm_params_payload(run, cfg):
    heads = []
    for head_blocks in run.q_blocks:
        heads.append([{
            "q_block_idx": qb.q_block_idx,
            "q_start": qb.q_start,
            "q_end": qb.q_end,
            "m_checksum": _checksum_f64(qb.m),
            "denom_checksum": _checksum_f64(qb.denom),
            "kv_stat_checksums": [st.checksum() for st in qb.kv_stats],
        } for qb in head_blocks])
    return {"config": cfg.to_dict(), "heads": heads}


def _run_case(op, record, q, k, v):
    """Full per-case pipeline; returns (report_entry, selfcheck_payload)."""
    cid = record["case_id"]
    plan = plan_blocks(record)
    q64 = np.ascontiguousarray(q, dtype=np.float64)
    k64 = np.ascontiguousarray(k, dtype=np.float64)
    v64 = np.ascontiguousarray(v, dtype=np.float64)
    session = BlockReadySession(record, q64, k64, v64, plan, op.cfg)
    run = session.run()
    ready_query = session.query_ready()
    whole = core.run_whole(q64, k64, v64, plan.kv_block_len,
                           plan.causal, op.cfg)
    verdict = consistency.adjudicate(run, whole, case_id=cid)
    stab = stability.verify_run(run, whole, op.cfg, case_id=cid)
    rerun = core.run_chunked(q64, k64, v64, plan.q_block_len,
                             plan.kv_block_len, plan.causal, op.cfg)
    deterministic = (
        rerun.output_checksum() == run.output_checksum()
        and rerun.stats_checksum() == run.stats_checksum())
    store = op.snapshots
    snap_meta = {}
    for kind, payload in (
            ("block_plan", plan.to_dict()),
            ("norm_params", _norm_params_payload(run, op.cfg)),
            ("ready_list", {"case_id": cid, "blocks": [
                {kk: vv for kk, vv in b.items() if kk != "output"}
                for b in ready_query["blocks"]]})):
        version = store.next_version(kind, cid)
        snap_meta[kind] = store.commit(kind, cid, version, payload)
    entry = {
        "case_id": cid,
        "status": "ok",
        "plan": plan.to_dict(),
        "first_block": {
            "measured_ms": session.first_block_latency_ms,
            "estimate": ready_query["estimate"],
            "budget_ms": plan.latency_budget_ms,
            "within_budget":
                session.first_block_latency_ms <= plan.latency_budget_ms,
        },
        "ready_blocks": [
            {kk: vv for kk, vv in b.items() if kk != "output"}
            for b in ready_query["blocks"]],
        "consistency": verdict,
        "stability": stab,
        "deterministic": deterministic,
        "output_checksum": run.output_checksum(),
        "stats_checksum": run.stats_checksum(),
        "snapshots": snap_meta,
    }
    sc_payload = {"case_id": cid, "chunked_run": run, "whole_run": whole,
                  "ready_blocks": session.ready_blocks}
    return entry, sc_payload


def run_pipeline(manifest_path, data_dir, snapshot_dir, cfg=None):
    """Execute the full pipeline; returns (report_dict, exit_ok)."""
    cfg = cfg or StabilityConfig()
    op = ChunkedAttentionOperator(snapshot_dir, cfg=cfg)
    records = samples.load_manifest(manifest_path)
    case_entries, sc_payloads, errors = [], [], []
    seen = set()
    for rec in records:
        cid = rec.get("case_id")
        try:
            validation.validate_record(rec)
            if cid in seen:
                raise BizError(E_DUPLICATE_CASE_ID,
                               "duplicate case_id '%s'" % cid,
                               field="case_id", case_id=cid,
                               stage="manifest_validate")
            seen.add(cid)
            q, k, v = samples.load_arrays(data_dir, cid)
            validation.validate_arrays(rec, q, k, v)
            entry, sc = _run_case(op, rec, q, k, v)
            case_entries.append(entry)
            sc_payloads.append(sc)
        except BizError as exc:
            errors.append({"case_id": cid, "status": "error",
                           "error": exc.to_dict()})
        except Exception as exc:
            errors.append({"case_id": cid, "status": "error",
                           "error": {"code": "E_INTERNAL",
                                     "message": repr(exc),
                                     "stage": "compute"}})
    try:
        sc = self_check(sc_payloads, cfg) if sc_payloads else {
            "verdict": "OK", "cases_checked": []}
        sc_error = None
    except BizError as exc:
        sc, sc_error = None, exc.to_dict()
    report = {
        "config": cfg.to_dict(),
        "cases": case_entries,
        "case_errors": errors,
        "selfcheck": sc,
        "selfcheck_error": sc_error,
        "summary": {
            "total": len(records),
            "ok": len(case_entries),
            "errors": len(errors),
            "all_byte_identical": all(
                e["consistency"]["byte_identical"] for e in case_entries),
            "all_deterministic": all(
                e["deterministic"] for e in case_entries),
            "all_within_budget": all(
                e["first_block"]["within_budget"] for e in case_entries),
        },
    }
    ok = not errors and sc_error is None and bool(case_entries)
    return report, ok


def _probe(name, expected_code, fn):
    try:
        fn()
        return {"name": name, "expected_code": expected_code,
                "actual_code": None, "passed": False,
                "message": "no error raised"}
    except BizError as exc:
        return {"name": name, "expected_code": expected_code,
                "actual_code": exc.code, "passed": exc.code == expected_code,
                "error": exc.to_dict()}


def run_probes(snapshot_dir, cfg):
    """Negative probes demonstrating each structured business code."""
    probes = []
    base = copy.deepcopy(samples.SAMPLE_CASES[0])
    q, k, v = samples.gen_arrays(base)
    q64 = q.astype(np.float64)
    k64 = k.astype(np.float64)
    v64 = v.astype(np.float64)

    def latency():
        rec = dict(base, latency_budget_ms=1e-9)
        plan = plan_blocks(rec)
        s = BlockReadySession(rec, q64, k64, v64, plan, cfg)
        s.run()
    probes.append(_probe("latency_budget", E_LATENCY_BUDGET, latency))

    store = SnapshotStore(os.path.join(snapshot_dir, "probe"))

    def rollback():
        store.commit("block_plan", "probe-case", 1, {"a": 1})
        store.commit("block_plan", "probe-case", 1, {"a": 2})
    probes.append(_probe("snapshot_rollback", E_SNAPSHOT_ROLLBACK, rollback))

    def corrupt():
        store.commit("ready_list", "probe-case", 1, {"blocks": []})
        path = store._path("ready_list", "probe-case")
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["payload"]["blocks"] = [{"tampered": True}]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        store.load("ready_list", "probe-case")
    probes.append(_probe("snapshot_corrupt", E_SNAPSHOT_CORRUPT, corrupt))

    def dup_case():
        validation.validate_manifest([base, dict(base)])
    probes.append(_probe("duplicate_case_id", E_DUPLICATE_CASE_ID, dup_case))

    def bad_causal():
        validation.validate_record(dict(base, causal="yes"))
    probes.append(_probe("invalid_causal", "E_INVALID_CAUSAL", bad_causal))

    def bad_block():
        validation.validate_record(dict(base, kv_block_len=0))
    probes.append(_probe("invalid_block", "E_INVALID_BLOCK", bad_block))

    def heads_mismatch():
        validation.validate_arrays(base, q, k, v[:-1])
    probes.append(_probe("heads_mismatch", "E_HEADS_MISMATCH", heads_mismatch))

    def dim_mismatch():
        validation.validate_arrays(base, q, k[:, :-1], v)
    probes.append(_probe("dim_mismatch", "E_DIM_MISMATCH", dim_mismatch))

    def _runs():
        rec = dict(base)
        plan = plan_blocks(rec)
        run = core.run_chunked(q64, k64, v64, plan.q_block_len,
                               plan.kv_block_len, plan.causal, cfg)
        whole = core.run_whole(q64, k64, v64, plan.kv_block_len,
                               plan.causal, cfg)
        return rec, plan, run, whole

    def stability_violation():
        rec, plan, run, whole = _runs()
        run.q_blocks[0][0].kv_stats[0].l_blk += 1.0
        stability.verify_run(run, whole, cfg, case_id="probe")
    probes.append(_probe("stability_violation", E_STABILITY_VIOLATION,
                         stability_violation))

    def block_mismatch():
        rec, plan, run, whole = _runs()
        run.output[0, 0, 0] += 1.0
        consistency.adjudicate(run, whole, case_id="probe")
    probes.append(_probe("block_mismatch", E_BLOCK_MISMATCH, block_mismatch))

    def inconsistent():
        rec, plan, run, whole = _runs()
        bad_ready = [{"q_block_idx": 0, "q_start": 0,
                      "q_end": plan.q_block_len,
                      "output_checksum": "0" * 64}]
        self_check([{"case_id": "probe", "chunked_run": run,
                     "whole_run": whole, "ready_blocks": bad_ready}], cfg)
    probes.append(_probe("inconsistent", E_INCONSISTENT, inconsistent))
    return probes


def _write_json(path, obj):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=True, indent=2)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="chunked_attn",
        description="Deterministic chunked-attention operator demo")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_gen = sub.add_parser("generate", help="generate deterministic samples")
    p_gen.add_argument("--out", default="samples")
    p_run = sub.add_parser("run", help="run the chunked pipeline")
    p_run.add_argument("--manifest", default="samples/cases.jsonl")
    p_run.add_argument("--data-dir", default="samples/arrays")
    p_run.add_argument("--snapshots", default="snapshots")
    p_run.add_argument("--out", default="outputs/report.json")
    p_run.add_argument("--with-probes", action="store_true")
    p_sc = sub.add_parser("selfcheck", help="run cross-module self-check")
    p_sc.add_argument("--manifest", default="samples/cases.jsonl")
    p_sc.add_argument("--data-dir", default="samples/arrays")
    p_sc.add_argument("--snapshots", default="snapshots")
    p_sc.add_argument("--out", default="outputs/selfcheck.json")
    p_demo = sub.add_parser("demo", help="generate + run + selfcheck")
    p_demo.add_argument("--workdir", default=".")
    args = parser.parse_args(argv)

    if args.cmd == "generate":
        path = samples.generate_samples(args.out)
        print("samples written to %s" % path)
        return 0

    if args.cmd in ("run", "selfcheck"):
        report, ok = run_pipeline(args.manifest, args.data_dir,
                                  args.snapshots)
        if args.cmd == "run" and args.with_probes:
            probes = run_probes(args.snapshots, StabilityConfig())
            report["probes"] = probes
            report["summary"]["probes_passed"] = sum(
                1 for p in probes if p["passed"])
            report["summary"]["probes_total"] = len(probes)
            ok = ok and all(p["passed"] for p in probes)
        _write_json(args.out, report)
        print("report written to %s (ok=%s)" % (args.out, ok))
        print(json.dumps(report["summary"], ensure_ascii=True, indent=2))
        return 0 if ok else 1

    if args.cmd == "demo":
        root = args.workdir
        manifest = samples.generate_samples(os.path.join(root, "samples"))
        report, ok = run_pipeline(
            manifest, os.path.join(root, "samples", "arrays"),
            os.path.join(root, "snapshots"))
        probes = run_probes(os.path.join(root, "snapshots"),
                            StabilityConfig())
        report["probes"] = probes
        report["summary"]["probes_passed"] = sum(
            1 for p in probes if p["passed"])
        report["summary"]["probes_total"] = len(probes)
        ok = ok and all(p["passed"] for p in probes)
        out = os.path.join(root, "outputs", "report.json")
        _write_json(out, report)
        print("demo report written to %s (ok=%s)" % (out, ok))
        print(json.dumps(report["summary"], ensure_ascii=True, indent=2))
        return 0 if ok else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
