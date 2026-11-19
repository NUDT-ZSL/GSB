from __future__ import annotations

import json
import os
from typing import Any

from .core import Simulator, assert_consistent, initial_state, replay_prefix
from .errors import (
    E_BAD_SEGMENT,
    E_NOTHING_TO_RESUME,
    E_RETRY_EXHAUSTED,
    E_TRACE_INCOMPLETE,
    EngineError,
)
from .scene import Scene
from .state import TcpState
from .store import Store


def observation_config_dict(scene: Scene) -> dict[str, Any]:
    config = scene.observations
    return {
        "sample_every_n_events": config.sample_every_n_events,
        "sample_times_ms": list(config.sample_times_ms),
        "sample_event_types": list(config.sample_event_types),
        "include_all_events": config.include_all_events,
        "round_decimals": config.round_decimals,
    }


def _state_at_start(
    store: Store,
    run_id: str,
    version: int,
    scene: Scene,
    start_index: int,
) -> tuple[Simulator, dict[str, Any]]:
    if start_index == 0:
        return Simulator(scene), initial_state(scene).to_dict()
    persisted = store.get_event_result(run_id, version, start_index - 1)
    if persisted is not None:
        state = TcpState.from_dict(persisted["state"])
        return Simulator(scene, state), state.to_dict()
    simulator, _ = replay_prefix(scene, start_index)
    return simulator, simulator.state.to_dict()


def _range(
    total: int,
    segment_start: int | None,
    segment_end: int | None,
) -> tuple[int, int]:
    start = 0 if segment_start is None else segment_start
    end = total if segment_end is None else segment_end
    if start < 0 or end < start or end > total:
        raise EngineError(
            E_BAD_SEGMENT,
            "event segment must satisfy 0 <= start <= end <= total_events",
            {
                "segment_start": start,
                "segment_end": end,
                "total_events": total,
            },
        )
    return start, end


def _crash_after() -> int | None:
    raw = os.environ.get("TCP_ENGINE_CRASH_AFTER")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _commit_loop(
    *,
    store: Store,
    run_id: str,
    version: int,
    scene: Scene,
    simulator: Simulator,
    start: int,
    end: int,
) -> tuple[int, dict[str, Any] | None]:
    executed = 0
    crash_after = _crash_after()
    for event in scene.events[start:end]:
        result = simulator.advance(event)
        assert_consistent(scene, result, event)
        commit = store.commit_event(
            run_id=run_id,
            version=version,
            event_index=event.index,
            event_id=event.event_id,
            event_digest=event.digest,
            event=event.canonical(),
            result=result,
        )
        executed += 1
        if crash_after is not None and executed >= crash_after:
            os._exit(70)
        fatal = result.get("fatal_error")
        if fatal is not None:
            raise EngineError(
                fatal.get("code", E_RETRY_EXHAUSTED),
                fatal.get("message", "connection entered a failed state"),
                {
                    "event_index": event.index,
                    "event_id": event.event_id,
                    "committed": commit["finalized"],
                    "failed_state": result["after"],
                },
            )
    return executed, None


def run_scene(
    *,
    store: Store,
    run_id: str,
    scene: Scene,
    segment_start: int | None = None,
    segment_end: int | None = None,
    version_tag: str | None = None,
) -> dict[str, Any]:
    total = len(scene.events)
    start, end = _range(total, segment_start, segment_end)
    store.ensure_run(run_id, scene.name, scene.source_digest)
    is_segment = start != 0 or end != total
    tag = version_tag if version_tag is not None else ("segments" if is_segment else None)
    allocated = store.allocate_version(
        run_id=run_id,
        scene_name=scene.name,
        scene_digest=scene.source_digest,
        total_events=total,
        initial_state=initial_state(scene).to_dict(),
        observations=observation_config_dict(scene),
        tag=tag,
    )
    version = allocated["version"]
    simulator, _ = _state_at_start(store, run_id, version, scene, start)
    executed, _ = _commit_loop(
        store=store,
        run_id=run_id,
        version=version,
        scene=scene,
        simulator=simulator,
        start=start,
        end=end,
    )
    store.finalize_if_complete(run_id, version)
    return build_run_summary(
        store=store,
        run_id=run_id,
        version=version,
        scene=scene,
        executed_this_call=executed,
        requested_range=[start, end],
    )


