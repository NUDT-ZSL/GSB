from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .models import (
    Event,
    NormalizedSample,
    PolicyView,
    Session,
    SessionCycle,
)
from .util import digest_json


@dataclass(frozen=True)
class Finding:
    code: str
    session_id: str | None
    generation: int | None
    policy_generation: int
    detail: dict[str, Any]

    @property
    def stable_key(self) -> tuple[Any, ...]:
        return (
            self.session_id or "",
            self.generation if self.generation is not None else -1,
            self.code,
            digest_json(self.detail),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "session_id": self.session_id,
            "generation": self.generation,
            "policy_generation": self.policy_generation,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SuccessorEdge:
    parent_session_id: str
    parent_generation: int
    child_session_id: str
    child_generation: int
    child_issued_at_ms: int

    @property
    def stable_key(self) -> tuple[str, int, str, int, int]:
        return (
            self.parent_session_id,
            self.parent_generation,
            self.child_session_id,
            self.child_generation,
            self.child_issued_at_ms,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_session_id": self.parent_session_id,
            "parent_generation": self.parent_generation,
            "child_session_id": self.child_session_id,
            "child_generation": self.child_generation,
            "child_issued_at_ms": self.child_issued_at_ms,
        }


@dataclass(frozen=True)
class RevocationRequest:
    target_kind: str
    target_value: str

    @property
    def stable_key(self) -> tuple[str, str]:
        return (self.target_kind, self.target_value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_kind": self.target_kind,
            "target_value": self.target_value,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RevocationRequest":
        return cls(
            target_kind=str(value["target_kind"]),
            target_value=str(value["target_value"]),
        )


@dataclass(frozen=True)
class RevocationOutcome:
    request: RevocationRequest
    resolved_session_id: str | None
    closure: tuple[str, ...]
    cycle_session_ids: tuple[str, ...]
    codes: tuple[str, ...]

    @property
    def stable_key(self) -> tuple[str, str]:
        return self.request.stable_key

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "resolved_session_id": self.resolved_session_id,
            "closure": list(self.closure),
            "cycle_session_ids": list(self.cycle_session_ids),
            "codes": list(self.codes),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RevocationOutcome":
        return cls(
            request=RevocationRequest.from_dict(value["request"]),
            resolved_session_id=value["resolved_session_id"],
            closure=tuple(str(item) for item in value["closure"]),
            cycle_session_ids=tuple(
                str(item) for item in value["cycle_session_ids"]
            ),
            codes=tuple(str(item) for item in value["codes"]),
        )


@dataclass(frozen=True)
class SessionDecision:
    session_id: str
    generation: int
    user_id: str
    policy_key: str
    policy_generation: int
    repair_state: str
    binding_check: str
    decision_watermark: int
    codes: tuple[str, ...]
    successors: tuple[str, ...]
    invalidated_by: tuple[str, ...]
    invalidation_closure: tuple[str, ...]
    findings: tuple[Finding, ...]

    @property
    def stable_key(self) -> tuple[str, int]:
        return (self.session_id, self.generation)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "generation": self.generation,
            "user_id": self.user_id,
            "policy_key": self.policy_key,
            "policy_generation": self.policy_generation,
            "repair_state": self.repair_state,
            "binding_check": self.binding_check,
            "decision_watermark": self.decision_watermark,
            "codes": list(self.codes),
            "successors": list(self.successors),
            "invalidated_by": list(self.invalidated_by),
            "invalidation_closure": list(self.invalidation_closure),
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class AdjudicationResult:
    sample_id: str
    processed_count: int
    total_event_count: int
    decision_watermark: int
    sessions: tuple[SessionDecision, ...]
    edges: tuple[SuccessorEdge, ...]
    findings: tuple[Finding, ...]
    cycles: tuple[SessionCycle, ...]
    revocations: tuple[RevocationOutcome, ...]
    business_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "processed_count": self.processed_count,
            "total_event_count": self.total_event_count,
            "decision_watermark": self.decision_watermark,
            "sessions": [item.to_dict() for item in self.sessions],
            "successor_edges": [item.to_dict() for item in self.edges],
            "findings": [item.to_dict() for item in self.findings],
            "cycles": [item.to_dict() for item in self.cycles],
            "revocations": [item.to_dict() for item in self.revocations],
            "business_codes": list(self.business_codes),
        }


def _session_policy_view(
    sample: NormalizedSample,
    session: Session,
    event: Event | None = None,
) -> PolicyView | None:
    policy_key = (
        event.policy_key
        if event is not None and event.policy_key is not None
        else session.policy_key
    )
    return sample.policy_views.get(policy_key)


def _build_successors(
    sessions: Iterable[Session],
) -> tuple[dict[str, list[Session]], tuple[SuccessorEdge, ...]]:
    by_id = {session.session_id: session for session in sessions}
    successors: dict[str, list[Session]] = {}
    edges: list[SuccessorEdge] = []
    for session in sessions:
        if session.parent_session_id is None:
            continue
        parent = by_id.get(session.parent_session_id)
        if parent is None:
            continue
        successors.setdefault(parent.session_id, []).append(session)
        edges.append(
            SuccessorEdge(
                parent_session_id=parent.session_id,
                parent_generation=parent.generation,
                child_session_id=session.session_id,
                child_generation=session.generation,
                child_issued_at_ms=session.issued_at_ms,
            )
        )
    for children in successors.values():
        children.sort(key=lambda item: (item.issued_at_ms, item.session_id, item.generation))
    edge_tuple = tuple(sorted(edges, key=lambda item: item.stable_key))
    return successors, edge_tuple


def _tarjan_cycles(
    nodes: set[str],
    graph: dict[str, list[str]],
) -> tuple[tuple[str, ...], ...]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[list[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for successor in graph.get(node, []):
            if successor not in nodes:
                continue
            if successor not in indices:
                strongconnect(successor)
                lowlinks[node] = min(lowlinks[node], lowlinks[successor])
            elif successor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[successor])
        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            components.append(component)

    for node in sorted(nodes):
        if node not in indices:
            strongconnect(node)
    cycles = []
    for component in components:
        if len(component) > 1:
            cycles.append(tuple(sorted(component)))
        elif component[0] in graph.get(component[0], []):
            cycles.append((component[0],))
    return tuple(sorted(cycles))


def _reachable_closure(
    start: str,
    graph: dict[str, list[str]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    visited: set[str] = set()
    pending = [start]
    while pending:
        node = pending.pop()
        if node in visited:
            continue
        visited.add(node)
        for successor in sorted(graph.get(node, []), reverse=True):
            if successor not in visited:
                pending.append(successor)
    cycles = _tarjan_cycles(visited, graph)
    cycle_ids = tuple(
        sorted({session_id for cycle in cycles for session_id in cycle})
    )
    return tuple(sorted(visited)), cycle_ids


def compute_revocations(
    sample: NormalizedSample,
    requests: Iterable[RevocationRequest],
    processed_count: int,
) -> tuple[RevocationOutcome, ...]:
    sessions = sample.session_map()
    cycle_groups = {
        session_id: cycle.session_ids
        for cycle in sample.cycles
        for session_id in cycle.session_ids
    }
    successors, _ = _build_successors(sample.sessions)
    graph = {
        parent_id: [child.session_id for child in children]
        for parent_id, children in successors.items()
    }
    event_by_sequence = {
        event.event_seq: event for event in sample.events
    }
    outcomes: list[RevocationOutcome] = []
    for request in sorted(set(requests), key=lambda item: item.stable_key):
        resolved: str | None = None
        codes: list[str] = []
        if request.target_kind == "session":
            resolved = request.target_value if request.target_value in sessions else None
            if resolved is None and request.target_value in cycle_groups:
                cycle_ids = tuple(sorted(cycle_groups[request.target_value]))
                outcomes.append(
                    RevocationOutcome(
                        request=request,
                        resolved_session_id=request.target_value,
                        closure=cycle_ids,
                        cycle_session_ids=cycle_ids,
                        codes=("E_SESSION_CYCLE",),
                    )
                )
                continue
            if resolved is None:
                codes.append("E_REVOCATION_TARGET")
        elif request.target_kind == "login":
            try:
                event_seq = int(request.target_value)
            except ValueError:
                event_seq = -1
            event = event_by_sequence.get(event_seq)
            if event is None:
                codes.append("E_REVOCATION_TARGET")
            else:
                resolved = event.session_id
        else:
            codes.append("E_REVOCATION_TARGET")
        if resolved is None:
            outcomes.append(
                RevocationOutcome(
                    request=request,
                    resolved_session_id=None,
                    closure=(),
                    cycle_session_ids=(),
                    codes=tuple(codes),
                )
            )
            continue
        closure, cycle_ids = _reachable_closure(resolved, graph)
        if cycle_ids:
            codes.append("E_SESSION_CYCLE")
        outcomes.append(
            RevocationOutcome(
                request=request,
                resolved_session_id=resolved,
                closure=closure,
                cycle_session_ids=cycle_ids,
                codes=tuple(sorted(codes)),
            )
        )
    return tuple(outcomes)


def _binding_check(
    sample: NormalizedSample,
    session: Session,
    events: list[Event],
    view: PolicyView | None,
) -> tuple[str, bool]:
    if view is None:
        return ("unknown", False)
    if view.tombstone:
        return ("inactive", False)
    if view.binding_required is None:
        return ("conflicted", False)
    if not view.binding_required:
        return ("not_required", False)
    if not events:
        return ("required_unverified", False)
    saw_missing = False
    mismatch = False
    for event in events:
        if event.device_id is None:
            saw_missing = True
        elif event.device_id != session.device_id:
            mismatch = True
    if mismatch:
        return ("required_mismatch", True)
    if saw_missing:
        return ("required_unverified", False)
    return ("required_ok", False)


def adjudicate(
    sample: NormalizedSample,
    processed_count: int | None = None,
    revocation_requests: Iterable[RevocationRequest] = (),
) -> AdjudicationResult:
    if processed_count is None:
        processed_count = len(sample.events)
    processed_count = max(0, min(processed_count, len(sample.events)))
    events = list(sample.events[:processed_count])
    event_positions = {event.event_seq: index + 1 for index, event in enumerate(events)}
    sessions = list(sample.sessions)
    session_map = {session.session_id: session for session in sessions}
    successors, edges = _build_successors(sessions)
    events_by_session: dict[str, list[Event]] = {}
    for event in events:
        events_by_session.setdefault(event.session_id, []).append(event)

    findings: list[Finding] = []
    findings_by_session: dict[str, list[Finding]] = {}

    def add_finding(
        code: str,
        session: Session | None,
        policy_generation: int,
        detail: dict[str, Any],
    ) -> None:
        finding = Finding(
            code=code,
            session_id=session.session_id if session is not None else None,
            generation=session.generation if session is not None else None,
            policy_generation=policy_generation,
            detail=detail,
        )
        findings.append(finding)
        if session is not None:
            findings_by_session.setdefault(session.session_id, []).append(finding)

    for cycle in sample.cycles:
        add_finding(
            "E_SESSION_CYCLE",
            None,
            0,
            {
                "session_ids": list(cycle.session_ids),
                "reason": cycle.reason,
            },
        )

    for session in sessions:
        view = _session_policy_view(sample, session)
        policy_generation = view.policy_generation if view is not None else 0
        if view is None:
            add_finding(
                "E_POLICY_UNKNOWN",
                session,
                0,
                {"policy_key": session.policy_key},
            )
        elif view.tombstone:
            add_finding(
                "E_POLICY_TOMBSTONED",
                session,
                policy_generation,
                {"policy_key": session.policy_key},
            )
        elif view.has_conflict:
            add_finding(
                "E_POLICY_CONFLICT",
                session,
                policy_generation,
                {
                    "policy_key": session.policy_key,
                    "fields": [item.field for item in view.conflicts],
                    "conflicts": [item.to_dict() for item in view.conflicts],
                },
            )

        binding_check, binding_mismatch = _binding_check(
            sample,
            session,
            events_by_session.get(session.session_id, []),
            view,
        )
        if binding_mismatch and view is not None:
            add_finding(
                "E_BINDING_MISMATCH",
                session,
                policy_generation,
                {
                    "policy_key": session.policy_key,
                    "expected_device_id": session.device_id,
                    "binding_check": binding_check,
                },
            )

    for event in events:
        session = session_map.get(event.session_id)
        if session is None:
            continue
        if (
            event.event_type != "login"
            or event.attempt_result != "success"
            or not event.privilege_escalated
            or event.event_time_ms <= session.issued_at_ms
        ):
            continue
        view = _session_policy_view(sample, session, event)
        policy_generation = view.policy_generation if view is not None else 0
        if view is None or view.tombstone or view.has_conflict:
            continue
        session_successors = successors.get(session.session_id, [])
        rotation_after = [
            child
            for child in session_successors
            if child.issued_at_ms >= event.event_time_ms
        ]
        rotation_before = [
            child
            for child in session_successors
            if child.issued_at_ms < event.event_time_ms
        ]
        if not rotation_after and not rotation_before:
            add_finding(
                "E_SESSION_FIXATION",
                session,
                policy_generation,
                {
                    "reused_session_id": session.session_id,
                    "event_seq": event.event_seq,
                    "event_time_ms": event.event_time_ms,
                    "privilege_escalated": True,
                    "rotation_mode": view.rotation_mode,
                    "policy_key": session.policy_key,
                },
            )

    for parent in sessions:
        children = successors.get(parent.session_id, [])
        if not children:
            continue
        view = _session_policy_view(sample, parent)
        policy_generation = view.policy_generation if view is not None else 0
        if view is None or view.tombstone or view.has_conflict:
            continue
        rotation_time = min(child.issued_at_ms for child in children)
        orphan_events = [
            event
            for event in events_by_session.get(parent.session_id, [])
            if event.event_time_ms > rotation_time
        ]
        if orphan_events:
            add_finding(
                "E_ORPHAN_SESSION",
                parent,
                policy_generation,
                {
                    "reused_session_id": parent.session_id,
                    "rotated_at_ms": rotation_time,
                    "successor_session_ids": sorted(
                        {child.session_id for child in children}
                    ),
                    "event_sequences": sorted(
                        event.event_seq for event in orphan_events
                    ),
                },
            )
        if len(children) >= 2:
            rotation_times = [child.issued_at_ms for child in children]
            too_fast = any(
                right - left < view.min_rotation_interval_ms
                for left, right in zip(rotation_times, rotation_times[1:])
            )
            if too_fast:
                add_finding(
                    "E_ROTATE_TOO_FAST",
                    parent,
                    policy_generation,
                    {
                        "policy_key": parent.policy_key,
                        "min_rotation_interval_ms": view.min_rotation_interval_ms,
                        "rotation_times_ms": rotation_times,
                        "successor_session_ids": [
                            child.session_id for child in children
                        ],
                    },
                )

    revocation_outcomes = compute_revocations(
        sample,
        revocation_requests,
        processed_count,
    )
    invalidated_by: dict[str, list[str]] = {}
    invalidation_closure: dict[str, set[str]] = {}
    for outcome in revocation_outcomes:
        for session_id in outcome.closure:
            invalidated_by.setdefault(session_id, []).append(
                f"{outcome.request.target_kind}:{outcome.request.target_value}"
            )
            invalidation_closure.setdefault(session_id, set()).update(
                outcome.closure
            )
    for session_id in sorted(invalidated_by):
        session = session_map.get(session_id)
        view = _session_policy_view(sample, session) if session is not None else None
        add_finding(
            "E_REVOCATION_INVALIDATED",
            session,
            view.policy_generation if view is not None else 0,
            {
                "session_id": session_id,
                "invalidated_by": sorted(invalidated_by[session_id]),
            },
        )

    decisions: list[SessionDecision] = []
    for session in sessions:
        session_findings = tuple(
            sorted(
                findings_by_session.get(session.session_id, []),
                key=lambda item: item.stable_key,
            )
        )
        codes = tuple(sorted({finding.code for finding in session_findings}))
        view = _session_policy_view(sample, session)
        policy_generation = view.policy_generation if view is not None else 0
        binding_check, _ = _binding_check(
            sample,
            session,
            events_by_session.get(session.session_id, []),
            view,
        )
        session_events = events_by_session.get(session.session_id, [])
        watermark = max(
            (event_positions[event.event_seq] for event in session_events),
            default=0,
        )
        invalidators = tuple(sorted(set(invalidated_by.get(session.session_id, []))))
        child_ids = tuple(
            child.session_id for child in successors.get(session.session_id, [])
        )
        had_escalation_login = any(
            event.event_type == "login"
            and event.attempt_result == "success"
            and event.privilege_escalated
            and event.event_time_ms > session.issued_at_ms
            for event in session_events
        )
        has_rotation_after_escalation = False
        for event in session_events:
            if (
                event.event_type == "login"
                and event.attempt_result == "success"
                and event.privilege_escalated
                and event.event_time_ms > session.issued_at_ms
            ):
                if any(
                    child.issued_at_ms >= event.event_time_ms
                    for child in successors.get(session.session_id, [])
                ):
                    has_rotation_after_escalation = True

        if invalidators:
            repair_state = "revoked"
        elif view is None or view.has_conflict:
            repair_state = "indeterminate"
        elif view.tombstone:
            repair_state = "inactive"
        elif "E_SESSION_FIXATION" in codes or "E_BINDING_MISMATCH" in codes:
            repair_state = "vulnerable"
        elif "E_ORPHAN_SESSION" in codes:
            repair_state = "orphaned"
        elif "E_ROTATE_TOO_FAST" in codes:
            repair_state = "rate_limited"
        elif had_escalation_login and has_rotation_after_escalation:
            repair_state = "fixed"
        else:
            repair_state = "clean"

        decisions.append(
            SessionDecision(
                session_id=session.session_id,
                generation=session.generation,
                user_id=session.user_id,
                policy_key=session.policy_key,
                policy_generation=policy_generation,
                repair_state=repair_state,
                binding_check=binding_check,
                decision_watermark=watermark,
                codes=codes,
                successors=child_ids,
                invalidated_by=invalidators,
                invalidation_closure=tuple(
                    sorted(invalidation_closure.get(session.session_id, set()))
                ),
                findings=session_findings,
            )
        )

    finding_tuple = tuple(sorted(findings, key=lambda item: item.stable_key))
    all_codes = {finding.code for finding in finding_tuple}
    for outcome in revocation_outcomes:
        all_codes.update(outcome.codes)
    return AdjudicationResult(
        sample_id=sample.sample_id,
        processed_count=processed_count,
        total_event_count=len(sample.events),
        decision_watermark=processed_count,
        sessions=tuple(sorted(decisions, key=lambda item: item.stable_key)),
        edges=edges,
        findings=finding_tuple,
        cycles=sample.cycles,
        revocations=revocation_outcomes,
        business_codes=tuple(sorted(all_codes)),
    )


def decision_digest(
    sample: NormalizedSample,
    result: AdjudicationResult,
) -> str:
    return digest_json(
        {
            "sample_digest": sample.sample_digest,
            "sessions": [item.to_dict() for item in result.sessions],
            "successor_edges": [item.to_dict() for item in result.edges],
            "findings": [item.to_dict() for item in result.findings],
            "cycles": [item.to_dict() for item in result.cycles],
            "revocations": [item.to_dict() for item in result.revocations],
            "rejections": [item.to_dict() for item in sample.rejections],
            "policy_conflicts": [
                item.to_dict() for item in sample.policy_conflicts
            ],
            "business_codes": list(result.business_codes),
        }
    )
