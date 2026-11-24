from pdre.models import EventRecord, PolicyRecord, SourceRef


def source(category: str = "event", seq: int = 1, name: str = "test.jsonl") -> SourceRef:
    return SourceRef(category, seq, name)


def event(
    *,
    seq: int = 1,
    process: str = "p1",
    parent: str | None = "root1",
    policy_key: str = "pdre:test",
    observed_ms: int = 1_000,
    digest: str = "sha256:" + "a" * 64,
) -> EventRecord:
    return EventRecord(
        event_seq=seq,
        endpoint_id="ep-test",
        process_guid=process,
        parent_process_guid=parent,
        policy_key=policy_key,
        action_kind="FILE_WRITE",
        risk_level=20,
        observed_ms=observed_ms,
        payload_digest=digest,
        source=source("event", seq, "events.jsonl"),
    )


def policy(
    *,
    key: str = "pdre:test",
    generation: int = 2,
    action: str = "BLOCK",
    digest: str = "sha256:" + "a" * 64,
    start_ms: int = 0,
    end_ms: int | None = 10_000,
    tombstone: bool = False,
    seq: int = 1,
    format_name: str = "v2",
) -> PolicyRecord:
    return PolicyRecord(
        policy_key=key,
        raw_policy_key=key,
        generation=generation,
        format=format_name,
        effective_start_ms=start_ms,
        effective_end_ms=end_ms,
        tombstone=tombstone,
        source=source("policy_v2", seq, "policies.jsonl"),
        action_code=None if tombstone else action,
        match_digest=None if tombstone else digest,
    )