def resume_scene(
    *,
    store: Store,
    run_id: str,
    scene: Scene,
    segment_end: int | None = None,
) -> dict[str, Any]:
    store.check_digest(run_id, scene.source_digest)
    pending = store.latest_incomplete(run_id)
    if pending is None:
        raise EngineError(
            E_NOTHING_TO_RESUME,
            "there is no incomplete trajectory version to resume",
            {"run_id": run_id},
        )
    version = pending["version"]
    head = store.get_head(run_id, version)
    start = head["contiguous_count"]
    total = len(scene.events)
    _, end = _range(total, start, segment_end)
    if start > end:
        raise EngineError(
            E_BAD_SEGMENT,
            "resume end precedes the committed event head",
            {"head": start, "segment_end": end},
        )
    state = TcpState.from_dict(head["state"])
    simulator = Simulator(scene, state)
    executed, _ = _commit_loop(
        store=store,
        run_id=run_id,
        version=version,
        scene=scene,
        simulator=simulator,
        start=start,
        end=end,
    )
    store.finalize_if_complete(run_id, version)
    return build_run_summary(
        store=store,
        run_id=run_id,
        version=version,
        scene=scene,
        executed_this_call=executed,
        requested_range=[start, end],
    )


def build_run_summary(
    *,
    store: Store,
    run_id: str,
    version: int,
    scene: Scene,
    executed_this_call: int,
    requested_range: list[int],
) -> dict[str, Any]:
    meta = store.get_version(run_id, version)
    head = store.get_head(run_id, version)
    transitions = 0
    retransmissions = 0
    active_trace: dict[str, Any] | None = None
    try:
        active_trace = store.get_active_trace(run_id)
    except EngineError as exc:
        if exc.code not in {E_TRACE_INCOMPLETE}:
            raise
    if (
        active_trace is not None
        and active_trace["version"]["version"] == version
    ):
        for item in active_trace["events"]:
            result = item["result"]
            if result.get("transition") is not None:
                transitions += 1
            if result.get("retransmission") is not None:
                retransmissions += 1
    return {
        "schema_version": 1,
        "run_id": run_id,
        "scene_name": scene.name,
        "source_digest": scene.source_digest,
        "status": meta["status"],
        "complete": meta["status"] == "COMPLETE",
        "error_code": meta["error_code"],
        "events_total": len(scene.events),
        "committed_events": head["contiguous_count"],
        "events_executed_this_call": executed_this_call,
        "requested_range": requested_range,
        "transitions": transitions,
        "retransmissions": retransmissions,
        "consistent": True,
        "trace_digest": head["trace_digest"],
        "state": head["state"],
    }


def verify_scene(*, store: Store, run_id: str, scene: Scene) -> dict[str, Any]:
    store.check_digest(run_id, scene.source_digest)
    active = store.get_active_trace(run_id)
    meta = active["version"]
    events = active["events"]
    if meta["status"] == "COMPLETE" and len(events) != len(scene.events):
        raise EngineError(
            E_TRACE_INCOMPLETE,
            "complete trajectory event count differs from the source scene",
            {"stored": len(events), "source": len(scene.events)},
        )
    simulator = Simulator(scene)
    first_divergence = None
    terminal_failure = None
    for item in events:
        event = scene.events[item["event"]["index"]]
        if event.digest != item["event_digest"]:
            first_divergence = {
                "event_index": event.index,
                "reason": "event_digest_mismatch",
                "stored": item["event_digest"],
                "source": event.digest,
            }
            break
        result = simulator.advance(event)
        if json.dumps(result, sort_keys=True) != json.dumps(
            item["result"], sort_keys=True
        ):
            first_divergence = {
                "event_index": event.index,
                "reason": "result_mismatch",
                "replayed": result,
                "stored": item["result"],
            }
            break
        assert_consistent(scene, result, event)
        if result.get("fatal_error") is not None:
            terminal_failure = {
                "event_index": event.index,
                "code": result["fatal_error"].get("code"),
            }
            break
    if first_divergence is not None:
        raise EngineError(
            "E_INCONSISTENCY",
            "persisted trajectory does not match deterministic replay",
            {"first_divergence": first_divergence},
        )
    status = store.status(run_id)
    return {
        "schema_version": 1,
        "run_id": run_id,
        "scene_name": scene.name,
        "source_digest": scene.source_digest,
        "status": meta["status"],
        "verified_events": len(events),
        "consistent": True,
        "trace_digest": active["trace_digest"],
        "terminal_failure": terminal_failure,
        "final_state": status["active"]["state"] if status["active"] else None,
    }


