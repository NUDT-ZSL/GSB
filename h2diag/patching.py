from __future__ import annotations

import copy
from typing import Any

from .canonical import canonical_json, diff_paths, digest_json, flatten_paths
from .engine import Engine, finalize_state, initial_state
from .errors import (
    CLOCK_001,
    HEADER_ORDER_001,
    PATCH_OP_001,
    PATCH_REF_001,
    PATCH_REPLAY_001,
    PATCH_SETTINGS_001,
    PATCH_VERSION_001,
    WINDOW_UPDATE_001,
    error,
)
from .scheduler import apply_ranks
from .validation import normalize_scenario


def _stream_doc(scenario: dict[str, Any], stream_id: int) -> dict[str, Any]:
    for stream in scenario["streams"]:
        if stream["stream_id"] == stream_id:
            return stream
    raise error(
        PATCH_REF_001,
        "patch references an undeclared stream",
        details={"stream_id": stream_id},
    )


def _state_stream(state: dict[str, Any], stream_id: int) -> dict[str, Any]:
    key = str(stream_id)
    if key not in state["streams"]:
        raise error(
            PATCH_REF_001,
            "patch references an undeclared stream",
            details={"stream_id": stream_id},
        )
    return state["streams"][key]


def _append_events_for_patch(patch: dict[str, Any]) -> list[dict[str, Any]]:
    events = list(copy.deepcopy(patch.get("events", [])))
    for operation in patch.get("operations", []):
        if operation["op"] == "append_events":
            events.extend(copy.deepcopy(operation["events"]))
    return events


def _apply_document_ops(
    scenario: dict[str, Any],
    patch: dict[str, Any],
) -> None:
    for operation in patch["operations"]:
        op = operation["op"]
        if op == "set_weight":
            _stream_doc(scenario, operation["stream_id"])["weight"] = operation["weight"]
        elif op == "set_settings":
            current = copy.deepcopy(scenario["connection"].get("settings", {}))
            current.update(operation["settings"])
            scenario["connection"]["settings"] = current
        elif op == "set_stream_end":
            stream = _stream_doc(scenario, operation["stream_id"])
            forced = copy.deepcopy(stream.get("forced_end", {"local": False, "remote": False}))
            if operation["side"] in {"local", "both"}:
                forced["local"] = True
            if operation["side"] in {"remote", "both"}:
                forced["remote"] = True
            stream["forced_end"] = forced
        elif op == "set_expected_final_state":
            _stream_doc(scenario, operation["stream_id"])["expected_final_state"] = operation[
                "expected_final_state"
            ]
        elif op in {"append_events", "window_update"}:
            continue
        else:
            raise error(PATCH_OP_001, "unsupported patch operation", details={"op": op})
    new_events = _append_events_for_patch(patch)
    if new_events:
        scenario["events"].extend(new_events)


def build_patched_scenario(
    old_scenario: dict[str, Any],
    patch: dict[str, Any],
    *,
    committed_seq: int,
) -> tuple[dict[str, Any], str]:
    if patch["mode"] == "replace":
        runtime_ops = {
            operation["op"]
            for operation in patch["operations"]
            if operation["op"] in {"set_stream_end", "window_update"}
        }
        if runtime_ops:
            raise error(
                PATCH_REPLACE_001,
                "replace patches must encode window/end state in the full scenario",
                details={"operations": sorted(runtime_ops)},
            )
        candidate = copy.deepcopy(patch["scenario"])
        candidate.setdefault("connection_id", old_scenario["connection_id"])
        if candidate.get("connection_id") != old_scenario["connection_id"]:
            raise error(
                PATCH_REPLAY_001,
                "replace patch cannot change connection_id",
                details={
                    "expected": old_scenario["connection_id"],
                    "actual": candidate.get("connection_id"),
                },
            )
        _apply_document_ops(candidate, patch)
        normalized = normalize_scenario(candidate, preflight=False)
        if canonical_json(normalized) == canonical_json(old_scenario):
            return normalized, "noop"
        old_prefix = old_scenario["events"][:committed_seq]
        new_prefix = normalized["events"][:committed_seq]
        if old_prefix != new_prefix:
            raise error(
                PATCH_REPLAY_001,
                "replace patch changes committed frame history",
                details={"committed_seq": committed_seq},
            )
        return normalized, "replace"

    candidate = copy.deepcopy(old_scenario)
    _apply_document_ops(candidate, patch)
    normalized = normalize_scenario(candidate, preflight=False)

    old_events = old_scenario["events"]
    new_events = normalized["events"]
    if new_events[: len(old_events)] != old_events:
        raise error(
            PATCH_REPLAY_001,
            "incremental patch may only append events",
            details={"old_event_count": len(old_events), "new_event_count": len(new_events)},
        )
    old_without_events = copy.deepcopy(old_scenario)
    new_without_events = copy.deepcopy(normalized)
    old_without_events.pop("events", None)
    new_without_events.pop("events", None)
    config_changed = canonical_json(old_without_events) != canonical_json(new_without_events)
    events_appended = len(new_events) > len(old_events)
    if canonical_json(old_scenario) == canonical_json(normalized):
        kind = "noop"
    elif events_appended and not config_changed and patch["mode"] != "replace":
        kind = "append"
    else:
        kind = "incremental"
    if patch["mode"] == "append" and kind != "append":
        raise error(
            PATCH_OP_001,
            "append patch must only append events",
            details={"classified": kind},
        )
    if patch["mode"] == "noop" and kind != "noop":
        raise error(
            PATCH_OP_001,
            "noop patch must leave scenario content unchanged",
            details={"classified": kind},
        )
    return normalized, kind


