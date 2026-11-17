"""Deterministic SLP state-machine and cross-session analysis."""

from __future__ import annotations

import hashlib
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    ErrorCode,
    ResourceStop,
    SLPError,
    codes_in_mask,
)
from .models import (
    FileResult,
    JudgeConfig,
    ResourceLimits,
    SessionVerdict,
    TruthSummary,
    Violation,
)
from .protocol import (
    Extension,
    ParsedSLP,
    Phase,
    ResourceMeter,
    SLPRecord,
    parse_bytes,
)


REQUIRED_PHASES = (
    int(Phase.HELLO),
    int(Phase.AUTH),
    int(Phase.NEGOTIATE),
    int(Phase.DATA),
    int(Phase.CLOSE),
)

LEGAL_BASE_TRANSITIONS = {
    (int(Phase.HELLO), int(Phase.AUTH)),
    (int(Phase.AUTH), int(Phase.NEGOTIATE)),
    (int(Phase.NEGOTIATE), int(Phase.DATA)),
    (int(Phase.DATA), int(Phase.CLOSE)),
}


def _violation(
    code: ErrorCode,
    session_id: int | None,
    records: Iterable[SLPRecord],
    detail: str,
    *,
    offset: int | None = None,
) -> Violation:
    materialized = list(records)
    return Violation(
        code=code,
        session_id=session_id,
        sequences=tuple(sorted({int(record.sequence) for record in materialized})),
        sync_ids=tuple(sorted({int(record.sync_id) for record in materialized})),
        detail=detail,
        offset=offset,
    )


def _dedupe_violations(violations: Iterable[Violation]) -> list[Violation]:
    seen: set[tuple[Any, ...]] = set()
    result: list[Violation] = []
    for violation in violations:
        key = (
            int(violation.code),
            violation.session_id,
            violation.sequences,
            violation.sync_ids,
            violation.detail,
        )
        if key not in seen:
            seen.add(key)
            result.append(violation)
    result.sort(key=Violation.sort_key)
    return result


def transition_table() -> dict[str, list[str]]:
    """Describe the legal phase graph reconstructed from the protocol contract."""

    return {
        "HELLO": ["AUTH"],
        "AUTH": ["NEGOTIATE"],
        "NEGOTIATE": ["DATA"],
        "DATA": ["CLOSE", "NEGOTIATE(RENEGOTIATE)"],
        "CLOSE": [],
    }


def _legal_transition(
    previous: SLPRecord | None,
    current: SLPRecord,
    seen_phases: set[int],
) -> bool:
    if previous is None:
        return current.phase == int(Phase.HELLO)
    pair = (int(previous.phase), int(current.phase))
    if pair in LEGAL_BASE_TRANSITIONS:
        return True
    if (
        pair == (int(Phase.DATA), int(Phase.NEGOTIATE))
        and (current.extensions & 0x01)
        and int(Phase.NEGOTIATE) in seen_phases
    ):
        return True
    return False


