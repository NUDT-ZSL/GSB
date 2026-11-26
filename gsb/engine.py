"""用例编排：load / merge / judge / resume / status / verify / patch / replace。"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .canon import digest
from .config import EngineConfig
from .delay import compute_delays
from .errors import Code, GsbError
from .judge import adjudicate
from .locking import file_lock
from .merge import merge_policies
from .model import Bundle, load_bundle
from .patch import (
    affected_closure,
    apply_patch,
    diff_snapshot_nodes,
    parse_version,
    snapshot_node_to_merged,
    validate_snapshot_doc,
)
from .store import Store


def new_run_id(worker: str) -> str:
    return f"{worker}-p{os.getpid()}-{uuid.uuid4().hex[:8]}"


class Engine:
    def __init__(
        self,
        samples_dir: str | Path = "samples",
        state_dir: str | Path = "state",
        now_override: Optional[int] = None,
        worker_id: Optional[str] = None,
    ) -> None:
        self.samples_dir = Path(samples_dir)
        self.state_dir = Path(state_dir)
        self.now_override = now_override
        self.worker_id = worker_id or f"w-{os.getpid()}"
        self.store = Store(self.state_dir)

    def close(self) -> None:
        self.store.close()

    # ----- shared --------------------------------------------------------
    def load_inputs(self) -> tuple[Bundle, EngineConfig]:
        bundle = load_bundle(self.samples_dir)
        cfg = EngineConfig.from_bundle(bundle, self.now_override)
        return bundle, cfg

    def _active_rev_or_raise(self, rev_no: Optional[int]) -> int:
        if rev_no is not None:
            if self.store.get_revision(rev_no) is None:
                raise GsbError(Code.E_NOT_FOUND, f"policy revision {rev_no} not found")
            return rev_no
        active = self.store.active_rev_no()
        if active is None:
            raise GsbError(Code.E_STATE, "no merged policy revision; run load then merge")
        return active

    # ----- load ----------------------------------------------------------
    def cmd_load(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        run_id = run_id or new_run_id("load")
        bundle, cfg = self.load_inputs()
        st = self.store
        with st.immediate():
            st.save_source("master_stream.json", "master", bundle.digests["master_stream"],
                           bundle.master_raw, cfg.now_ms)
            st.save_source("replica_stream.json", "replica", bundle.digests["replica_stream"],
                           bundle.replica_raw, cfg.now_ms)
            st.save_source("policy_v1.json", "policy_v1", bundle.digests["policy_v1"],
                           bundle.v1_raw, cfg.now_ms)
            st.save_source("policy_v2/meta.sqlite", "policy_v2", bundle.digests["policy_v2_meta"],
                           bundle.v2_meta_raw, cfg.now_ms)

            max_txn = max(t.txn_id for t in bundle.transactions)
            latest_commit = max(t.commit_clock_ms for t in bundle.transactions)
            st.upsert_watermark("master", "", max_txn, latest_commit,
                                {"segment_count": len(bundle.segments),
                                 "segments": bundle.segment_ids}, run_id)
            last_by_inst: Dict[str, Any] = {}
            for rec in bundle.replica_records:
                last_by_inst[rec.instance_id] = rec
            for iid, rec in sorted(last_by_inst.items()):
                st.upsert_watermark("replica", iid, rec.txn_id, rec.replay_clock_ms,
                                   {"replay_id": rec.replay_id, "tombstone": rec.tombstone}, run_id)
            st.meta_set_locked("loaded_run_id", run_id)
            st.meta_set_locked("clock_watermark_ms", str(cfg.now_ms))
        return {
            "command": "load",
            "ok": True,
            "run_id": run_id,
            "now_ms": cfg.now_ms,
            "digests": bundle.digests,
            "master": {"txn_count": len(bundle.transactions),
                        "segments": bundle.segment_ids, "max_txn_id": max_txn},
            "replica": {"instance_count": len(bundle.replica_instances),
                        "record_count": len(bundle.replica_records)},
            "v2_nodes": len(bundle.v2_nodes),
            "checkpoints": [c.checkpoint_id for c in bundle.checkpoints],
        }

    # ----- merge ---------------------------------------------------------
    def cmd_merge(self, run_id: Optional[str] = None, crash_phase: Optional[str] = None) -> Dict[str, Any]:
        run_id = run_id or new_run_id("merge")
        bundle, cfg = self.load_inputs()
        merged = merge_policies(bundle, cfg)
        st = self.store
        item_key = "merge:1"

        existing = st.get_revision(1)
        if existing is not None and existing.get("kind") == "initial_merge":
            pending = [p for p in st.pending_journal() if p["item_key"] == item_key]
            if pending:
                with st.immediate():
                    st.conn.execute(
                        "UPDATE commit_journal SET state='committed' WHERE item_key=?", (item_key,))
            active = st.active_rev_no()
            return {"command": "merge", "ok": True, "idempotent": True, "rev_no": 1,
                    "active_rev_no": active, "basis_count": merged["basis_count"],
                    "node_count": len(merged["nodes"]),
                    "content_digest": existing["content_digest"]}

        st.journal_start(item_key, run_id, "merge", merged["content_digest"], cfg.now_ms)
        if crash_phase == "merge":
            os._exit(137)

        status, inserted = st.insert_revision(
            rev_no=1, version="1.0.0",
            generation=max((n.get("generation") or 0) for n in merged["nodes"]),
            snapshot_id="merged-initial", idempotency_key="initial-merge-v1",
            content_digest=merged["content_digest"], signature_set=[],
            kind="initial_merge", payload=merged, now_ms=cfg.now_ms,
        )
        st.save_instance_deadlines(1, merged["nodes"])
        with st.immediate():
            st.conn.execute(
                "UPDATE commit_journal SET state='committed' WHERE item_key=?", (item_key,))
            st.meta_set_locked("clock_watermark_ms", str(cfg.now_ms))
        return {"command": "merge", "ok": True, "idempotent": status == "idempotent",
                "rev_no": 1, "active_rev_no": st.active_rev_no(),
                "basis_count": merged["basis_count"], "node_count": len(merged["nodes"]),
                "content_digest": merged["content_digest"]}

    # ----- judge ---------------------------------------------------------
    def _adjudicate_active(self, rev_no: int) -> tuple[Bundle, EngineConfig, Dict[str, Any], Dict[str, Any]]:
        bundle, cfg = self.load_inputs()
        rev = self.store.get_revision(rev_no)
        if rev is None:
            raise GsbError(Code.E_NOT_FOUND, f"revision {rev_no} missing")
        merged = rev["payload"]
        delays = compute_delays(bundle, cfg)
        verdict = adjudicate(bundle, merged, cfg, delays)
        return bundle, cfg, merged, verdict

    def cmd_judge(
        self,
        segment_ids: Optional[List[int]] = None,
        rev_no: Optional[int] = None,
        worker_id: Optional[str] = None,
        run_id: Optional[str] = None,
        crash_after_commits: Optional[int] = None,
        crash_phase: Optional[str] = None,
        slow_ms: int = 0,
    ) -> Dict[str, Any]:
        worker = worker_id or self.worker_id
        run_id = run_id or new_run_id(worker)
        rev = self._active_rev_or_raise(rev_no)
        bundle, cfg, merged, verdict = self._adjudicate_active(rev)
        st = self.store

        st.save_delay_evidence(rev, verdict["delays"])
        if crash_phase == "delay":
            os._exit(137)

        seg_map = {s["segment_id"]: s for s in verdict["segments"]}
        all_sids = [s.segment_id for s in bundle.segments]
        if segment_ids:
            targets = sorted(set(int(x) for x in segment_ids))
            unknown = [x for x in targets if x not in seg_map]
            if unknown:
                raise GsbError(Code.E_NOT_FOUND, f"unknown segment ids: {unknown}")
        else:
            targets = all_sids

        committed: List[int] = []
        idempotent: List[int] = []
        stolen: List[int] = []
        commits = 0

        for sid in targets:
            existing = st.get_decision(rev, sid)
            if existing is not None:
                idempotent.append(sid)
                continue

            if crash_after_commits is not None and commits >= crash_after_commits:
                key = Store.segment_item_key(rev, sid)
                st.journal_start(key, run_id, "segment",
                                 seg_map[sid]["decision_digest"], cfg.now_ms)
                os._exit(137)

            lock_path = st.locks_dir / f"seg-r{rev}-s{sid}.lock"
            with file_lock(lock_path):
                # 拿锁后二次检查，保证跨进程只提交一次
                existing = st.get_decision(rev, sid)
                if existing is not None:
                    idempotent.append(sid)
                    continue
                key = Store.segment_item_key(rev, sid)
                seg_result = seg_map[sid]
                st.journal_start(key, run_id, "segment", seg_result["decision_digest"], cfg.now_ms)
                claim_state = st.claim_segment(rev, sid, worker, run_id, cfg.now_ms)
                if claim_state == "stolen":
                    stolen.append(sid)
                if slow_ms:
                    import time
                    time.sleep(slow_ms / 1000.0)

                precision_for_seg = [
                    p for p in verdict["precision_sensitive"] if sid in p["segments"]
                ]
                conflicts_for_seg = [
                    c for c in verdict["conflicts"] if c["segment_id"] == sid
                ]
                state = st.commit_segment_result(
                    rev_no=rev, sid=sid, status=seg_result["status"],
                    error_code=seg_result.get("error_code"),
                    decision_digest=seg_result["decision_digest"],
                    payload=seg_result, precision=precision_for_seg,
                    conflicts=conflicts_for_seg, worker=worker, run_id=run_id,
                    now_ms=cfg.now_ms,
                )
                if state == "committed":
                    committed.append(sid)
                    commits += 1
                else:
                    idempotent.append(sid)

        return {
            "command": "judge", "ok": True, "run_id": run_id, "worker_id": worker,
            "rev_no": rev, "targets": targets, "committed": sorted(committed),
            "idempotent": sorted(idempotent), "stolen_claims": sorted(stolen),
            "summary": verdict["summary"],
        }

    # ----- resume --------------------------------------------------------
    def cmd_resume(self, worker_id: Optional[str] = None,
                   run_id: Optional[str] = None) -> Dict[str, Any]:
        worker = worker_id or f"resume-{os.getpid()}"
        run_id = run_id or new_run_id(worker)
        st = self.store
        bundle, cfg = self.load_inputs()

        # 合并阶段崩溃：存在 pending merge 日志或缺少初始修订时，先重放 merge
        pending_before = st.pending_journal()
        merge_pending = [p for p in pending_before if p["item_key"].startswith("merge:")]
        merge_report = None
        if merge_pending or st.get_revision(1) is None or st.active_rev_no() is None:
            merge_report = self.cmd_merge(run_id=run_id + "-merge")

        rev = self._active_rev_or_raise(None)
        all_sids = [s.segment_id for s in bundle.segments]

        pending = st.pending_journal()
        half_committed: List[Dict[str, Any]] = []
        reconciled: List[str] = []
        for item in pending:
            key = item["item_key"]
            if key.startswith("merge:"):
                rev1 = st.get_revision(1)
                if rev1 is not None:
                    with st.immediate():
                        st.conn.execute(
                            "UPDATE commit_journal SET state='committed' WHERE item_key=?", (key,))
                    reconciled.append(key)
                continue
            if key.startswith("seg:"):
                parts = key.split(":")
                item_rev, sid = int(parts[1]), int(parts[2])
                decision = st.get_decision(item_rev, sid)
                if decision is not None:
                    with st.immediate():
                        st.conn.execute(
                            "UPDATE commit_journal SET state='committed' WHERE item_key=?", (key,))
                    reconciled.append(key)
                else:
                    half_committed.append({"item_key": key, "rev_no": item_rev,
                                           "segment_id": sid, "prior_run": item["run_id"],
                                           "attempts": item["attempts"]})

        decided = {d["segment_id"] for d in st.list_decisions(rev)}
        missing = [sid for sid in all_sids if sid not in decided]
        replay_sids = sorted(set(missing) | {h["segment_id"] for h in half_committed
                                              if h["rev_no"] == rev})

        judge_summary = None
        if replay_sids:
            judge_summary = self.cmd_judge(
                segment_ids=replay_sids, rev_no=rev, worker_id=worker,
                run_id=run_id + "-judge",
            )
        report = {
            "command": "resume", "ok": True, "run_id": run_id, "rev_no": rev,
            "merge_replay": merge_report,
            "half_committed": half_committed, "reconciled_committed_journal": reconciled,
            "missing_segments": missing, "replayed_segments": replay_sids,
            "judge": judge_summary,
        }
        st.record_recovery(run_id, cfg.now_ms, report)
        return report

    # ----- status --------------------------------------------------------
    def cmd_status(self, rev_no: Optional[int] = None) -> Dict[str, Any]:
        st = self.store
        rev = rev_no or st.active_rev_no()
        revision = st.get_revision(rev) if rev is not None else None
        decisions = st.list_decisions(rev) if rev is not None else []
        delays = st.list_delay_evidence(rev) if rev is not None else {}
        precision = st.list_precision(rev) if rev is not None else []
        conflicts = st.list_conflicts(rev) if rev is not None else []

        seg_status = []
        for d in sorted(decisions, key=lambda x: x["segment_id"]):
            p = d["payload"]
            seg_status.append({
                "segment_id": d["segment_id"],
                "status": d["status"],
                "error_code": d["error_code"],
                "reasons": p.get("reasons", []),
                "watermark_deadline_ms": p.get("watermark_deadline_ms"),
                "watermark_instance": p.get("watermark_instance"),
                "archive_gate_ms": p.get("archive_gate_ms"),
                "gate_instance": p.get("gate_instance"),
                "archive_batch_id": p.get("archive_batch_id"),
                "precision_sensitive": p.get("precision_sensitive", False),
                "precision_pair_keys": p.get("precision_pair_keys", []),
                "carried_forward": bool(d["carried"]),
                "source_rev": d["source_rev"],
                "adjudicated_by": d["adjudicated_by"],
                "decision_digest": d["decision_digest"],
            })

        delay_summary = {}
        for iid in sorted(delays):
            e = delays[iid]
            delay_summary[iid] = {
                "delay_exact_ms": e.get("delay_exact_ms"),
                "delay_fixed_units": e.get("delay_fixed_units"),
                "delay_fixed_ms": e.get("delay_fixed_ms"),
                "carried_remainder_ms": e.get("carried_remainder_ms"),
                "scale": e.get("scale"),
                "last_delay_ms": e.get("last_delay_ms"),
                "lag_alert": e.get("lag_alert"),
                "last_replayed_txn_id": e.get("last_replayed_txn_id"),
            }

        return {
            "ok": True,
            "command": "status",
            "state_db": str(st.db_path),
            "active_rev_no": st.active_rev_no(),
            "requested_rev_no": rev,
            "revision": None if revision is None else {
                "rev_no": revision["rev_no"], "version": revision["version"],
                "generation": revision["generation"], "kind": revision["kind"],
                "content_digest": revision["content_digest"],
            },
            "clock_watermark_ms": st.meta_get("clock_watermark_ms"),
            "segments": seg_status,
            "delays": delay_summary,
            "precision_sensitive": sorted(
                [p for p in precision if p.get("precision_sensitive")],
                key=lambda p: p["pair_key"]),
            "conflicts": conflicts,
            "watermarks": st.list_watermarks(),
            "pending_journal": st.pending_journal(),
        }

    # ----- verify --------------------------------------------------------
    def cmd_verify(self, rev_no: Optional[int] = None) -> Dict[str, Any]:
        st = self.store
        rev = self._active_rev_or_raise(rev_no)
        bundle, cfg, merged, verdict = self._adjudicate_active(rev)
        fresh = {s["segment_id"]: s for s in verdict["segments"]}
        stored = {d["segment_id"]: d for d in st.list_decisions(rev)}
        all_sids = [s.segment_id for s in bundle.segments]

        checks: List[Dict[str, Any]] = []

        def check(name: str, ok: bool, detail: Any = None) -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        check("sqlite_integrity", st.integrity_check() == "ok", st.integrity_check())
        check("all_segments_decided", set(stored) == set(all_sids),
              {"decided": sorted(stored), "expected": all_sids})
        digest_mismatch = []
        status_mismatch = []
        for sid in all_sids:
            d = stored.get(sid)
            f = fresh.get(sid)
            if d is None or f is None:
                continue
            if d["decision_digest"] != f["decision_digest"]:
                digest_mismatch.append(sid)
            if d["status"] != f["status"]:
                status_mismatch.append(sid)
        check("decision_digests_match_deterministic_recompute", not digest_mismatch,
              {"mismatch": digest_mismatch})
        check("statuses_match_recompute", not status_mismatch, {"mismatch": status_mismatch})

        pending_rev = [p["item_key"] for p in st.pending_journal()
                       if p["item_key"].startswith(f"seg:{rev}:")]
        check("no_pending_journal_for_rev", not pending_rev, {"pending": pending_rev})

        dup = st.conn.execute(
            "SELECT rev_no,segment_id,COUNT(*) c FROM segment_decisions "
            "WHERE rev_no=? GROUP BY rev_no,segment_id HAVING c>1", (rev,)).fetchall()
        check("exactly_one_decision_per_segment", not dup, {"duplicates": [dict(r) for r in dup]})

        delays = st.list_delay_evidence(rev)
        check("delay_evidence_persisted_for_all_instances",
              len(delays) == len(bundle.replica_instances),
              {"persisted": len(delays), "instances": len(bundle.replica_instances)})

        revs = st.list_revisions()
        rev_nos = [r["rev_no"] for r in revs]
        check("revisions_monotonic", rev_nos == sorted(rev_nos), {"rev_nos": rev_nos})

        ok = all(c["ok"] for c in checks)
        return {"command": "verify", "ok": ok, "rev_no": rev, "checks": checks,
                "summary": verdict["summary"]}

    # ----- patch ---------------------------------------------------------
    def cmd_patch(self, patch_path: str | Path, run_id: Optional[str] = None) -> Dict[str, Any]:
        run_id = run_id or new_run_id("patch")
        st = self.store
        active = self._active_rev_or_raise(None)
        bundle, cfg = self.load_inputs()
        old_rev = st.get_revision(active)
        old_merged = old_rev["payload"]
        patch_doc = _read_json(Path(patch_path))

        idem = patch_doc.get("idempotency_key")
        if idem:
            existed = st.conn.execute(
                "SELECT rev_no FROM policy_revisions WHERE idempotency_key=?", (idem,)).fetchone()
            if existed:
                return {"command": "patch", "ok": True, "idempotent": True,
                        "rev_no": existed["rev_no"], "patch_id": patch_doc.get("patch_id")}

        new_merged, changed_instances = apply_patch(old_merged, patch_doc)
        old_precision = st.list_precision(active)
        closure = affected_closure(
            changed_instances=changed_instances, new_merged=new_merged,
            old_merged=old_merged, segment_ids=bundle.segment_ids,
            old_precision=old_precision,
        )
        new_rev_no = st.next_rev_no()
        version = f"{old_rev['version']}+p{new_rev_no}"
        st.insert_revision(
            rev_no=new_rev_no, version=version,
            generation=(old_rev.get("generation") or 0) + 1,
            snapshot_id=patch_doc.get("patch_id", f"patch-{new_rev_no}"),
            idempotency_key=idem or f"patch-run-{run_id}",
            content_digest=new_merged["content_digest"], signature_set=[],
            kind="patch", payload=new_merged, now_ms=cfg.now_ms,
        )
        st.save_instance_deadlines(new_rev_no, new_merged["nodes"])
        carry = self._carry_forward(active, new_rev_no, closure["reusable_segments"], cfg.now_ms)
        return {"command": "patch", "ok": True, "idempotent": False,
                "base_rev_no": active, "rev_no": new_rev_no,
                "changed_instances": changed_instances, "closure": closure,
                "carried_decisions": carry["decisions"],
                "carried_precision_pairs": carry["precision"],
                "carried_conflicts": carry["conflicts"]}

    # ----- snapshot replacement -----------------------------------------
    def cmd_replace(self, snapshot_path: str | Path) -> Dict[str, Any]:
        st = self.store
        active = self._active_rev_or_raise(None)
        bundle, cfg = self.load_inputs()
        doc = validate_snapshot_doc(_read_json(Path(snapshot_path)))

        idem = doc["idempotency_key"]
        existing = st.conn.execute(
            "SELECT rev_no FROM policy_revisions WHERE idempotency_key=?", (idem,)).fetchone()
        if existing:
            return {"command": "replace", "ok": True, "idempotent": True,
                    "rev_no": existing["rev_no"], "idempotency_key": idem}
        same_digest = st.conn.execute(
            "SELECT rev_no FROM policy_revisions WHERE content_digest=?",
            (doc["content_digest"],)).fetchone()
        if same_digest:
            return {"command": "replace", "ok": True, "idempotent": True,
                    "rev_no": same_digest["rev_no"], "reason": "same_content_digest"}

        old_rev = st.get_revision(active)
        old_version = parse_version(old_rev["version"])
        new_version = parse_version(doc["version"])
        if new_version < old_version or (
            new_version == old_version and int(doc["generation"]) < int(old_rev.get("generation") or 0)
        ):
            raise GsbError(
                Code.E_VERSION_ROLLBACK,
                "policy snapshot version rollback rejected",
                {"active_version": old_rev["version"],
                 "active_generation": old_rev.get("generation"),
                 "incoming_version": doc["version"],
                 "incoming_generation": doc["generation"]},
            )

        merged_nodes = [snapshot_node_to_merged(n, origin="snapshot") for n in doc["nodes"]]
        new_merged = {
            "merge_version": 1, "injected_now_ms": cfg.now_ms,
            "nodes": merged_nodes,
            "content_digest": digest({"nodes": merged_nodes, "now": cfg.now_ms}),
            "replaced_by": doc["snapshot_id"],
        }
        changed_instances, touched = diff_snapshot_nodes(old_rev["payload"], doc["nodes"])
        old_precision = st.list_precision(active)
        closure = affected_closure(
            changed_instances=changed_instances, new_merged=new_merged,
            old_merged=old_rev["payload"], segment_ids=bundle.segment_ids,
            old_precision=old_precision,
        )
        new_rev_no = st.next_rev_no()
        st.insert_revision(
            rev_no=new_rev_no, version=doc["version"], generation=doc["generation"],
            snapshot_id=doc["snapshot_id"], idempotency_key=idem,
            content_digest=doc["content_digest"], signature_set=doc["signatures"],
            kind="full_snapshot_replacement", payload=new_merged, now_ms=cfg.now_ms,
        )
        st.save_instance_deadlines(new_rev_no, merged_nodes)
        carry = self._carry_forward(active, new_rev_no, closure["reusable_segments"], cfg.now_ms)
        return {"command": "replace", "ok": True, "idempotent": False,
                "base_rev_no": active, "rev_no": new_rev_no,
                "version": doc["version"], "generation": doc["generation"],
                "changed_instances": changed_instances,
                "touched_segments": sorted(touched), "closure": closure,
                "carried_decisions": carry["decisions"]}

    def _carry_forward(self, old_rev: int, new_rev: int, reusable: List[int],
                       now_ms: int) -> Dict[str, Any]:
        st = self.store
        reusable_set = set(reusable)
        carried_decisions: List[int] = []
        for d in st.list_decisions(old_rev):
            if d["segment_id"] in reusable_set:
                st.carry_decision(target_rev=new_rev, source=d,
                                  source_rev=old_rev, now_ms=now_ms)
                carried_decisions.append(d["segment_id"])

        carried_precision: List[str] = []
        for p in st.list_precision(old_rev):
            a, b = p.get("segments", [None, None])
            if a in reusable_set and b in reusable_set:
                st.conn.execute(
                    "INSERT OR IGNORE INTO precision_records(rev_no,pair_key,digest,payload_json) "
                    "SELECT ?,pair_key,digest,payload_json FROM precision_records "
                    "WHERE rev_no=? AND pair_key=?",
                    (new_rev, old_rev, p["pair_key"]),
                )
                carried_precision.append(p["pair_key"])

        carried_conflicts: List[int] = []
        for c in st.list_conflicts(old_rev):
            if c["segment_id"] in reusable_set:
                st.conn.execute(
                    "INSERT OR IGNORE INTO conflict_records(rev_no,conflict_key,segment_id,code,"
                    "payload_json) SELECT ?,conflict_key,segment_id,code,payload_json "
                    "FROM conflict_records WHERE rev_no=? AND conflict_key=?",
                    (new_rev, old_rev, f"seg:{c['segment_id']}"),
                )
                carried_conflicts.append(c["segment_id"])
        return {"decisions": sorted(carried_decisions),
                "precision": sorted(carried_precision),
                "conflicts": sorted(carried_conflicts)}


def _read_json(path: Path) -> Dict[str, Any]:
    import json
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GsbError(Code.E_NOT_FOUND, f"file not found: {path}")
    except json.JSONDecodeError as exc:
        raise GsbError(Code.E_BAD_POLICY, f"invalid JSON {path}: {exc}")


def judge_process_entry(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """multiprocessing 工作进程入口（参数均可 pickle）。"""
    eng = Engine(kwargs["samples_dir"], kwargs["state_dir"],
                 now_override=kwargs.get("now_ms"),
                 worker_id=kwargs.get("worker_id"))
    try:
        return eng.cmd_judge(
            segment_ids=kwargs.get("segment_ids"),
            rev_no=kwargs.get("rev_no"),
            worker_id=kwargs.get("worker_id"),
            run_id=kwargs.get("run_id"),
        )
    finally:
        eng.close()
