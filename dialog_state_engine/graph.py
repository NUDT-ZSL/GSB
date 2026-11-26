from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

from .cache import ContentCache, UpstreamRef, make_cache_key
from .canonical import canonical_line, content_hash
from .config import Rules
from .errors import (
    E_CONFIDENCE_ABOVE_THRESHOLD,
    E_FORMAT_CORRUPT,
    E_PENDING_SLOT_MISSING,
    E_SEMANTIC_DRIFT,
    fail,
)
from .models import Candidate, InternalState, SlotState
from .turns import ParsedTurn


GRAPH_VERSION = "diagnostic-dag-v1"


@dataclass(frozen=True)
class ExecutionLog:
    mode: str
    affected_suffix: tuple[int, ...]
    recomputed: tuple[str, ...]
    reused: tuple[str, ...]
    withdrawn: tuple[int, ...] = ()
    appended: tuple[int, ...] = ()
    modified: tuple[int, ...] = ()
    retained: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "affected_suffix": list(self.affected_suffix),
            "recomputed": list(self.recomputed),
            "reused": list(self.reused),
            "withdrawn": list(self.withdrawn),
            "appended": list(self.appended),
            "modified": list(self.modified),
            "retained": list(self.retained),
        }


@dataclass(frozen=True)
class RunResult:
    session_id: str
    turn_numbers: tuple[int, ...]
    state: InternalState
    scoreboard: tuple[dict[str, Any], ...]
    audit_history: tuple[dict[str, Any], ...]
    replay_verification: dict[str, Any]
    cache_keys: dict[str, str]
    execution: ExecutionLog

    def deterministic_core(self) -> dict[str, Any]:
        core = {
            "ok": True,
            "session_id": self.session_id,
            "turns": list(self.turn_numbers),
            "internal_state": self.state.to_view(),
            "scoreboard": list(self.scoreboard),
            "audit_history": list(self.audit_history),
            "replay_verification": self.replay_verification,
            "cache_keys": self.cache_keys,
        }
        core["result_digest"] = content_hash(core)
        return core

    def to_dict(self) -> dict[str, Any]:
        report = self.deterministic_core()
        report["execution"] = self.execution.to_dict()
        return report

    def result_bytes(self) -> bytes:
        return canonical_line(self.deterministic_core()).encode("utf-8")

    def report_bytes(self) -> bytes:
        return canonical_line(self.to_dict()).encode("utf-8")


@dataclass
class _TurnNode:
    turn: ParsedTurn
    parse_ref: UpstreamRef
    score_ref: UpstreamRef
    score_payload: dict[str, Any]


@dataclass
class _StatusCollector:
    recomputed: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)

    def recompute(self, stage: str) -> None:
        self.recomputed.append(stage)

    def reuse(self, stage: str) -> None:
        self.reused.append(stage)