def judge(parsed: ParsedSLP, config: JudgeConfig | None = None) -> list[SessionVerdict]:
    """Judge a fully parsed file.

    The result is normalized by session ID, sequence, sync marker, and code;
    physical file order never influences the business verdict.
    """

    cfg = config or JudgeConfig()
    records = list(parsed.records)
    by_session: dict[int, list[SLPRecord]] = defaultdict(list)
    by_sequence: dict[int, list[SLPRecord]] = defaultdict(list)
    for record in records:
        by_session[record.session_id].append(record)
        by_sequence[record.sequence].append(record)

    violations: dict[int, list[Violation]] = defaultdict(list)

    for sequence, sequence_records in sorted(by_sequence.items()):
        session_ids = sorted({record.session_id for record in sequence_records})
        if len(session_ids) > 1:
            detail = (
                f"sequence {sequence} is reused across sessions "
                f"{session_ids}; conflicting packets are listed together"
            )
            for session_id in session_ids:
                session_slice = [
                    record
                    for record in sequence_records
                    if record.session_id == session_id
                ]
                violations[session_id].append(
                    _violation(
                        ErrorCode.E_MIXUP,
                        session_id,
                        session_slice,
                        detail,
                    )
                )

    verdicts: list[SessionVerdict] = []
    for session_id in sorted(by_session):
        session_records = sorted(
            by_session[session_id],
            key=lambda record: (record.sequence, record.sync_id, record.offset),
        )
        session_violations: list[Violation] = list(violations[session_id])

        direction_counts = Counter(record.direction for record in session_records)
        directions = tuple(sorted(direction_counts))
        if len(directions) > 1:
            max_count = max(direction_counts.values())
            modes = {direction for direction, count in direction_counts.items() if count == max_count}
            conflicting = (
                session_records
                if len(modes) != 1
                else [record for record in session_records if record.direction not in modes]
            )
            session_violations.append(
                _violation(
                    ErrorCode.E_MIXUP,
                    session_id,
                    conflicting,
                    (
                        f"direction markers conflict within session {session_id}: "
                        f"{dict(sorted(direction_counts.items()))}"
                    ),
                )
            )

        by_sequence_in_session: dict[int, list[SLPRecord]] = defaultdict(list)
        for record in session_records:
            by_sequence_in_session[record.sequence].append(record)
        for sequence, duplicates in sorted(by_sequence_in_session.items()):
            if len(duplicates) > 1:
                session_violations.append(
                    _violation(
                        ErrorCode.E_TRANSITION,
                        session_id,
                        duplicates,
                        f"duplicate packet sequence {sequence} in session {session_id}",
                    )
                )

        negotiations = [
            record for record in session_records if record.phase == int(Phase.NEGOTIATE)
        ]
        renegotiations = [
            record
            for record in negotiations
            if record.extensions & int(Extension.RENEGOTIATE)
        ]

        for index, negotiation in enumerate(negotiations):
            if index > 0 and not (
                negotiation.extensions & int(Extension.RENEGOTIATE)
            ):
                session_violations.append(
                    _violation(
                        ErrorCode.E_MIXUP,
                        session_id,
                        [negotiation],
                        (
                            f"repeated NEGOTIATE at sequence {negotiation.sequence} "
                            "does not declare the RENEGOTIATE extension"
                        ),
                    )
                )

        for renegotiation in renegotiations:
            if not any(
                negotiation.sequence < renegotiation.sequence
                and not (
                    negotiation.extensions & int(Extension.RENEGOTIATE)
                )
                for negotiation in negotiations
            ):
                session_violations.append(
                    _violation(
                        ErrorCode.E_MIXUP,
                        session_id,
                        [renegotiation],
                        (
                            f"RENEGOTIATE at sequence {renegotiation.sequence} "
                            "has no earlier base NEGOTIATE phase"
                        ),
                    )
                )

        if len(renegotiations) > cfg.max_renegotiations:
            over_limit = renegotiations[cfg.max_renegotiations :]
            session_violations.append(
                _violation(
                    ErrorCode.E_STORM,
                    session_id,
                    over_limit,
                    (
                        f"session {session_id} has {len(renegotiations)} "
                        f"renegotiations, limit={cfg.max_renegotiations}"
                    ),
                )
            )

        close_positions = [
            index
            for index, record in enumerate(session_records)
            if record.phase == int(Phase.CLOSE)
        ]
        if close_positions:
            first_close = close_positions[0]
            active = session_records[: first_close + 1]
            after_close = session_records[first_close + 1 :]
            if after_close:
                session_violations.append(
                    _violation(
                        ErrorCode.E_AFTER_CLOSE,
                        session_id,
                        after_close,
                        (
                            f"session {session_id} sends {len(after_close)} packet(s) "
                            "after CLOSE"
                        ),
                    )
                )
        else:
            active = session_records

        seen_phases: set[int] = set()
        previous: SLPRecord | None = None
        for index, record in enumerate(active):
            if not _legal_transition(previous, record, seen_phases):
                if index == 0:
                    detail = (
                        f"session {session_id} starts at {record.phase_name} "
                        "instead of HELLO"
                    )
                else:
                    detail = (
                        f"illegal transition {previous.phase_name}->{record.phase_name} "
                        f"at sequence {record.sequence}"
                    )
                session_violations.append(
                    _violation(
                        ErrorCode.E_TRANSITION,
                        session_id,
                        [record],
                        detail,
                        offset=record.offset,
                    )
                )
            seen_phases.add(int(record.phase))
            previous = record

        missing = [phase for phase in REQUIRED_PHASES if phase not in seen_phases]
        if missing:
            missing_names = ", ".join(Phase(phase).name for phase in missing)
            last_record = session_records[-1]
            session_violations.append(
                _violation(
                    ErrorCode.E_INCOMPLETE,
                    session_id,
                    [last_record],
                    f"session {session_id} is missing required phase(s): {missing_names}",
                )
            )

        verdicts.append(
            SessionVerdict(
                session_id=session_id,
                packet_count=len(session_records),
                directions=directions,
                sequence_min=session_records[0].sequence,
                sequence_max=session_records[-1].sequence,
                renegotiations=len(renegotiations),
                violations=_dedupe_violations(session_violations),
            )
        )

    return verdicts