def _apply_settings_operation(
    state: dict[str, Any],
    operation: dict[str, Any],
) -> None:
    direction = operation["direction"]
    side = "peer" if direction == "recv" else "local"
    settings = operation["settings"]
    old_settings = state["settings"][side]
    old_initial = int(old_settings.get("initial_window_size", 65535))
    new_initial = int(settings.get("initial_window_size", old_initial))
    delta = new_initial - old_initial
    for stream in state["streams"].values():
        if stream["reset"] or stream["state"] == "closed":
            continue
        if direction == "recv":
            new_window = int(stream["send_window"]) + delta
            maximum = int(stream["max_send_window"])
            field = "send_window"
        else:
            new_window = int(stream["recv_window"]) + delta
            maximum = int(stream["max_recv_window"])
            field = "recv_window"
        if new_window < 0 or new_window > maximum:
            raise error(
                PATCH_SETTINGS_001,
                "SETTINGS patch would violate a stream window invariant",
                details={
                    "stream_id": stream["stream_id"],
                    "field": field,
                    "new": new_window,
                    "minimum": 0,
                    "maximum": maximum,
                },
            )
        stream[field] = new_window
    merged = dict(old_settings)
    merged.update(settings)
    state["settings"][side] = merged


def _apply_stream_end_operation(
    state: dict[str, Any],
    operation: dict[str, Any],
) -> None:
    stream_id = operation["stream_id"]
    stream = _state_stream(state, stream_id)
    pending = state["headers"].get("pending")
    if pending is not None and pending["stream_id"] == stream_id:
        raise error(
            HEADER_ORDER_001,
            "cannot patch END_STREAM while a header block is incomplete",
            details={"stream_id": stream_id, "start_seq": pending["start_seq"]},
        )
    if stream["state"] == "idle":
        raise error(
            PATCH_OP_001,
            "cannot apply END_STREAM to an idle stream",
            details={"stream_id": stream_id},
        )
    side = operation["side"]
    sides = ["local", "remote"] if side == "both" else [side]
    for item in sides:
        flag = f"{item}_end"
        if stream[flag]:
            continue
        stream[flag] = True
        stream["forced_end"][item] = True
        if item == "local":
            if stream["state"] == "open":
                stream["state"] = "half-closed-local"
            elif stream["state"] == "half-closed-remote":
                stream["state"] = "closed"
        else:
            if stream["state"] == "open":
                stream["state"] = "half-closed-remote"
            elif stream["state"] == "half-closed-local":
                stream["state"] = "closed"


def _apply_window_update_operation(
    state: dict[str, Any],
    operation: dict[str, Any],
) -> None:
    direction = operation["direction"]
    level = operation["level"]
    increment = int(operation["increment"])
    if level == "connection":
        connection = state["connection"]
        if direction == "recv":
            new_value = int(connection["send_window"]) + increment
            maximum = int(connection["max_send_window"])
            field = "send_window"
            in_flight = "send_in_flight"
        else:
            new_value = int(connection["recv_window"]) + increment
            maximum = int(connection["max_recv_window"])
            field = "recv_window"
            in_flight = "recv_in_flight"
        if new_value > maximum:
            raise error(
                WINDOW_UPDATE_001,
                "patch WINDOW_UPDATE would exceed the connection upper bound",
                details={"new": new_value, "maximum": maximum},
            )
        connection[field] = new_value
        connection[in_flight] = max(0, int(connection[in_flight]) - increment)
        return
    stream = _state_stream(state, operation["stream_id"])
    if stream["reset"] or stream["state"] in {"closed", "idle"}:
        raise error(
            WINDOW_UPDATE_001,
            "patch WINDOW_UPDATE targets a closed or idle stream",
            details={"stream_id": stream["stream_id"]},
        )
    if direction == "recv":
        new_value = int(stream["send_window"]) + increment
        maximum = int(stream["max_send_window"])
        field = "send_window"
        in_flight = "send_in_flight"
    else:
        new_value = int(stream["recv_window"]) + increment
        maximum = int(stream["max_recv_window"])
        field = "recv_window"
        in_flight = "recv_in_flight"
    if new_value > maximum:
        raise error(
            WINDOW_UPDATE_001,
            "patch WINDOW_UPDATE would exceed the stream upper bound",
            details={"stream_id": stream["stream_id"], "new": new_value, "maximum": maximum},
        )
    stream[field] = new_value
    stream[in_flight] = max(0, int(stream[in_flight]) - increment)


