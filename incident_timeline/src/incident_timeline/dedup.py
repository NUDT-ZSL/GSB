"""告警风暴去重：同服务同事件码在风暴窗口内的重复触发首次保留、后续剔除。"""
from __future__ import annotations

from .models import DedupRecord, Event


def deduplicate(
    events: list[Event], storm_window_ms: int
) -> tuple[list[Event], list[Event], list[DedupRecord]]:
    """按 (service, code) 维护最近保留触发，窗口内重复触发计入重复计数。

    仅对告警/探测的 trigger 去重；recovery、release、note、complaint 全部保留，
    其中 note/complaint 后续作为佐证事件标注，不参与去重。
    """
    ordered = sorted(events, key=lambda item: (item.ts_ms, item.event_id))
    anchors: dict[tuple[str, str], Event] = {}
    kept: list[Event] = []
    removed: list[Event] = []
    records: list[DedupRecord] = []

    for event in ordered:
        key = (event.service, event.code)
        is_trigger = event.source in ("alert", "probe") and event.status == "trigger"
        if not is_trigger:
            kept.append(event)
            continue

        anchor = anchors.get(key)
        gap = event.ts_ms - anchor.ts_ms if anchor else None
        if anchor is not None and gap is not None and gap <= storm_window_ms:
            records.append(
                DedupRecord(
                    removed_event_id=event.event_id,
                    kept_event_id=anchor.event_id,
                    service=event.service,
                    code=event.code,
                    reason=(
                        f"告警风暴窗口内重复触发：同服务 {event.service} 同事件码 {event.code}，"
                        f"距首次保留事件 {anchor.event_id} {gap}ms <= 窗口 {storm_window_ms}ms"
                    ),
                    gap_ms=gap,
                    window_ms=storm_window_ms,
                    batch_id=event.batch_id,
                )
            )
            removed.append(event)
            continue

        anchors[key] = event
        kept.append(event)

    kept.sort(key=lambda item: (item.ts_ms, item.event_id))
    removed.sort(key=lambda item: (item.ts_ms, item.event_id))
    records.sort(key=lambda item: (item.gap_ms, item.removed_event_id))
    return kept, removed, records
