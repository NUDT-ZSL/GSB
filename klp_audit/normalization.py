"""Per-session stable ordering, idempotent deduplication, and seq checks."""

from __future__ import annotations

from collections import defaultdict

from .errors import E_SEQ_CONFLICT
from .models import Event, Issue, SessionData


def normalize_sessions(events: tuple[Event, ...] | list[Event]) -> list[SessionData]:
    grouped: dict[str, list[Event]] = defaultdict(list)
    for event in events:
        grouped[event.session_id].append(event)

    sessions: list[SessionData] = []
    for session_id in sorted(grouped):
        ordered = sorted(grouped[session_id], key=lambda item: (item.seq, item.record_index))
        normalized: list[Event] = []
        issues: list[Issue] = []
        deduplicated = 0
        for event in ordered:
            if not normalized:
                normalized.append(event)
                continue
            previous = normalized[-1]
            if event.seq > previous.seq:
                normalized.append(event)
                continue
            if event.seq == previous.seq and event.semantic_key() == previous.semantic_key():
                deduplicated += 1
                continue
            issues.append(
                Issue(
                    code=E_SEQ_CONFLICT,
                    message=(
                        f"session {session_id!r} has conflicting content at stage sequence "
                        f"{event.seq}"
                    ),
                    stage=event.stage,
                    seq=event.seq,
                    offset=event.offset,
                    details={
                        "kept_record_index": previous.record_index,
                        "conflicting_record_index": event.record_index,
                        "kept_offset": previous.offset,
                    },
                )
            )
        sessions.append(
            SessionData(
                session_id=session_id,
                events=normalized,
                deduplicated_count=deduplicated,
                normalization_issues=issues,
            )
        )
    return sessions