def apply_patch_ops_to_state(
    state_before: dict[str, Any],
    patch: dict[str, Any],
    *,
    scenario_after: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    state = copy.deepcopy(state_before)
    touched: list[str] = []
    for operation in patch["operations"]:
        op = operation["op"]
        if op == "set_weight":
            stream = _state_stream(state, operation["stream_id"])
            stream["weight"] = operation["weight"]
            touched.append(f"streams.{stream['stream_id']}.weight")
        elif op == "set_settings":
            _apply_settings_operation(state, operation)
            touched.append(f"settings.{'peer' if operation['direction'] == 'recv' else 'local'}")
        elif op == "set_stream_end":
            _apply_stream_end_operation(state, operation)
            stream = _state_stream(state, operation["stream_id"])
            touched.append(f"streams.{stream['stream_id']}.state")
        elif op == "set_expected_final_state":
            stream = _state_stream(state, operation["stream_id"])
            stream["expected_final_state"] = operation["expected_final_state"]
            touched.append(f"streams.{stream['stream_id']}.expected_final_state")
        elif op == "window_update":
            _apply_window_update_operation(state, operation)
            if operation["level"] == "connection":
                touched.append("connection.window")
            else:
                touched.append(f"streams.{operation['stream_id']}.window")
        elif op == "append_events":
            touched.append("scenario.events")
        else:
            raise error(PATCH_OP_001, "unsupported patch operation", details={"op": op})

    clock = patch.get("clock_ms", state["clock_ms"])
    if not isinstance(clock, int) or isinstance(clock, bool) or clock < state["clock_ms"]:
        raise error(
            CLOCK_001,
            "patch logical clock must be a non-decreasing integer",
            details={"watermark": state["clock_ms"], "patch_clock": clock},
        )
    state["clock_ms"] = clock
    state["patch_version"] = patch["target_version"]
    state["settings"]["peer"].setdefault(
        "initial_window_size",
        scenario_after["connection"]["settings"].get("initial_window_size", 65535),
    )
    apply_ranks(state)
    finalize_state(state, digest_json({"patch_id": patch["patch_id"], "version": patch["target_version"]}))
    return state, touched


def _affected_streams(patch: dict[str, Any], kind: str, state: dict[str, Any]) -> set[int]:
    if kind == "replace":
        return {int(stream_id) for stream_id in state["streams"]}
    affected: set[int] = set()
    for operation in patch["operations"]:
        op = operation["op"]
        if op in {"set_weight", "set_stream_end", "set_expected_final_state"}:
            affected.add(operation["stream_id"])
        elif op == "window_update" and operation["level"] == "stream":
            affected.add(operation["stream_id"])
        elif op == "set_settings":
            affected.update(
                stream["stream_id"]
                for stream in state["streams"].values()
                if not stream["reset"] and stream["state"] != "closed"
            )
    return affected


def build_invalidation_manifest(
    before: dict[str, Any],
    after: dict[str, Any],
    patch: dict[str, Any],
    *,
    classified_kind: str,
) -> dict[str, Any]:
    recomputed: set[str] = {
        "patch_version",
        "state_digest",
        "chain_digest",
        "clock_ms",
        "scheduler.ranks",
        "scheduler.plan",
    }
    affected = _affected_streams(patch, classified_kind, before)
    if classified_kind == "replace":
        recomputed.update(flatten_paths(after).keys())
    else:
        for operation in patch["operations"]:
            op = operation["op"]
            if op == "set_weight":
                stream_id = operation["stream_id"]
                recomputed.add(f"streams.{stream_id}.weight")
                for stream in after["streams"].values():
                    recomputed.add(f"streams.{stream['stream_id']}.schedule_rank")
            elif op == "set_settings":
                side = "peer" if operation["direction"] == "recv" else "local"
                recomputed.add(f"settings.{side}")
                window_field = "send_window" if operation["direction"] == "recv" else "recv_window"
                for stream in after["streams"].values():
                    if not stream["reset"] and stream["state"] != "closed":
                        recomputed.add(f"streams.{stream['stream_id']}.{window_field}")
                        recomputed.add(f"streams.{stream['stream_id']}.schedule_rank")
                recomputed.add("connection.flow_capacity")
                recomputed.add(
                    "connection.send_window"
                    if operation["direction"] == "recv"
                    else "connection.recv_window"
                )
            elif op == "set_stream_end":
                stream_id = operation["stream_id"]
                recomputed.update(
                    {
                        f"streams.{stream_id}.state",
                        f"streams.{stream_id}.local_end",
                        f"streams.{stream_id}.remote_end",
                        f"streams.{stream_id}.forced_end",
                        f"streams.{stream_id}.schedule_rank",
                        f"headers.{stream_id}.reuse_decision",
                    }
                )
                for stream in after["streams"].values():
                    recomputed.add(f"streams.{stream['stream_id']}.schedule_rank")
            elif op == "set_expected_final_state":
                stream_id = operation["stream_id"]
                recomputed.add(f"streams.{stream_id}.expected_final_state")
            elif op == "window_update":
                if operation["level"] == "connection":
                    recomputed.add("connection.window")
                else:
                    recomputed.add(f"streams.{operation['stream_id']}.window")
                for stream in after["streams"].values():
                    recomputed.add(f"streams.{stream['stream_id']}.schedule_rank")
            elif op == "append_events":
                # No committed runtime value changes; future event validation was
                # performed before commit.
                recomputed.add("scenario.events")
        changed = {item["path"] for item in diff_paths(before, after)}
        changed.discard("state_digest")
        changed.discard("chain_digest")
        recomputed.update(changed)

    affected_headers = {
        str(stream_id): {
            "digest": after["streams"][str(stream_id)]["header_digest"],
            "size": after["streams"][str(stream_id)]["header_size"],
            "reused": f"headers.{stream_id}.reuse_decision" not in recomputed,
        }
        for stream_id in sorted(after["streams"], key=int)
    }
    reused_streams: dict[str, str] = {}
    for stream_id, stream in sorted(after["streams"].items(), key=lambda item: int(item[0])):
        if int(stream_id) in affected:
            continue
        reused_streams[stream_id] = digest_json(
            {
                "state": stream["state"],
                "weight": stream["weight"],
                "send_window": stream["send_window"],
                "recv_window": stream["recv_window"],
                "local_end": stream["local_end"],
                "remote_end": stream["remote_end"],
                "header_digest": stream["header_digest"],
            }
        )
    changed_paths = diff_paths(before, after)
    return {
        "patch_id": patch["patch_id"],
        "base_version": patch["base_version"],
        "target_version": patch["target_version"],
        "classification": classified_kind,
        "affected_streams": sorted(affected),
        "invalidated": sorted(recomputed),
        "recomputed": sorted(recomputed),
        "changed": changed_paths,
        "reused_stream_digests": reused_streams,
        "header_decisions": affected_headers,
        "dependencies": [
            "SETTINGS -> connection.flow_capacity -> stream windows -> scheduler ranks",
            "stream.weight -> scheduler ranks -> future dispatch only",
            "stream END marker -> stream state -> scheduler ranks -> header reuse decision",
            "committed DATA frames -> immutable in-flight order",
        ],
    }


def apply_patch_in_memory(
    old_scenario: dict[str, Any],
    state_before: dict[str, Any],
    patch: dict[str, Any],
    *,
    committed_seq: int,
) -> tuple[dict[str, Any], dict[str, Any], str, dict[str, Any]]:
    scenario_after, kind = build_patched_scenario(
        old_scenario, patch, committed_seq=committed_seq
    )
    if patch["target_version"] <= state_before["patch_version"]:
        raise error(
            PATCH_VERSION_001,
            "patch target version must be greater than the current version",
            details={
                "current": state_before["patch_version"],
                "target": patch["target_version"],
            },
            http_status=409,
            category="version",
        )
    if kind == "noop":
        return scenario_after, copy.deepcopy(state_before), kind, {
            "patch_id": patch["patch_id"],
            "classification": "noop",
            "invalidated": [],
            "recomputed": [],
            "changed": [],
        }
    state_after, _ = apply_patch_ops_to_state(
        state_before, patch, scenario_after=scenario_after
    )
    # A full replacement recomputes the watermark state from the replacement
    # scenario. The committed event prefix is required to be unchanged.
    if kind == "replace":
        base_state = initial_state(
            scenario_after, patch_version=int(patch["target_version"])
        )
        engine = Engine(scenario_after, base_state)
        max_events = min(committed_seq, len(scenario_after["events"]))
        engine.run_events(scenario_after["events"][:max_events])
        state_after = engine.state
    manifest = build_invalidation_manifest(
        state_before, state_after, patch, classified_kind=kind
    )
    return scenario_after, state_after, kind, manifest
