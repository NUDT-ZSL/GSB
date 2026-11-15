from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .canonical import sha256_value
from .config import RulesConfig
from .errors import ErrorCode, EventRejected, TriggerChainRejected, Violation
from .rng import derive_rng_stream, rng_stream_hash
from .state import GameState, ResourceBudgetTracker

KNOWN_ACTIONS = {"play_card", "gain_resource", "end_turn", "damage"}
TRIGGER_ACTIONS = {"damage", "gain_resource", "noop"}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class ParsedEvent:
    seq: int
    actor: str
    action: str
    target: str
    cost: int
    rng_seed_delta: int
    seed: int
    card_id: str | None = None
    amount: int | None = None
    triggers: tuple[dict[str, Any], ...] = ()
    rng_expected: tuple[int, ...] | None = None
    rng_hash_expected: str | None = None
    source_line: int | None = None


@dataclass
class MatchReplayResult:
    match_id: str
    seed: int
    status: str
    errors: list[Violation]
    traces: list[dict[str, Any]]
    final_state: dict[str, Any]
    final_state_hash: str
    budget: dict[str, Any]
    events_received: int
    events_processed: int
    rejected_events: int
    trigger_rollbacks: list[dict[str, Any]]
    memory_estimate_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "seed": self.seed,
            "status": self.status,
            "events_received": self.events_received,
            "events_processed": self.events_processed,
            "rejected_events": self.rejected_events,
            "errors": [violation.to_dict() for violation in self.errors],
            "traces": self.traces,
            "trigger_rollbacks": self.trigger_rollbacks,
            "budget": self.budget,
            "final_state": self.final_state,
            "final_state_hash": self.final_state_hash,
            "memory_estimate_bytes": self.memory_estimate_bytes,
        }


def _malformed(seq: int | None, message: str, **context: Any) -> Violation:
    return Violation(ErrorCode.MALFORMED_EVENT, message, seq=seq, context=context)


