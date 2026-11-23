"""引擎编排: 拓扑加载、事件摄取、降噪+关联、补丁增量失效、崩溃恢复。

引擎不持有决定状态的内存残留: 每次运行都从持久化状态
(拓扑版本 / 事件水位 / 已提交簇 / 窗口) 出发裁决。
"""

import json

from . import errors as E
from .correlate import correlate_approx, correlate_exact
from .denoise import compute_clusters
from .model import canonical_json, sha256_text
from .patches import apply_patch_to_topology
from .scale import ScaleConfig, decide_modes, estimate_approx_quality
from .store import Store
from .validation import validate_events_list, validate_topology_dict


class Engine:
    def __init__(self, db_path, cfg=None, lock=None):
        self.store = Store(db_path)
        self.cfg = cfg or ScaleConfig()
        self.lock = lock

    def close(self):
        self.store.close()

    def _locked(self):
        if self.lock is None:
            return _NullCtx()
        return self.lock

    def _topology(self):
        raw = self.store.get_meta("topology")
        if raw is None:
            raise E.BusinessError(E.E1008_BAD_TOPOLOGY_SHAPE,
                                  "no topology loaded")
        return validate_topology_dict(json.loads(raw))

    def load_topology(self, topo_dict):
        """加载(或整体替换)拓扑。版本回退返回 E3003, 非法输入不落库。"""
        with self._locked():
            topo = validate_topology_dict(topo_dict)
            current = self.store.get_meta("topology_version")
            if current is not None and topo.version <= int(current):
                raise E.BusinessError(
                    E.E3003_TOPOLOGY_VERSION_ROLLBACK,
                    "topology version rollback rejected",
                    incoming_version=topo.version,
                    current_version=int(current))
            commit_id = "topology-v%d" % topo.version
            if self.store.begin_commit(commit_id, "topology"):
                self.store.finalize_commit(
                    commit_id, version=topo.version,
                    meta_updates={
                        "topology": canonical_json(topo_dict),
                        "topology_version": topo.version,
                    })
            return topo.version

    def ingest(self, raw_events, batch_size=200):
        """校验并摄取事件流。整批先校验, 任何非法事件都不落库。"""
        with self._locked():
            topo = self._topology()
            events = validate_events_list(raw_events, topo)
            existing = {ev.seq for ev in self.store.load_events()}
            fresh = [ev for ev in events if ev.seq not in existing]
            for i in range(0, len(fresh), batch_size):
                self.store.insert_events(fresh[i:i + batch_size])
            watermark = max([ev.seq for ev in events], default=0)
            old_wm = int(self.store.get_meta("event_watermark", "0"))
            if watermark > old_wm:
                self.store.set_meta_autocommit("event_watermark", watermark)
            return {"ingested": len(fresh), "watermark": watermark}

    def run(self):
        """全量重算当前拓扑版本下的簇与根因候选并原子提交。"""
        with self._locked():
            topo = self._topology()
            events = self.store.load_events()
            commit_id = "results-v%d-e%d" % (topo.version, len(events))
            if not self.store.begin_commit(commit_id, "results"):
                return self.dump()
            clusters, candidates, decisions = self._compute_all(topo, events)
            self.store.stage_clusters(clusters)
            self.store.stage_candidates(candidates)
            self.store.stage_decisions(decisions)
            self.store.finalize_commit(
                commit_id, version=topo.version,
                meta_updates={"results_version": topo.version})
            return self.dump()

    def _compute_all(self, topo, events):
        cfg = self.cfg
        windows = list(topo.windows.values())
        clusters = compute_clusters(events, topo, windows,
                                    cfg.cluster_window_ms)
        decisions = decide_modes(topo, events, cfg)
        candidates = {}
        for domain, decision in decisions.items():
            domain_clusters = [cid for cid, c in clusters.items()
                               if topo.services[c["service"]].fault_domain
                               == domain]
            if decision["mode"] == "exact":
                part = correlate_exact(clusters, topo, cfg.causal_window_ms,
                                       cfg.tolerance,
                                       cluster_ids=domain_clusters)
            else:
                part, stats = correlate_approx(
                    clusters, topo, cfg.causal_window_ms, cfg.tolerance,
                    sample_mod=cfg.approx_sample_mod,
                    cluster_ids=domain_clusters)
                quality = estimate_approx_quality(clusters, topo, cfg, part, stats)
                decision["approx_stats"] = stats
                decision["recall_estimate"] = quality["recall_at_k"]
                decision["error_bound"] = quality["error_bound"]
                decision["error_bound_definition"] = quality[
                    "error_bound_definition"]
            candidates.update(part)
        return clusters, candidates, decisions

    def apply_patch(self, patch):
        """应用增量补丁: 精确失效闭包 + 按需重算, 未受影响结论逐字复用。"""
        with self._locked():
            commit_id = "patch-%s" % patch["patch_id"]
            if self.store.commit_status(commit_id) == "committed":
                report = json.loads(self.store.get_meta(
                    "patch_report:%s" % patch["patch_id"]))
                report["idempotent_replay"] = True
                return report
            topo = self._topology()
            new_topo, affected, sources = apply_patch_to_topology(topo, patch)
            self.store.begin_commit(commit_id, "patch")

            events = self.store.load_events()
            old_version = topo.version
            old_clusters = self.store.load_clusters(old_version)
            old_candidates = self.store.load_candidates(old_version)

            cfg = self.cfg
            windows = list(new_topo.windows.values())
            recomputed_clusters = compute_clusters(
                events, new_topo, windows, cfg.cluster_window_ms,
                services=set(affected))
            new_clusters = {cid: c for cid, c in old_clusters.items()
                            if c["service"] not in affected}
            new_clusters.update(recomputed_clusters)

            closure_cache = {n: new_topo.upstream_closure(n)
                             for n in new_topo.services}
            cand_recompute = set()
            for cid, c in new_clusters.items():
                if c["status"] != "active":
                    continue
                if set(closure_cache[c["service"]]) & set(affected):
                    cand_recompute.add(cid)
            decisions = decide_modes(new_topo, events, cfg)
            new_candidates = {cid: cands for cid, cands in
                              old_candidates.items()
                              if cid not in cand_recompute
                              and cid in new_clusters
                              and new_clusters[cid]["status"] == "active"}
            exact_ids = [cid for cid in sorted(cand_recompute)
                         if decisions[new_topo.services[
                             new_clusters[cid]["service"]]
                             .fault_domain]["mode"] == "exact"]
            approx_ids = [cid for cid in sorted(cand_recompute)
                          if cid not in set(exact_ids)]
            if exact_ids:
                new_candidates.update(correlate_exact(
                    new_clusters, new_topo, cfg.causal_window_ms,
                    cfg.tolerance, cluster_ids=exact_ids))
            if approx_ids:
                part, _stats = correlate_approx(
                    new_clusters, new_topo, cfg.causal_window_ms,
                    cfg.tolerance, sample_mod=cfg.approx_sample_mod,
                    cluster_ids=approx_ids)
                new_candidates.update(part)

            invalidations = []
            for svc in sorted(affected):
                invalidations.append({
                    "item": "service:%s" % svc,
                    "layer": affected[svc],
                    "source": ";".join(sources)})
            for cid in sorted(recomputed_clusters):
                invalidations.append({
                    "item": "cluster:%s" % cid,
                    "layer": affected.get(new_clusters[cid]["service"], 0),
                    "source": ";".join(sources)})
            for cid in sorted(cand_recompute):
                invalidations.append({
                    "item": "candidates:%s" % cid,
                    "layer": affected.get(new_clusters[cid]["service"], 0),
                    "source": ";".join(sources)})

            report = {
                "patch_id": patch["patch_id"],
                "from_version": old_version,
                "to_version": new_topo.version,
                "affected_services": {s: affected[s]
                                      for s in sorted(affected)},
                "sources": sources,
                "recomputed_clusters": len(recomputed_clusters),
                "reused_clusters": len(new_clusters)
                - len(recomputed_clusters),
                "recomputed_candidates": len(cand_recompute),
                "reused_candidates": len(new_candidates)
                - len(cand_recompute),
                "invalidated": invalidations,
                "idempotent_replay": False,
            }

            self.store.stage_clusters(new_clusters)
            self.store.stage_candidates(new_candidates)
            self.store.stage_decisions(decisions)
            self.store.stage_invalidations(invalidations)
            topo_dict = json.loads(canonical_json(_topology_to_dict(new_topo)))
            self.store.finalize_commit(
                commit_id, version=new_topo.version,
                meta_updates={
                    "topology": canonical_json(topo_dict),
                    "topology_version": new_topo.version,
                    "results_version": new_topo.version,
                    "patch_report:%s" % patch["patch_id"]:
                        canonical_json(report),
                })
            return report

    def recover(self):
        """识别半提交并清理, 返回半提交清单。之后重放未完成部分即可。"""
        pending = self.store.pending_commits()
        for p in pending:
            self.store.abort_commit(p["commit_id"])
        return pending

    def dump(self):
        version = int(self.store.get_meta("results_version", "0"))
        out = {
            "topology_version": int(
                self.store.get_meta("topology_version", "0")),
            "results_version": version,
            "event_watermark": int(
                self.store.get_meta("event_watermark", "0")),
            "clusters": self.store.load_clusters(version),
            "candidates": self.store.load_candidates(version),
            "decisions": self.store.load_decisions(version),
            "invalidations": self.store.load_invalidations(version),
        }
        out["sha"] = sha256_text(canonical_json(
            {k: v for k, v in out.items() if k != "sha"}))
        return out


def _topology_to_dict(topo):
    return {
        "version": topo.version,
        "services": [
            {"name": s.name, "fault_domain": s.fault_domain,
             "upstream": list(s.upstream),
             "instances": [
                 {"id": iid,
                  "weight": topo.instances[iid].weight_micro / 1_000_000}
                 for iid in s.instances]}
            for s in (topo.services[n] for n in sorted(topo.services))],
        "windows": [
            {"id": w.window_id, "kind": w.kind, "service": w.service,
             "instance_id": w.instance_id, "start_ms": w.start_ms,
             "end_ms": w.end_ms}
            for w in (topo.windows[i] for i in sorted(topo.windows))],
    }


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