def verify_truth(records: Iterable[SLPRecord], sessions: Iterable[SessionVerdict]) -> TruthSummary:
    """Compare only embedded sync markers/truth masks with normalized verdicts.

    Injection parameters and intermediate injector data are intentionally not
    accepted by this function.
    """

    materialized_records = list(records)
    materialized_sessions = list(sessions)
    expected: set[tuple[int, int, int]] = set()
    expected_records = 0
    for record in materialized_records:
        if record.truth_mask:
            expected_records += 1
        for code in codes_in_mask(record.truth_mask):
            expected.add((record.session_id, record.sync_id, int(code)))

    observed: set[tuple[int, int, int]] = set()
    for session in materialized_sessions:
        for violation in session.violations:
            if violation.code in (ErrorCode.E_STEPS, ErrorCode.E_TIMEOUT):
                continue
            for sync_id in violation.sync_ids:
                observed.add((session.session_id, sync_id, int(violation.code)))

    matched_set = expected & observed
    missing = expected - observed
    extra = observed - expected
    details: list[dict[str, Any]] = []
    for session_id, sync_id, code_id in sorted(missing):
        details.append(
            {
                "kind": "missing",
                "session_id": session_id,
                "sync_id": sync_id,
                "code": ErrorCode(code_id).name,
            }
        )
    for session_id, sync_id, code_id in sorted(extra):
        details.append(
            {
                "kind": "unexpected",
                "session_id": session_id,
                "sync_id": sync_id,
                "code": ErrorCode(code_id).name,
            }
        )

    return TruthSummary(
        complete=True,
        expected_records=expected_records,
        expected_events=len(expected),
        matched=len(matched_set),
        mismatched=len(missing),
        unexpected=len(extra),
        details=details[:100],
    )


def _empty_truth(complete: bool = False) -> TruthSummary:
    return TruthSummary(complete=complete)


def _file_result(
    *,
    path: str,
    file_id: str,
    status: str,
    elapsed: float,
    limits: ResourceLimits,
    meter: ResourceMeter | None,
    version: int | None,
    declared_count: int | None,
    sessions: list[SessionVerdict] | None = None,
    parse_error: dict[str, Any] | None = None,
    termination: str | None = None,
    termination_detail: str | None = None,
    truth: TruthSummary | None = None,
) -> FileResult:
    return FileResult(
        path=path,
        file_id=file_id,
        status=status,
        version=version,
        declared_message_count=declared_count,
        processed_messages=meter.processed_messages if meter else 0,
        sessions=sessions or [],
        parse_error=parse_error,
        termination=termination,
        termination_detail=termination_detail,
        truth=truth or _empty_truth(),
        elapsed_seconds=elapsed,
        steps_by_session=dict(meter.steps_by_session) if meter else {},
        limits=limits.to_dict(),
    )


