"""最小追赶余量、精度敏感识别与日志段归档裁决（纯计算、确定性）。"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from .canon import digest
from .config import EngineConfig
from .delay import compute_delays, last_txn_by_instance, segment_backlog_bytes
from .errors import Code
from .fixedpoint import derive_margin
from .model import Bundle


def _instance_rate(bundle: Bundle, iid: str, cfg: EngineConfig) -> Decimal:
    for inst in bundle.replica_instances:
        if inst.get("instance_id") == iid and inst.get("rate_bytes_per_ms") is not None:
            return Decimal(str(inst["rate_bytes_per_ms"]))
    return cfg.rate_bytes_per_ms


def _contribution(
    bundle: Bundle, cfg: EngineConfig, iid: str, deadline_ms: int,
    last_txn: Optional[int], segment_id: int,
) -> Dict[str, Any]:
    backlog = segment_backlog_bytes(bundle, last_txn, segment_id)
    rate = _instance_rate(bundle, iid, cfg)
    margin = derive_margin(backlog, rate, cfg.safety_factor, cfg.slack_ms, cfg.margin_scale)
    fine_scale = cfg.margin_scale.finer(cfg.fine_factor)
    margin_fine = derive_margin(backlog, rate, cfg.safety_factor, cfg.slack_ms, fine_scale)
    gate = Decimal(int(deadline_ms)) + margin.margin_ms
    gate_fine = Decimal(int(deadline_ms)) + margin_fine.margin_ms
    return {
        "instance_id": iid,
        "retention_deadline_ms": int(deadline_ms),
        "backlog_bytes": backlog,
        "margin": margin.evidence(),
        "margin_fine": margin_fine.evidence(),
        "archive_gate_ms": str(gate),
        "archive_gate_fine_ms": str(gate_fine),
        "gate_error_bound_ms": str(margin.error_bound_ms),
    }


def _decision_core(seg_result: Dict[str, Any]) -> Dict[str, Any]:
    """参与摘要的裁决内核（不含 worker/run 等运行期元数据）。"""
    return {
        "segment_id": seg_result["segment_id"],
        "status": seg_result["status"],
        "standalone_status": seg_result["standalone_status"],
        "error_code": seg_result.get("error_code"),
        "reasons": seg_result["reasons"],
        "watermark_deadline_ms": seg_result.get("watermark_deadline_ms"),
        "watermark_instance": seg_result.get("watermark_instance"),
        "archive_gate_ms": seg_result.get("archive_gate_ms"),
        "gate_instance": seg_result.get("gate_instance"),
        "archive_batch_id": seg_result.get("archive_batch_id"),
        "precision_sensitive": seg_result.get("precision_sensitive", False),
        "precision_pair_keys": seg_result.get("precision_pair_keys", []),
        "conflict": seg_result.get("conflict_basis"),
    }


def adjudicate(
    bundle: Bundle,
    merged: Dict[str, Any],
    cfg: EngineConfig,
    delays: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    delays = delays if delays is not None else compute_delays(bundle, cfg)
    watermarks = last_txn_by_instance(bundle)
    nodes = merged["nodes"] if isinstance(merged, dict) and "nodes" in merged else merged
    active = [n for n in nodes if not n.get("tombstone")]

    segments_out: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []

    for seg in bundle.segments:
        sid = seg.segment_id
        refs = [n for n in active if sid in n.get("segment_refs", [])]
        refs.sort(key=lambda n: n["instance_id"])
        excluded = sorted(
            n["instance_id"] for n in nodes
            if n.get("tombstone") and sid in n.get("segment_refs", [])
        )

        result: Dict[str, Any] = {
            "segment_id": sid,
            "status": "retained",
            "standalone_status": "retained",
            "reasons": [],
            "active_instances": [n["instance_id"] for n in refs],
            "tombstoned_instances_excluded": excluded,
            "precision_sensitive": False,
            "precision_pair_keys": [],
        }

        if not refs:
            result["status"] = "archivable"
            result["standalone_status"] = "archivable"
            result["reasons"].append("no_active_retainers")
            segments_out.append(result)
            continue

        contribs: List[Dict[str, Any]] = []
        for node in refs:
            iid = node["instance_id"]
            contribs.append(_contribution(
                bundle, cfg, iid, int(node["retention_deadline_ms"]),
                watermarks.get(iid), sid,
            ))
        contribs.sort(key=lambda c: c["instance_id"])
        result["contributions"] = contribs

        wm = min(contribs, key=lambda c: c["retention_deadline_ms"])
        gate_winner = min(contribs, key=lambda c: Decimal(c["archive_gate_ms"]))
        result["watermark_deadline_ms"] = wm["retention_deadline_ms"]
        result["watermark_instance"] = wm["instance_id"]
        result["archive_gate_ms"] = gate_winner["archive_gate_ms"]
        result["gate_instance"] = gate_winner["instance_id"]
        result["gate_error_bound_ms"] = gate_winner["gate_error_bound_ms"]

        # 矛盾保留需求：同段同时存在 pin 与 expedite 指令
        pins, expedites = [], []
        for node in refs:
            for directive in node.get("directives", []):
                if directive.get("segment_id") != sid:
                    continue
                item = {
                    "instance_id": node["instance_id"],
                    "winner_source": node.get("winner_source"),
                    "version": node.get("version"),
                    "signature": node.get("signature"),
                    "policy_clock_ms": node.get("policy_clock_ms"),
                    "generation": node.get("generation"),
                    "directive": directive,
                }
                if directive.get("action") == "pin":
                    pins.append(item)
                elif directive.get("action") == "expedite":
                    expedites.append(item)

        lag_instances = sorted(
            iid for iid, d in delays.items()
            if d.get("lag_alert") and iid in result["active_instances"]
        )
        result["lag_alert_instances"] = lag_instances

        if pins and expedites:
            basis = {
                "code": Code.E_CONFLICT_POLICY,
                "segment_id": sid,
                "pin": pins,
                "expedite": expedites,
                "message": "contradictory retention requirements: pin vs expedite",
            }
            conflicts.append(basis)
            result["status"] = "alert"
            result["standalone_status"] = "alert"
            result["error_code"] = Code.E_CONFLICT_POLICY
            result["conflict_basis"] = basis
            result["reasons"].append("conflicting_retention_directives")
        elif lag_instances:
            result["status"] = "alert"
            result["standalone_status"] = "alert"
            result["error_code"] = "E_LAG_HIGH"
            result["reasons"].append("replica_replay_delay_over_threshold")
        else:
            gate = Decimal(result["archive_gate_ms"])
            if gate <= cfg.now_ms:
                result["status"] = "archivable"
                result["standalone_status"] = "archivable"
                result["reasons"].append("archive_gate_elapsed")
            else:
                result["status"] = "retained"
                result["standalone_status"] = "retained"
                result["reasons"].append("archive_gate_in_future")

        segments_out.append(result)

    sensitivity = _precision_analysis(segments_out, cfg)
    _apply_batches(segments_out, sensitivity, cfg)

    for seg_result in segments_out:
        seg_result["decision_digest"] = digest(_decision_core(seg_result))

    status_groups: Dict[str, List[int]] = {"archivable": [], "retained": [], "alert": []}
    for s in segments_out:
        status_groups[s["status"]].append(s["segment_id"])

    return {
        "now_ms": cfg.now_ms,
        "tolerance_ms": str(cfg.tolerance_ms),
        "fine_scale_factor": cfg.fine_factor,
        "segments": segments_out,
        "delays": delays,
        "precision_sensitive": sensitivity,
        "conflicts": conflicts,
        "summary": {
            "archivable": sorted(status_groups["archivable"]),
            "retained": sorted(status_groups["retained"]),
            "alert": sorted(status_groups["alert"]),
            "precision_sensitive_pairs": [p["pair_key"] for p in sensitivity if p["precision_sensitive"]],
            "conflict_segments": sorted(c["segment_id"] for c in conflicts),
        },
    }


def _seg_gate(s: Dict[str, Any], fine: bool = False) -> Optional[Decimal]:
    if not s.get("contributions"):
        return None
    if fine:
        return min(Decimal(c["archive_gate_fine_ms"]) for c in s["contributions"])
    return Decimal(s["archive_gate_ms"])


def _seg_error(s: Dict[str, Any], fine: bool = False) -> Decimal:
    if fine:
        winner = min(s["contributions"], key=lambda c: Decimal(c["archive_gate_fine_ms"]))
        scale = Decimal(winner["margin_fine"]["scale"]["unit_ms"])
        # 细标度误差上界：unit*safety + 2*unit（与 derive_margin 递推一致）
        safety = Decimal(winner["margin_fine"]["safety_factor"])
        return scale * safety + Decimal(2) * scale
    return Decimal(s.get("gate_error_bound_ms", "0"))


def _precision_analysis(
    segments: List[Dict[str, Any]], cfg: EngineConfig
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for a, b in zip(segments, segments[1:]):
        sa, sb = a["segment_id"], b["segment_id"]
        ga, gb = _seg_gate(a), _seg_gate(b)
        if ga is None or gb is None:
            continue
        delta = abs(ga - gb)
        err_a = _seg_error(a)
        err_b = _seg_error(b)
        err = err_a + err_b
        linked = delta < cfg.tolerance_ms
        could_cross = (delta - err) < cfg.tolerance_ms
        applicable = a["standalone_status"] in ("archivable", "retained") and \
            b["standalone_status"] in ("archivable", "retained")
        sensitive = bool(applicable and (linked or could_cross))

        gaf, gbf = _seg_gate(a, True), _seg_gate(b, True)
        delta_fine = abs(gaf - gbf)  # type: ignore[operator]
        err_fine = _seg_error(a, True) + _seg_error(b, True)
        linked_fine = delta_fine < cfg.tolerance_ms

        insts = sorted(set(a.get("active_instances", [])) | set(b.get("active_instances", [])))
        chains = []
        for s in (a, b):
            for c in s.get("contributions", []):
                chains.append({
                    "segment_id": s["segment_id"],
                    "instance_id": c["instance_id"],
                    "coarse_error_bound_ms": c["margin"]["error_bound_ms"],
                    "coarse_chain": c["margin"]["error_source_chain"],
                    "fine_error_bound_ms": c["margin_fine"]["error_bound_ms"],
                })

        pair_key = f"{sa}|{sb}"
        rec = {
            "pair_key": pair_key,
            "segments": [sa, sb],
            "participating_instances": insts,
            "precision_sensitive": sensitive,
            "verdict_depends_on_difference": bool(applicable and linked),
            "coarse": {
                "gates_ms": {str(sa): str(ga), str(sb): str(gb)},
                "delta_ms": str(delta),
                "combined_error_bound_ms": str(err),
                "could_cross_tolerance": bool(could_cross),
                "tolerance_ms": str(cfg.tolerance_ms),
                "linked_as_batch": bool(linked),
            },
            "fine_scale": {
                "factor": cfg.fine_factor,
                "gates_ms": {str(sa): str(gaf), str(sb): str(gbf)},
                "delta_ms": str(delta_fine),
                "combined_error_bound_ms": str(err_fine),
                "linked_as_batch": bool(linked_fine),
            },
            "error_source_chain": chains,
        }
        records.append(rec)

        if sensitive:
            a["precision_sensitive"] = True
            b["precision_sensitive"] = True
            a["precision_pair_keys"].append(pair_key)
            b["precision_pair_keys"].append(pair_key)
    return records


def _apply_batches(
    segments: List[Dict[str, Any]],
    sensitivity: List[Dict[str, Any]],
    cfg: EngineConfig,
) -> None:
    """按粗标度 linked 边构造归档批次；批次门限取 max，保守持有可归档段。"""
    by_id = {s["segment_id"]: s for s in segments}
    link_edges = [
        tuple(r["segments"]) for r in sensitivity
        if r["coarse"]["linked_as_batch"]
        and all(by_id[x]["standalone_status"] in ("archivable", "retained") for x in r["segments"])
    ]
    parent = {sid: sid for sid in by_id}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    for x, y in link_edges:
        union(x, y)

    batches: Dict[int, List[int]] = {}
    for sid in by_id:
        batches.setdefault(find(sid), []).append(sid)

    fine_link = {
        tuple(r["segments"]): r["fine_scale"]["linked_as_batch"] for r in sensitivity
    }

    for members in batches.values():
        members.sort()
        if len(members) == 1:
            sid = members[0]
            by_id[sid]["archive_batch_id"] = f"b-{sid}"
            continue
        gates = {sid: _seg_gate(by_id[sid]) for sid in members}
        batch_gate = max(g for g in gates.values() if g is not None)
        batch_id = "b-" + "-".join(str(m) for m in members)
        for sid in members:
            seg = by_id[sid]
            seg["archive_batch_id"] = batch_id
            seg["batch_gate_ms"] = str(batch_gate)
            if batch_gate > cfg.now_ms and seg["standalone_status"] == "archivable":
                seg["status"] = "retained"
                seg["precision_hold"] = True
                if "precision_batch_hold" not in seg["reasons"]:
                    seg["reasons"].append("precision_batch_hold")

    # 回填每个敏感对“提高标度是否翻转裁决”
    for rec in sensitivity:
        a, b = rec["segments"]
        coarse_linked = rec["coarse"]["linked_as_batch"]
        fine_linked = fine_link.get((a, b), coarse_linked)
        coarse_status = {a: by_id[a]["status"], b: by_id[b]["status"]}

        fine_status = {}
        for sid in (a, b):
            seg = by_id[sid]
            if seg["standalone_status"] == "alert":
                fine_status[sid] = "alert"
                continue
            ga = _seg_gate(seg, True)
            # 细标度下若不再相连，按各自门限裁决
            if not fine_linked:
                fine_status[sid] = "archivable" if ga is not None and ga <= cfg.now_ms else "retained"
            else:
                gaf, gbf = _seg_gate(by_id[a], True), _seg_gate(by_id[b], True)
                bg = max(gaf, gbf)  # type: ignore[type-var]
                fine_status[sid] = "archivable" if bg <= cfg.now_ms else "retained"  # type: ignore[operator]

        verdict_flip = fine_status != coarse_status
        link_flip = coarse_linked != fine_linked
        rec["verdict_coarse"] = coarse_status
        rec["verdict_fine_scale"] = fine_status
        if not rec["precision_sensitive"]:
            rec["flip_if_scale_increased"] = None
            rec["flip_reason"] = "pair_not_flagged_precision_sensitive"
        elif link_flip and verdict_flip:
            rec["flip_if_scale_increased"] = True
            rec["flip_reason"] = "batch_link_and_verdict_change"
        elif link_flip:
            rec["flip_if_scale_increased"] = True
            rec["flip_reason"] = "batch_link_changes"
        elif verdict_flip:
            rec["flip_if_scale_increased"] = True
            rec["flip_reason"] = "segment_status_changes"
        else:
            rec["flip_if_scale_increased"] = False
            rec["flip_reason"] = "no_flip"
