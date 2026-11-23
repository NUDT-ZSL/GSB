"""告警降噪聚合。

按 (服务, 告警规则名) 分组, 依据注入逻辑时钟把时间邻近的告警归并为事件簇。
被静默/维护窗口完整覆盖(或事件自带标记)的告警计入抑制数, 不参与代表事件裁决。
代表事件 = 严重度最高 -> 实例权重最大 -> 逻辑时钟最早 -> 事件序号最小。
全部判定只依赖注入逻辑时钟与事件序号, 任意次运行结果逐位一致。
"""

from .model import SEVERITY_RANK, canonical_json, sha256_text


def is_suppressed(event, windows):
    if event.flag in ("silenced", "maintenance"):
        return True
    return any(win.covers(event) for win in windows)


def _representative(event_seqs, events_by_seq, topology):
    def key(seq):
        ev = events_by_seq[seq]
        weight = topology.instances[ev.instance_id].weight_micro
        return (-SEVERITY_RANK[ev.severity], -weight, ev.ts_ms, ev.seq)

    return min(event_seqs, key=key)


def compute_clusters(events, topology, windows, cluster_window_ms, services=None):
    """计算事件簇。services 为 None 时处理全部服务, 否则只处理给定服务集合。

    返回 {cluster_id: payload_dict}, payload 为规范化可序列化字典。
    """
    scope = set(services) if services is not None else None
    groups = {}
    for ev in events:
        if scope is not None and ev.service not in scope:
            continue
        groups.setdefault((ev.service, ev.rule), []).append(ev)

    clusters = {}
    for (service, rule) in sorted(groups):
        members = sorted(groups[(service, rule)], key=lambda e: (e.ts_ms, e.seq))
        ordinal = 0
        current = []
        start_ts = None
        for ev in members:
            if start_ts is None or ev.ts_ms - start_ts > cluster_window_ms:
                if current:
                    cid = f"{service}~{rule}~{ordinal:04d}"
                    clusters[cid] = _build_payload(
                        cid, service, rule, ordinal, current,
                        topology=topology, windows=windows)
                    ordinal += 1
                current = []
                start_ts = ev.ts_ms
            current.append(ev)
        if current:
            cid = f"{service}~{rule}~{ordinal:04d}"
            clusters[cid] = _build_payload(
                cid, service, rule, ordinal, current,
                topology=topology, windows=windows)
    return clusters


def _build_payload(cluster_id, service, rule, ordinal, member_events,
                   topology, windows):
    by_seq = {ev.seq: ev for ev in member_events}
    suppressed = sorted(ev.seq for ev in member_events
                        if is_suppressed(ev, windows))
    suppressed_set = set(suppressed)
    active = [seq for seq in sorted(by_seq) if seq not in suppressed_set]
    rep = _representative(active, by_seq, topology) if active else None
    rep_ev = by_seq[rep] if rep is not None else None
    payload = {
        "cluster_id": cluster_id,
        "service": service,
        "rule": rule,
        "ordinal": ordinal,
        "start_ts": min(ev.ts_ms for ev in member_events),
        "end_ts": max(ev.ts_ms for ev in member_events),
        "events": sorted(by_seq),
        "suppressed": suppressed,
        "suppressed_count": len(suppressed),
        "representative": rep,
        "status": "active" if rep is not None else "suppressed",
        "rep_instance": rep_ev.instance_id if rep_ev else None,
        "rep_severity": rep_ev.severity if rep_ev else None,
        "rep_ts": rep_ev.ts_ms if rep_ev else None,
    }
    payload["sha"] = sha256_text(canonical_json(payload))
    return payload