def analyze_bytes(
    data: bytes,
    *,
    path: str = "<memory>",
    file_id: str | None = None,
    limits: ResourceLimits | None = None,
    judge_config: JudgeConfig | None = None,
    started_at: float | None = None,
) -> FileResult:
    """Parse, judge, and verify one in-memory sample with resource isolation."""

    cfg = judge_config or JudgeConfig()
    resource_limits = limits or ResourceLimits()
    start = time.perf_counter() if started_at is None else started_at
    clock = time.perf_counter
    meter = ResourceMeter(
        started_at=start,
        max_steps_per_session=resource_limits.max_steps_per_session,
        max_file_seconds=resource_limits.max_file_seconds,
        clock=clock,
    )
    actual_file_id = file_id or hashlib.sha256(bytes(data)).hexdigest()

    try:
        parsed = parse_bytes(data, meter=meter)
    except ResourceStop as stop:
        elapsed = meter.elapsed()
        code = stop.code.name
        return _file_result(
            path=path,
            file_id=actual_file_id,
            status="limited",
            elapsed=elapsed,
            limits=resource_limits,
            meter=meter,
            version=None,
            declared_count=stop.total_messages,
            parse_error=stop.to_dict(),
            termination=code,
            termination_detail=stop.detail,
            truth=_empty_truth(False),
        )
    except SLPError as error:
        elapsed = meter.elapsed()
        return _file_result(
            path=path,
            file_id=actual_file_id,
            status="parse_error",
            elapsed=elapsed,
            limits=resource_limits,
            meter=meter,
            version=None,
            declared_count=meter.total_messages,
            parse_error=error.to_dict(),
            termination=error.code.name,
            termination_detail=error.detail,
            truth=_empty_truth(False),
        )

    try:
        meter.check_time()
        sessions = judge(parsed, cfg)
        truth = verify_truth(parsed.records, sessions)
        meter.check_time()
    except ResourceStop as stop:
        elapsed = meter.elapsed()
        return _file_result(
            path=path,
            file_id=actual_file_id,
            status="limited",
            elapsed=elapsed,
            limits=resource_limits,
            meter=meter,
            version=parsed.version,
            declared_count=parsed.declared_message_count,
            sessions=[],
            parse_error=stop.to_dict(),
            termination=stop.code.name,
            termination_detail=stop.detail,
            truth=_empty_truth(False),
        )

    status = "ok" if truth.all_consistent else "verification_failed"
    return _file_result(
        path=path,
        file_id=actual_file_id,
        status=status,
        elapsed=meter.elapsed(),
        limits=resource_limits,
        meter=meter,
        version=parsed.version,
        declared_count=parsed.declared_message_count,
        sessions=sessions,
        truth=truth,
    )


def analyze_path(
    path: str | Path,
    *,
    limits: ResourceLimits | None = None,
    judge_config: JudgeConfig | None = None,
) -> FileResult:
    start = time.perf_counter()
    target = Path(path)
    try:
        data = target.read_bytes()
    except OSError as error:
        return FileResult(
            path=str(target),
            file_id="",
            status="io_error",
            version=None,
            declared_message_count=None,
            processed_messages=0,
            sessions=[],
            parse_error={
                "code": ErrorCode.E_IO.name,
                "code_id": int(ErrorCode.E_IO),
                "detail": str(error),
                "offset": None,
            },
            termination=ErrorCode.E_IO.name,
            termination_detail=str(error),
            truth=_empty_truth(False),
            elapsed_seconds=time.perf_counter() - start,
            steps_by_session={},
            limits=(limits or ResourceLimits()).to_dict(),
        )
    return analyze_bytes(
        data,
        path=str(target),
        file_id=hashlib.sha256(data).hexdigest(),
        limits=limits,
        judge_config=judge_config,
        started_at=start,
    )


def session_verdict_from_dict(data: dict[str, Any]) -> SessionVerdict:
    violations = [
        Violation(
            code=ErrorCode(int(item["code_id"]))
            if "code_id" in item
            else ErrorCode[item["code"]],
            session_id=item.get("session_id"),
            sequences=tuple(int(value) for value in item.get("sequences", [])),
            sync_ids=tuple(int(value) for value in item.get("sync_ids", [])),
            detail=str(item.get("detail", "")),
            offset=item.get("offset"),
        )
        for item in data.get("violations", [])
    ]
    return SessionVerdict(
        session_id=int(data["session_id"]),
        packet_count=int(data.get("packet_count", 0)),
        directions=tuple(int(value) for value in data.get("directions", [])),
        sequence_min=int(data.get("sequence_min", 0)),
        sequence_max=int(data.get("sequence_max", 0)),
        renegotiations=int(data.get("renegotiations", 0)),
        violations=violations,
    )