class DiagnosticEngine:
    def __init__(self, rules: Rules, cache_dir: str | Path):
        self.rules = rules
        self.cache = ContentCache(cache_dir)

    def execute(
        self,
        session_id: str,
        turns: Iterable[ParsedTurn],
    ) -> RunResult:
        return self._run(session_id, turns, old_turns=None, mode="full")

    def revise(
        self,
        session_id: str,
        old_turns: Iterable[ParsedTurn],
        new_turns: Iterable[ParsedTurn],
    ) -> RunResult:
        return self._run(
            session_id,
            new_turns,
            old_turns=tuple(old_turns),
            mode="revision",
        )

    def replay_value(self, candidate_key: str, source_turn: int) -> Any:
        cached = self.cache.get(candidate_key, node="candidate")
        if cached is None:
            fail(
                E_SEMANTIC_DRIFT,
                "candidate cache object needed for replay is missing",
                field="candidate_key",
                details={"candidate_key": candidate_key},
            )
        candidate = cached.payload
        if candidate.get("source_turn") != source_turn:
            fail(
                E_SEMANTIC_DRIFT,
                "candidate replay source turn mismatch",
                field="source_turn",
                details={"expected": source_turn, "actual": candidate.get("source_turn")},
            )
        recomputed = Candidate.build(
            candidate["slot_key"],
            candidate["value"],
            candidate["confidence"],
            candidate["source_turn"],
            candidate["candidate_key"],
        )
        if recomputed.candidate_key != candidate_key:
            fail(
                E_SEMANTIC_DRIFT,
                "candidate replay content key mismatch",
                field="candidate_key",
            )
        return recomputed.value

    def _run(
        self,
        session_id: str,
        turns: Iterable[ParsedTurn],
        *,
        old_turns: tuple[ParsedTurn, ...] | None,
        mode: str,
    ) -> RunResult:
        ordered = tuple(sorted(turns, key=lambda turn: turn.turn_no))
        for turn in ordered:
            if turn.session_id != session_id:
                fail(
                    E_FORMAT_CORRUPT,
                    "turn session_id does not match engine execution session",
                    field="session_id",
                    details={"expected": session_id, "actual": turn.session_id},
                )
        status = _StatusCollector()
        diff = self._compute_diff(ordered, old_turns, mode)
        nodes: list[_TurnNode] = []
        for turn in ordered:
            nodes.append(self._parse_and_score(turn, status))
        merge_refs, merge_payloads = self._fold_merges(nodes, status)
        final_merge_ref = merge_refs[-1] if merge_refs else self._empty_merge_ref()
        final_merge_payload = (
            merge_payloads[-1] if merge_payloads else self._empty_merge_payload()
        )
        pending_ref, pending_payload = self._pending_node(
            final_merge_ref, final_merge_payload, status
        )
        state = self._build_state(
            session_id,
            final_merge_payload,
            pending_payload["pending_clarifications"],
        )
        scoreboard = self._scoreboard(nodes)
        summary_ref, summary_payload = self._summary_node(
            final_merge_ref, pending_ref, state, scoreboard, nodes, status
        )
        replay = self._verify_replay(state, nodes, summary_ref)
        audit_history = tuple(final_merge_payload.get("events", []))
        cache_keys = {
            "final_merge": final_merge_ref.key,
            "pending": pending_ref.key,
            "summary": summary_ref.key,
        }
        for node in nodes:
            cache_keys[f"parse.turn.{node.turn.turn_no}"] = node.parse_ref.key
            cache_keys[f"score.turn.{node.turn.turn_no}"] = node.score_ref.key
        execution = ExecutionLog(
            mode=mode,
            affected_suffix=diff["affected_suffix"],
            recomputed=tuple(status.recomputed),
            reused=tuple(status.reused),
            withdrawn=diff["withdrawn"],
            appended=diff["appended"],
            modified=diff["modified"],
            retained=diff["retained"],
        )
        # Keep the summary cache payload in the result for byte-level diagnostics.
        cache_keys["summary_payload_hash"] = summary_ref.payload_hash
        return RunResult(
            session_id=session_id,
            turn_numbers=tuple(turn.turn_no for turn in ordered),
            state=state,
            scoreboard=scoreboard,
            audit_history=audit_history,
            replay_verification=replay,
            cache_keys=cache_keys,
            execution=execution,
        )

    def _compute_diff(
        self,
        new_turns: tuple[ParsedTurn, ...],
        old_turns: tuple[ParsedTurn, ...] | None,
        mode: str,
    ) -> dict[str, Any]:
        new_by_no = {turn.turn_no: turn for turn in new_turns}
        if old_turns is None:
            return {
                "affected_suffix": tuple(sorted(new_by_no)),
                "withdrawn": (),
                "appended": tuple(sorted(new_by_no)),
                "modified": (),
                "retained": (),
            }
        old_by_no = {turn.turn_no: turn for turn in old_turns}
        withdrawn = tuple(sorted(set(old_by_no) - set(new_by_no)))
        appended = tuple(sorted(set(new_by_no) - set(old_by_no)))
        modified = tuple(
            sorted(
                turn_no
                for turn_no in set(old_by_no) & set(new_by_no)
                if content_hash(old_by_no[turn_no].to_content_dict())
                != content_hash(new_by_no[turn_no].to_content_dict())
            )
        )
        retained = tuple(
            sorted(set(old_by_no) & set(new_by_no) - set(modified))
        )
        changed = set(withdrawn) | set(appended) | set(modified)
        if not changed:
            affected: tuple[int, ...] = ()
        else:
            first_changed = min(changed)
            affected = tuple(
                turn_no for turn_no in sorted(new_by_no) if turn_no >= first_changed
            )
        return {
            "affected_suffix": affected,
            "withdrawn": withdrawn,
            "appended": appended,
            "modified": modified,
            "retained": retained,
        }

    def _parse_and_score(
        self,
        turn: ParsedTurn,
        status: _StatusCollector,
    ) -> _TurnNode:
        parse_payload = {
            "parser": "turn-parser-v1",
            "turn": turn.to_content_dict(),
        }
        parse_key = make_cache_key(
            "parse-turn",
            config_digest=self.rules.digest,
            extra=parse_payload,
        )
        parse_stage = f"parse:turn={turn.turn_no}"
        parsed = self.cache.get(parse_key, node="parse-turn")
        if parsed is None:
            parsed = self.cache.put(parse_key, node="parse-turn", payload=parse_payload)
            status.recompute(parse_stage)
        else:
            status.reuse(parse_stage)

        scored_candidates = [
            {
                **candidate.to_dict(),
                "score": self._score_candidate(candidate, turn.turn_no),
            }
            for candidate in turn.candidates
        ]
        scored_candidates.sort(
            key=lambda item: (
                -float(item["score"]),
                item["source_turn"],
                item["slot_key"],
                Candidate.build(
                    item["slot_key"],
                    item["value"],
                    item["confidence"],
                    item["source_turn"],
                    item["candidate_key"],
                ).value_key,
                item["candidate_key"],
            )
        )
        score_payload = {
            "scorer": "weighted-confidence-turn-v1",
            "session_id": turn.session_id,
            "turn_no": turn.turn_no,
            "parse_key": parsed.key,
            "candidates": scored_candidates,
            "clarification_requests": list(turn.clarification_requests),
        }
        score_key = make_cache_key(
            "score-turn",
            config_digest=self.rules.digest,
            upstream=(UpstreamRef(parsed.key, parsed.payload_hash),),
            extra={
                "scorer": "weighted-confidence-turn-v1",
                "turn_no": turn.turn_no,
            },
        )
        score_stage = f"score:turn={turn.turn_no}"
        scored = self.cache.get(score_key, node="score-turn")
        if scored is None:
            scored = self.cache.put(score_key, node="score-turn", payload=score_payload)
            status.recompute(score_stage)
        else:
            status.reuse(score_stage)
        for item in scored.payload["candidates"]:
            candidate_payload = {
                key: item[key]
                for key in (
                    "slot_key",
                    "value",
                    "confidence",
                    "source_turn",
                    "candidate_key",
                )
            }
            existing = self.cache.get(item["candidate_key"], node="candidate")
            if existing is None:
                self.cache.put(
                    item["candidate_key"],
                    node="candidate",
                    payload=candidate_payload,
                )
        return _TurnNode(
            turn=turn,
            parse_ref=UpstreamRef(parsed.key, parsed.payload_hash),
            score_ref=UpstreamRef(scored.key, scored.payload_hash),
            score_payload=scored.payload,
        )

    def _score_candidate(self, candidate: Candidate, turn_no: int) -> float:
        confidence = Decimal(str(candidate.confidence))
        turn_factor = Decimal(1) - Decimal(1) / Decimal(turn_no + 1)
        raw = (
            confidence * Decimal(str(self.rules.confidence_weight))
            + turn_factor * Decimal(str(self.rules.turn_weight))
        )
        quantum = Decimal(1).scaleb(-self.rules.score_precision)
        return float(raw.quantize(quantum, rounding=ROUND_HALF_UP))

    def _empty_merge_payload(self) -> dict[str, Any]:
        return {
            "session_id": None,
            "through_turn": None,
            "slots": {},
            "clarification_requests": [],
            "events": [],
        }

    def _empty_merge_ref(self) -> UpstreamRef:
        payload = self._empty_merge_payload()
        key = make_cache_key(
            "merge-empty",
            config_digest=self.rules.digest,
            extra={"merge": self.rules.merge_strategy},
        )
        cached = self.cache.get(key, node="merge-empty")
        if cached is None:
            cached = self.cache.put(key, node="merge-empty", payload=payload)
        return UpstreamRef(cached.key, cached.payload_hash)

    def _fold_merges(
        self,
        nodes: list[_TurnNode],
        status: _StatusCollector,
    ) -> tuple[list[UpstreamRef], list[dict[str, Any]]]:
        if not nodes:
            return [], []
        refs: list[UpstreamRef] = []
        payloads: list[dict[str, Any]] = []
        previous_payload = self._empty_merge_payload()
        previous_ref = self._empty_merge_ref()
        for node in nodes:
            merged_payload = self._merge_payload(previous_payload, node)
            key = make_cache_key(
                "merge-prefix",
                config_digest=self.rules.digest,
                upstream=(
                    previous_ref,
                    node.score_ref,
                ),
                extra={
                    "strategy": self.rules.merge_strategy,
                    "through_turn": node.turn.turn_no,
                },
            )
            stage = f"merge:through_turn={node.turn.turn_no}"
            cached = self.cache.get(key, node="merge-prefix")
            if cached is None:
                cached = self.cache.put(
                    key,
                    node="merge-prefix",
                    payload=merged_payload,
                )
                status.recompute(stage)
            else:
                status.reuse(stage)
            ref = UpstreamRef(cached.key, cached.payload_hash)
            refs.append(ref)
            payloads.append(cached.payload)
            previous_ref = ref
            previous_payload = cached.payload
        return refs, payloads

    def _merge_payload(
        self,
        previous: dict[str, Any],
        node: _TurnNode,
    ) -> dict[str, Any]:
        slots: dict[str, dict[str, Any]] = {
            key: {
                "winner": dict(value["winner"]),
                "overwritten": [dict(item) for item in value["overwritten"]],
                "conflict": value["conflict"],
                "updated_at_ms": value["updated_at_ms"],
            }
            for key, value in previous.get("slots", {}).items()
        }
        events = [dict(event) for event in previous.get("events", [])]
        requests = {
            item["slot_key"]: item["turn_no"]
            for item in previous.get("clarification_requests", [])
        }
        for request in node.score_payload["clarification_requests"]:
            requests[request] = max(requests.get(request, 0), node.turn.turn_no)
        for item in node.score_payload["candidates"]:
            candidate = Candidate.build(
                item["slot_key"],
                item["value"],
                item["confidence"],
                item["source_turn"],
                item["candidate_key"],
            )
            score = float(item["score"])
            current = slots.get(candidate.slot_key)
            if current is None:
                slots[candidate.slot_key] = {
                    "winner": candidate.to_dict(),
                    "overwritten": [],
                    "conflict": False,
                    "updated_at_ms": self.rules.timestamp_for_turn(
                        candidate.source_turn
                    ),
                }
                events.append(
                    self._event("selected", candidate, node.turn.turn_no, None)
                )
                continue
            winner = Candidate.build(
                current["winner"]["slot_key"],
                current["winner"]["value"],
                current["winner"]["confidence"],
                current["winner"]["source_turn"],
                current["winner"]["candidate_key"],
            )
            winner_score = self._score_candidate(winner, winner.source_turn)
            overwritten = list(current["overwritten"])
            action: str | None = None
            previous_key: str | None = winner.candidate_key
            if candidate.source_turn > winner.source_turn:
                overwritten.append(winner.to_dict())
                current["winner"] = candidate.to_dict()
                current["conflict"] = False
                current["updated_at_ms"] = self.rules.timestamp_for_turn(
                    candidate.source_turn
                )
                action = "turn_override"
            elif candidate.source_turn == winner.source_turn:
                if score > winner_score:
                    overwritten.append(winner.to_dict())
                    current["winner"] = candidate.to_dict()
                    current["updated_at_ms"] = self.rules.timestamp_for_turn(
                        candidate.source_turn
                    )
                    action = "same_turn_score_override"
                elif score == winner_score:
                    if candidate.value_key == winner.value_key:
                        action = "same_turn_duplicate"
                    else:
                        current["conflict"] = True
                        if candidate.value_key < winner.value_key:
                            overwritten.append(winner.to_dict())
                            current["winner"] = candidate.to_dict()
                            action = "tie_conflict_win"
                        else:
                            overwritten.append(candidate.to_dict())
                            action = "tie_conflict_lose"
                else:
                    overwritten.append(candidate.to_dict())
                    action = "same_turn_lower_score"
            else:
                overwritten.append(candidate.to_dict())
                action = "superseded"
            if action == "same_turn_duplicate":
                continue
            current["overwritten"] = overwritten
            slots[candidate.slot_key] = current
            events.append(
                self._event(action, candidate, node.turn.turn_no, previous_key)
            )
        for key, slot in slots.items():
            winner_key = slot["winner"]["candidate_key"]
            slot["overwritten"] = sorted(
                (
                    item
                    for item in slot["overwritten"]
                    if item["candidate_key"] != winner_key
                ),
                key=lambda item: (
                    item["source_turn"],
                    -self._score_candidate(
                        Candidate.build(
                            item["slot_key"],
                            item["value"],
                            item["confidence"],
                            item["source_turn"],
                            item["candidate_key"],
                        ),
                        item["source_turn"],
                    ),
                    Candidate.build(
                        item["slot_key"],
                        item["value"],
                        item["confidence"],
                        item["source_turn"],
                        item["candidate_key"],
                    ).value_key,
                    item["candidate_key"],
                ),
            )
        return {
            "session_id": node.turn.session_id,
            "through_turn": node.turn.turn_no,
            "slots": dict(sorted(slots.items())),
            "clarification_requests": [
                {"slot_key": slot_key, "turn_no": turn_no}
                for slot_key, turn_no in sorted(requests.items())
            ],
            "events": events,
        }

    @staticmethod
    def _event(
        action: str | None,
        candidate: Candidate,
        processed_turn: int,
        previous_candidate_key: str | None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "action": action or "noop",
            "slot_key": candidate.slot_key,
            "candidate_key": candidate.candidate_key,
            "source_turn": candidate.source_turn,
            "processed_turn": processed_turn,
        }
        if previous_candidate_key is not None:
            event["previous_candidate_key"] = previous_candidate_key
        return event

    def _pending_node(
        self,
        merge_ref: UpstreamRef,
        merge_payload: dict[str, Any],
        status: _StatusCollector,
    ) -> tuple[UpstreamRef, dict[str, Any]]:
        slots = merge_payload.get("slots", {})
        requests = {
            item["slot_key"]: item["turn_no"]
            for item in merge_payload.get("clarification_requests", [])
        }
        pending: list[str] = []
        missing: list[str] = []
        above_threshold: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []
        for slot_key, request_turn in sorted(requests.items()):
            slot = slots.get(slot_key)
            if slot is None:
                missing.append(slot_key)
                checks.append(
                    {
                        "slot_key": slot_key,
                        "status": "missing",
                        "request_turn": request_turn,
                    }
                )
                continue
            winner_turn = slot["winner"]["source_turn"]
            if winner_turn > request_turn:
                checks.append(
                    {
                        "slot_key": slot_key,
                        "status": "resolved_by_later_turn",
                        "request_turn": request_turn,
                        "winner_turn": winner_turn,
                    }
                )
                continue
            confidence = float(slot["winner"]["confidence"])
            if confidence >= self.rules.clarification_threshold:
                above_threshold.append(
                    {
                        "slot_key": slot_key,
                        "confidence": confidence,
                        "threshold": self.rules.clarification_threshold,
                    }
                )
                checks.append(
                    {
                        "slot_key": slot_key,
                        "status": "confidence_not_below_threshold",
                        "confidence": confidence,
                    }
                )
                continue
            pending.append(slot_key)
            checks.append(
                {
                    "slot_key": slot_key,
                    "status": "pending",
                    "confidence": confidence,
                    "threshold": self.rules.clarification_threshold,
                }
            )
        if missing:
            fail(
                E_PENDING_SLOT_MISSING,
                "clarification request refers to a missing slot",
                field=f"pending_clarifications.{missing[0]}",
                details={"missing_slots": missing},
            )
        if above_threshold:
            first = above_threshold[0]
            fail(
                E_CONFIDENCE_ABOVE_THRESHOLD,
                "clarification request slot confidence is not below threshold",
                field=f"slots.{first['slot_key']}.winner.confidence",
                details={"violations": above_threshold},
            )
        payload = {
            "pending_clarifications": sorted(pending),
            "checks": checks,
            "threshold": self.rules.clarification_threshold,
            "merge_key": merge_ref.key,
        }
        key = make_cache_key(
            "pending-clarifications",
            config_digest=self.rules.digest,
            upstream=(merge_ref,),
            extra={"threshold": self.rules.clarification_threshold},
        )
        cached = self.cache.get(key, node="pending-clarifications")
        if cached is None:
            cached = self.cache.put(
                key,
                node="pending-clarifications",
                payload=payload,
            )
            status.recompute("pending")
        else:
            status.reuse("pending")
        return UpstreamRef(cached.key, cached.payload_hash), cached.payload

    def _build_state(
        self,
        session_id: str,
        merge_payload: dict[str, Any],
        pending: Iterable[str],
    ) -> InternalState:
        slots: dict[str, SlotState] = {}
        for slot_key, value in merge_payload.get("slots", {}).items():
            winner_data = value["winner"]
            winner = Candidate.build(
                winner_data["slot_key"],
                winner_data["value"],
                winner_data["confidence"],
                winner_data["source_turn"],
                winner_data["candidate_key"],
            )
            overwritten = tuple(
                Candidate.build(
                    item["slot_key"],
                    item["value"],
                    item["confidence"],
                    item["source_turn"],
                    item["candidate_key"],
                )
                for item in value["overwritten"]
            )
            slots[slot_key] = SlotState(
                winner=winner,
                overwritten=overwritten,
                conflict=bool(value["conflict"]),
                updated_at_ms=int(value["updated_at_ms"]),
            )
        updated_at_ms = (
            max(slot.updated_at_ms for slot in slots.values())
            if slots
            else self.rules.time_origin_ms
        )
        return InternalState.build(
            session_id=session_id,
            state_generation=self.rules.default_state_generation,
            updated_at_ms=updated_at_ms,
            slots=slots,
            pending_clarifications=pending,
        )

    def _scoreboard(self, nodes: list[_TurnNode]) -> tuple[dict[str, Any], ...]:
        candidates = [
            dict(item)
            for node in nodes
            for item in node.score_payload["candidates"]
        ]
        candidates.sort(
            key=lambda item: (
                -float(item["score"]),
                item["source_turn"],
                item["slot_key"],
                Candidate.build(
                    item["slot_key"],
                    item["value"],
                    item["confidence"],
                    item["source_turn"],
                    item["candidate_key"],
                ).value_key,
                item["candidate_key"],
            )
        )
        return tuple(candidates)

    def _summary_node(
        self,
        merge_ref: UpstreamRef,
        pending_ref: UpstreamRef,
        state: InternalState,
        scoreboard: tuple[dict[str, Any], ...],
        nodes: list[_TurnNode],
        status: _StatusCollector,
    ) -> tuple[UpstreamRef, dict[str, Any]]:
        payload = {
            "internal_state": state.to_semantic_dict(),
            "state_checksum": state.state_checksum,
            "scoreboard": list(scoreboard),
            "turn_numbers": [node.turn.turn_no for node in nodes],
        }
        key = make_cache_key(
            "state-summary",
            config_digest=self.rules.digest,
            upstream=(merge_ref, pending_ref),
            extra={"graph_version": GRAPH_VERSION},
        )
        cached = self.cache.get(key, node="state-summary")
        if cached is None:
            cached = self.cache.put(key, node="state-summary", payload=payload)
            status.recompute("summary")
        else:
            status.reuse("summary")
        return UpstreamRef(cached.key, cached.payload_hash), cached.payload

    def _verify_replay(
        self,
        state: InternalState,
        nodes: list[_TurnNode],
        summary_ref: UpstreamRef,
    ) -> dict[str, Any]:
        by_turn = {node.turn.turn_no: node for node in nodes}
        checks: list[dict[str, Any]] = []
        for slot_key, slot in state.slots:
            candidates = (("winner", slot.winner),) + tuple(
                ("overwritten", item) for item in slot.overwritten
            )
            for role, candidate in candidates:
                replayed = self.replay_value(
                    candidate.candidate_key, candidate.source_turn
                )
                source = by_turn.get(candidate.source_turn)
                if source is None:
                    fail(
                        E_SEMANTIC_DRIFT,
                        "candidate source turn is absent from the graph",
                        field=f"slots.{slot_key}.{role}.source_turn",
                    )
                source_keys = {
                    item["candidate_key"]
                    for item in source.score_payload["candidates"]
                }
                if candidate.candidate_key not in source_keys:
                    fail(
                        E_SEMANTIC_DRIFT,
                        "candidate is absent from its source scoring artifact",
                        field=f"slots.{slot_key}.{role}.candidate_key",
                    )
                if replayed != candidate.value:
                    fail(
                        E_SEMANTIC_DRIFT,
                        "candidate replay produced a different slot value",
                        field=f"slots.{slot_key}.{role}.value",
                    )
                checks.append(
                    {
                        "slot_key": slot_key,
                        "role": role,
                        "candidate_key": candidate.candidate_key,
                        "source_turn": candidate.source_turn,
                        "replayed_value_hash": content_hash(candidate.value),
                    }
                )
        if not checks:
            checks = []
        return {
            "ok": True,
            "checked_count": len(checks),
            "summary_key": summary_ref.key,
            "checks": checks,
        }