def _sample_observation(
    item: dict[str, Any], config: dict[str, Any]
) -> bool:
    if config.get("include_all_events", True):
        return True
    event = item["event"]
    if event["index"] % config.get("sample_every_n_events", 1) == 0:
        return True
    if event["ts_ms"] in set(config.get("sample_times_ms", [])):
        return True
    if event["type"] in set(config.get("sample_event_types", [])):
        return True
    return False


def build_report(*, store: Store, run_id: str) -> dict[str, Any]:
    active = store.get_active_trace(run_id)
    meta = active["version"]
    events = active["events"]
    observations_config = json.loads(meta["observations_json"])
    transitions: list[dict[str, Any]] = []
    cwnd_trace: list[dict[str, Any]] = []
    retransmissions: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    blocking_counts: dict[str, int] = {}
    failure = None
    for item in events:
        event = item["event"]
        result = item["result"]
        after = result["after"]
        if result.get("transition") is not None:
            transitions.append(result["transition"])
        if result.get("retransmission") is not None:
            retransmissions.append(result["retransmission"])
        reason = after["blocked_reason"]
        blocking_counts[reason] = blocking_counts.get(reason, 0) + 1
        sample = {
            "event_index": event["index"],
            "event_id": event["event_id"],
            "event_type": event["type"],
            "ts_ms": event["ts_ms"],
            "tcp_state": after["tcp_state"],
            "cwnd_fixed": after["cwnd_fixed"],
            "cwnd_bytes": after["cwnd_bytes"],
            "ssthresh_fixed": after["ssthresh_fixed"],
            "ssthresh_bytes": after["ssthresh_bytes"],
            "in_flight_bytes": after["in_flight_bytes"],
            "receiver_window_bytes": after["receiver_window_bytes"],
            "flight_limit_bytes": after["flight_limit_bytes"],
            "sendable_now_bytes": after["sendable_now_bytes"],
            "blocked_reason": reason,
            "retry_count": after["retry_count"],
            "rto_ms": after["rto_ms"],
            "deadline_ms": after["deadline_ms"],
            "window_steps": result["window_steps"],
            "state_digest": item["result_digest"],
        }
        cwnd_trace.append(sample)
        if _sample_observation(item, observations_config):
            observations.append(sample)
        if result.get("fatal_error") is not None:
            failure = {
                "event_index": event["index"],
                "event_id": event["event_id"],
                **result["fatal_error"],
            }
    return {
        "schema_version": 1,
        "run_id": run_id,
        "scene_name": active["run"]["scene_name"],
        "source_digest": meta["scene_digest"],
        "status": meta["status"],
        "script_event_count": meta["total_events"],
        "executed_event_count": len(events),
        "trace_digest": active["trace_digest"],
        "failure": failure,
        "transitions": transitions,
        "cwnd_trace": cwnd_trace,
        "blocking_reason_counts": dict(sorted(blocking_counts.items())),
        "retransmissions": retransmissions,
        "observations": observations,
        "observation_config": observations_config,
    }


def status_report(*, store: Store, run_id: str) -> dict[str, Any]:
    result = store.status(run_id)
    result["schema_version"] = 1
    return result