def _validated_trigger(
    raw: Any,
    config: RulesConfig,
    *,
    path: str,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise EventRejected(
            _malformed(None, "trigger must be an object", trigger_path=path)
        )
    action = raw.get("action")
    if not isinstance(action, str) or action not in TRIGGER_ACTIONS:
        raise EventRejected(
            _malformed(
                None,
                "trigger has an unknown or invalid action",
                trigger_path=path,
                action=action,
            )
        )
    target = raw.get("target", "self")
    if not isinstance(target, str) or not target:
        raise EventRejected(
            _malformed(
                None,
                "trigger target must be a non-empty string",
                trigger_path=path,
            )
        )
    resolved_target = target
    if target == "self" or target in config.players:
        resolved_target = target
    else:
        raise EventRejected(
            _malformed(
                None,
                "trigger target is not a configured player",
                trigger_path=path,
                target=target,
            )
        )

    amount = raw.get("amount", 0)
    if not _is_int(amount) or amount < 0:
        raise EventRejected(
            _malformed(
                None,
                "trigger amount must be a non-negative integer",
                trigger_path=path,
            )
        )
    children = raw.get("children", [])
    if not isinstance(children, list):
        raise EventRejected(
            _malformed(
                None,
                "trigger children must be a list",
                trigger_path=path,
            )
        )
    validated: dict[str, Any] = {
        "action": action,
        "target": resolved_target,
        "amount": amount,
        "children": [
            _validated_trigger(child, config, path=f"{path}.{index}")
            for index, child in enumerate(children)
        ],
    }
    return validated


def _parse_event(
    raw: Mapping[str, Any],
    config: RulesConfig,
    *,
    seq: int,
    match_seed: int,
) -> ParsedEvent:
    source_line = raw.get("_source_line")
    source_context = {"line": source_line} if _is_int(source_line) else {}

    actor = raw.get("actor")
    if not isinstance(actor, str) or actor not in config.players:
        raise EventRejected(
            _malformed(
                seq,
                "actor must be a configured player",
                actor=actor,
                **source_context,
            )
        )
    action = raw.get("action")
    if not isinstance(action, str) or action not in KNOWN_ACTIONS:
        raise EventRejected(
            _malformed(
                seq,
                "action is missing, unknown, or not a string",
                action=action,
                allowed_actions=sorted(KNOWN_ACTIONS),
                **source_context,
            )
        )
    target_value = raw.get("target")
    if not isinstance(target_value, str) or not target_value:
        raise EventRejected(
            _malformed(
                seq,
                "target must be a non-empty string",
                target=target_value,
                **source_context,
            )
        )
    target = actor if target_value == "self" else target_value
    if target not in config.players:
        raise EventRejected(
            _malformed(
                seq,
                "target is not a configured player",
                target=target_value,
                **source_context,
            )
        )

    cost = raw.get("cost")
    if not _is_int(cost) or cost < 0:
        raise EventRejected(
            _malformed(
                seq,
                "cost must be a non-negative integer",
                cost=cost,
                **source_context,
            )
        )
    rng_seed_delta = raw.get("rng_seed_delta")
    if not _is_int(rng_seed_delta):
        raise EventRejected(
            _malformed(
                seq,
                "rng_seed_delta must be an integer",
                rng_seed_delta=rng_seed_delta,
                **source_context,
            )
        )

    card_id: str | None = None
    amount: int | None = None
    if action == "play_card":
        card_value = raw.get("card_id")
        if not isinstance(card_value, str) or card_value not in config.cards:
            raise EventRejected(
                _malformed(
                    seq,
                    "play_card requires a configured card_id",
                    card_id=card_value,
                    **source_context,
                )
            )
        card_id = card_value
        expected_cost = config.cards[card_id].cost
        if cost != expected_cost:
            raise EventRejected(
                Violation(
                    ErrorCode.COST_MISMATCH,
                    "declared event cost does not match the card cost",
                    seq=seq,
                    context={
                        "card_id": card_id,
                        "declared_cost": cost,
                        "configured_cost": expected_cost,
                        **source_context,
                    },
                )
            )
    elif action in {"gain_resource", "damage"}:
        amount_value = raw.get("amount")
        if not _is_int(amount_value) or amount_value < 0:
            raise EventRejected(
                _malformed(
                    seq,
                    f"{action} requires a non-negative integer amount",
                    amount=amount_value,
                    **source_context,
                )
            )
        amount = amount_value
        if cost != 0:
            raise EventRejected(
                Violation(
                    ErrorCode.COST_MISMATCH,
                    f"{action} must have zero cost",
                    seq=seq,
                    context={"declared_cost": cost, "configured_cost": 0, **source_context},
                )
            )
    else:
        if cost != 0:
            raise EventRejected(
                Violation(
                    ErrorCode.COST_MISMATCH,
                    "end_turn must have zero cost",
                    seq=seq,
                    context={"declared_cost": cost, "configured_cost": 0, **source_context},
                )
            )
        if target != actor:
            raise EventRejected(
                _malformed(
                    seq,
                    "end_turn target must be the acting player or self",
                    target=target_value,
                    **source_context,
                )
            )

    raw_triggers = raw.get("triggers", [])
    if not isinstance(raw_triggers, list):
        raise EventRejected(
            _malformed(seq, "triggers must be a list", **source_context)
        )
    try:
        triggers = tuple(
            _validated_trigger(trigger, config, path=str(index))
            for index, trigger in enumerate(raw_triggers)
        )
    except EventRejected as exc:
        violation = Violation(
            exc.violation.code,
            exc.violation.message,
            seq=seq,
            context={**exc.violation.context, "line": source_line}
            if _is_int(source_line)
            else dict(exc.violation.context),
        )
        raise EventRejected(violation) from exc

    rng_expected: tuple[int, ...] | None = None
    if "rng_expected" in raw:
        expected_raw = raw["rng_expected"]
        if (
            not isinstance(expected_raw, list)
            or not expected_raw
            or len(expected_raw) > config.rng_stream_size
            or any(not _is_int(value) or value < 0 or value >= 2**64 for value in expected_raw)
        ):
            raise EventRejected(
                _malformed(
                    seq,
                    "rng_expected must be a non-empty list of uint64 integers",
                    **source_context,
                )
            )
        rng_expected = tuple(int(value) for value in expected_raw)

    rng_hash_expected: str | None = None
    if "rng_hash_expected" in raw:
        hash_raw = raw["rng_hash_expected"]
        if not isinstance(hash_raw, str) or not hash_raw.startswith("sha256:"):
            raise EventRejected(
                _malformed(
                    seq,
                    "rng_hash_expected must be a sha256-prefixed string",
                    **source_context,
                )
            )
        rng_hash_expected = hash_raw

    return ParsedEvent(
        seq=seq,
        actor=actor,
        action=action,
        target=target,
        cost=cost,
        rng_seed_delta=rng_seed_delta,
        seed=match_seed,
        card_id=card_id,
        amount=amount,
        triggers=triggers,
        rng_expected=rng_expected,
        rng_hash_expected=rng_hash_expected,
        source_line=source_line if _is_int(source_line) else None,
    )


def _resource_violations(
    seq: int,
    violations: Mapping[str, list[str]],
) -> list[Violation]:
    result: list[Violation] = []
    if "negative" in violations:
        result.append(
            Violation(
                ErrorCode.RESOURCE_OVERFLOW,
                "resource balance became negative",
                seq=seq,
                context={
                    "kind": "negative",
                    "players": sorted(violations["negative"]),
                },
            )
        )
    if "above_max" in violations:
        result.append(
            Violation(
                ErrorCode.RESOURCE_OVERFLOW,
                "resource balance exceeded the configured cap",
                seq=seq,
                context={
                    "kind": "above_max",
                    "players": sorted(violations["above_max"]),
                },
            )
        )
    return result


def _merge_violation_maps(
    destination: dict[str, list[str]], source: Mapping[str, list[str]]
) -> None:
    for kind, players in source.items():
        existing = destination.setdefault(kind, [])
        for player_id in players:
            if player_id not in existing:
                existing.append(player_id)


def _event_resource_before(state: GameState, event: ParsedEvent) -> int:
    return state.resources[event.actor]


def _apply_base_event(state: GameState, event: ParsedEvent, config: RulesConfig) -> int:
    charged_cost = 0
    if event.action == "play_card":
        assert event.card_id is not None
        card = config.cards[event.card_id]
        charged_cost = card.cost
        state.resources[event.actor] -= card.cost
        state.total_cost += card.cost
        state.hp[event.target] -= card.damage
        if card.resource_gain:
            state.resources[event.actor] += card.resource_gain
        state.played_cards[event.card_id] = state.played_cards.get(event.card_id, 0) + 1
    elif event.action == "gain_resource":
        assert event.amount is not None
        state.resources[event.target] += event.amount
    elif event.action == "end_turn":
        state.turn += 1
        if config.turn_regeneration:
            state.resources[event.actor] += config.turn_regeneration
    elif event.action == "damage":
        assert event.amount is not None
        state.hp[event.target] -= event.amount
    state.event_count += 1
    return charged_cost


def _execute_trigger(
    trigger: Mapping[str, Any],
    *,
    depth: int,
    path: str,
    state: GameState,
    config: RulesConfig,
    tracker: ResourceBudgetTracker,
    seq: int,
    actor: str,
    trigger_trace: list[dict[str, Any]],
    violation_map: dict[str, list[str]],
) -> None:
    if depth > config.trigger_chain_depth:
        raise TriggerChainRejected(
            Violation(
                ErrorCode.TRIGGER_DEPTH,
                "trigger chain exceeded the configured maximum depth",
                seq=seq,
                context={
                    "trigger_path": path,
                    "attempted_depth": depth,
                    "max_depth": config.trigger_chain_depth,
                },
            )
        )

    action = trigger["action"]
    target_value = trigger["target"]
    target = actor if target_value == "self" else target_value
    amount = trigger["amount"]
    if target not in config.players:
        raise TriggerChainRejected(
            _malformed(
                seq,
                "trigger target is not a configured player",
                trigger_path=path,
                target=target_value,
            )
        )

    if action == "damage":
        state.hp[target] -= amount
    elif action == "gain_resource":
        state.resources[target] += amount
    elif action != "noop":
        raise TriggerChainRejected(
            _malformed(
                seq,
                "trigger has an unknown action",
                trigger_path=path,
                action=action,
            )
        )
    state.trigger_count += 1
    observed = tracker.observe(state, seq)
    _merge_violation_maps(violation_map, observed)
    trigger_trace.append(
        {
            "path": path,
            "depth": depth,
            "action": action,
            "target": target,
            "amount": amount,
            "status": "applied",
            "state_hash": state.state_hash(),
            "resource_overflow": bool(observed),
        }
    )

    for index, child in enumerate(trigger.get("children", [])):
        _execute_trigger(
            child,
            depth=depth + 1,
            path=f"{path}.{index}",
            state=state,
            config=config,
            tracker=tracker,
            seq=seq,
            actor=actor,
            trigger_trace=trigger_trace,
            violation_map=violation_map,
        )


def _rejected_trace(
    *,
    seq: int | None,
    raw: Mapping[str, Any],
    state: GameState,
    violation: Violation,
) -> dict[str, Any]:
    state_dict = state.to_dict()
    return {
        "seq": seq,
        "processed": False,
        "status": "rejected",
        "action": raw.get("action"),
        "actor": raw.get("actor"),
        "target": raw.get("target"),
        "state_before_hash": state.state_hash(),
        "state_hash": state.state_hash(),
        "state": state_dict,
        "errors": [violation.to_dict()],
    }


def _failure_result(
    match_id: str,
    seed: int,
    events: list[dict[str, Any]],
    config: RulesConfig,
    violation: Violation,
) -> MatchReplayResult:
    state = GameState.initial(config)
    tracker = ResourceBudgetTracker.initial(state, config.resource_cap)
    final_state = state.to_dict()
    return MatchReplayResult(
        match_id=match_id,
        seed=seed,
        status="violation",
        errors=[violation],
        traces=[],
        final_state=final_state,
        final_state_hash=sha256_value(final_state),
        budget=tracker.to_dict(),
        events_received=len(events),
        events_processed=0,
        rejected_events=0,
        trigger_rollbacks=[],
    )


def _replay_match_impl(
    match_id: str,
    events: list[dict[str, Any]],
    config: RulesConfig,
) -> MatchReplayResult:
    state = GameState.initial(config)
    tracker = ResourceBudgetTracker.initial(state, config.resource_cap)
    errors: list[Violation] = []
    traces: list[dict[str, Any]] = []
    trigger_rollbacks: list[dict[str, Any]] = []
    events_processed = 0
    rejected_events = 0
    match_seed: int | None = None
    expected_seq = 1

    for raw in events:
        if not isinstance(raw, dict):
            violation = _malformed(None, "event must be a JSON object")
            errors.append(violation)
            rejected_events += 1
            break

        seq_value = raw.get("seq")
        if not _is_int(seq_value) or seq_value < 1:
            violation = _malformed(
                None,
                "seq must be a positive integer",
                seq=seq_value,
                line=raw.get("_source_line"),
            )
            errors.append(violation)
            rejected_events += 1
            break
        seq = int(seq_value)

        if seq != expected_seq:
            violation = Violation(
                ErrorCode.SEQ_GAP,
                "event sequence is not contiguous and strictly increasing",
                seq=seq,
                context={"expected_seq": expected_seq, "actual_seq": seq},
            )
            errors.append(violation)
            rejected_events += 1
            traces.append(
                _rejected_trace(seq=seq, raw=raw, state=state, violation=violation)
            )
            break

        seed_value = raw.get("seed", match_seed if match_seed is not None else 0)
        if not _is_int(seed_value):
            violation = _malformed(seq, "seed must be an integer")
            errors.append(violation)
            rejected_events += 1
            traces.append(
                _rejected_trace(seq=seq, raw=raw, state=state, violation=violation)
            )
            break
        if match_seed is None:
            match_seed = int(seed_value)
        elif int(seed_value) != match_seed:
            violation = _malformed(
                seq,
                "seed changed within one match",
                expected_seed=match_seed,
                actual_seed=seed_value,
            )
            errors.append(violation)
            rejected_events += 1
            traces.append(
                _rejected_trace(seq=seq, raw=raw, state=state, violation=violation)
            )
            break

        try:
            event = _parse_event(raw, config, seq=seq, match_seed=match_seed)
        except EventRejected as exc:
            violation = exc.violation
            errors.append(violation)
            rejected_events += 1
            traces.append(
                _rejected_trace(seq=seq, raw=raw, state=state, violation=violation)
            )
            break

        state_before = copy.deepcopy(state)
        rng_stream = derive_rng_stream(
            match_seed,
            seq,
            event.rng_seed_delta,
            config.rng_stream_size,
        )
        rng_hash = rng_stream_hash(rng_stream)
        event_errors: list[Violation] = []
        if event.rng_expected is not None:
            expected_values = list(event.rng_expected)
            actual_values = rng_stream[: len(expected_values)]
            if actual_values != expected_values:
                event_errors.append(
                    Violation(
                        ErrorCode.RNG_DRIFT,
                        "derived random stream differs from the event expectation",
                        seq=seq,
                        context={
                            "expected": expected_values,
                            "actual": actual_values,
                        },
                    )
                )
        if (
            event.rng_hash_expected is not None
            and rng_hash != event.rng_hash_expected
        ):
            event_errors.append(
                Violation(
                    ErrorCode.RNG_DRIFT,
                    "derived random stream hash differs from the event expectation",
                    seq=seq,
                    context={
                        "expected_rng_hash": event.rng_hash_expected,
                        "actual_rng_hash": rng_hash,
                    },
                )
            )

        charged_cost = _apply_base_event(state, event, config)
        base_violation_map = tracker.observe(state, seq)
        trigger_trace: list[dict[str, Any]] = []
        trigger_rolled_back = False

        if event.triggers:
            chain_state = copy.deepcopy(state)
            chain_tracker = copy.deepcopy(tracker)
            chain_violations: dict[str, list[str]] = {}
            try:
                for root_index, trigger in enumerate(event.triggers):
                    _execute_trigger(
                        trigger,
                        depth=1,
                        path=str(root_index),
                        state=state,
                        config=config,
                        tracker=tracker,
                        seq=seq,
                        actor=event.actor,
                        trigger_trace=trigger_trace,
                        violation_map=chain_violations,
                    )
                _merge_violation_maps(base_violation_map, chain_violations)
            except TriggerChainRejected as exc:
                state = chain_state
                tracker = chain_tracker
                trigger_rolled_back = True
                event_errors.append(exc.violation)
                trigger_rollbacks.append(
                    {
                        "seq": seq,
                        "trigger_path": exc.violation.context.get("trigger_path"),
                        "attempted_depth": exc.violation.context.get("attempted_depth"),
                        "max_depth": config.trigger_chain_depth,
                        "rollback_state_hash": state.state_hash(),
                        "discarded_state_hash": trace_hash
                        if (trace_hash := trigger_trace[-1].get("state_hash"))
                        else None,
                        "discarded_trigger_steps": len(trigger_trace),
                    }
                )

        event_errors.extend(_resource_violations(seq, base_violation_map))
        tracker.total_cost = state.total_cost
        events_processed += 1
        expected_seq = seq + 1
        errors.extend(event_errors)

        final_state = state.to_dict()
        resource_before = _event_resource_before(state_before, event)
        trace = {
            "seq": seq,
            "processed": True,
            "status": "ok" if not event_errors else "violation",
            "actor": event.actor,
            "action": event.action,
            "target": event.target,
            "card_id": event.card_id,
            "declared_cost": event.cost,
            "cost_charged": charged_cost,
            "resource_before": resource_before,
            "resource_after": state.resources[event.actor],
            "rng_seed_delta": event.rng_seed_delta,
            "rng_stream": rng_stream,
            "rng_hash": rng_hash,
            "state_before_hash": state_before.state_hash(),
            "state_hash": state.state_hash(),
            "state": final_state,
            "trigger_trace": trigger_trace,
            "trigger_rolled_back": trigger_rolled_back,
            "errors": [violation.to_dict() for violation in event_errors],
        }
        traces.append(trace)

    if match_seed is None:
        match_seed = 0
    final_state = state.to_dict()
    return MatchReplayResult(
        match_id=match_id,
        seed=match_seed,
        status="ok" if not errors else "violation",
        errors=errors,
        traces=traces,
        final_state=final_state,
        final_state_hash=sha256_value(final_state),
        budget=tracker.to_dict(),
        events_received=len(events),
        events_processed=events_processed,
        rejected_events=rejected_events,
        trigger_rollbacks=trigger_rollbacks,
    )


def replay_match(
    match_id: str,
    events: list[dict[str, Any]],
    config: RulesConfig,
) -> MatchReplayResult:
    try:
        return _replay_match_impl(match_id, events, config)
    except Exception as exc:
        seed_value = 0
        if events and isinstance(events[0], dict) and _is_int(events[0].get("seed")):
            seed_value = int(events[0]["seed"])
        violation = Violation(
            ErrorCode.MALFORMED_EVENT,
            "replay failed while processing the match",
            context={"exception_type": type(exc).__name__},
        )
        return _failure_result(match_id, seed_value, events, config, violation)


def replay_all(
    ordered_matches: Iterable[tuple[str, list[dict[str, Any]]]],
    config: RulesConfig,
) -> list[MatchReplayResult]:
    return [replay_match(match_id, events, config) for match_id, events in ordered_matches]
