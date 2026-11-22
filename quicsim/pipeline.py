from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .engine import (
    Engine,
    diff_snapshots,
    simulate_scene,
)
from .errors import (
    E_BAD_SCENE,
    E_RECOVERY,
    E_STATE_EXISTS,
    E_TRACE_DIVERGENCE,
    EngineError,
)
from .model import SCALE, EngineState, hash_json
from .scene import PACKET_SPACES, Scene, canonical_json, load_scene
from .storage import (
    begin_immediate,
    choose_current_row,
    commit,
    connect_read,
    connect_write,
    copy_trace_graph,
    create_session,
    current_row,
    ensure_scene_row,
    file_lock,
    insert_divergence_report,
    insert_event_commit,
    insert_rounding_entry,
    insert_run,
    insert_trace_event,
    insert_verification,
    load_stored_scene,
    new_run_id,
    next_version,
    read_rounding_ledger,
    read_trace,
    read_verifications,
    recover_committed_state,
    replace_current_pointer,
    rollback,
    run_row,
    set_session_status,
)


def _run_status(watermark: int, event_count: int, failure_code: str | None) -> str:
    if watermark < event_count:
        return "incomplete"
    if failure_code:
        return "failed"
    return "complete"


def _summary_from_row(
    row: Any,
    *,
    events_committed_this_invocation: int = 0,
    commits_written: int = 0,
    idempotent: bool = False,
) -> dict[str, Any]:
    state = json.loads(row["state_json"])
    connection = state.get("connection", {})
    connection_status = (
        "failed"
        if connection.get("failure_code")
        else "closed"
        if connection.get("closed")
        else "open"
    )
    return {
        "scene_id": row["scene_id"],
        "scene_hash": row["scene_hash"],
        "session_id": row["session_id"],
        "run_id": row["run_id"],
        "version": row["version"],
        "watermark_event": row["watermark_event"],
        "event_count": row["event_count"],
        "complete": row["watermark_event"] == row["event_count"],
        "connection_status": connection_status,
        "failure_code": connection.get("failure_code"),
        "error": (
            {
                "code": connection.get("failure_code"),
                "detail": connection.get("failure_detail"),
            }
            if connection.get("failure_code")
            else None
        ),
        "events_committed_this_invocation": events_committed_this_invocation,
        "commits_written": commits_written,
        "idempotent": idempotent,
    }


def _dependency_payload(
    scene: Scene, event_seq: int, incremental: dict[str, Any] | None
) -> dict[str, Any]:
    event = scene.events[event_seq - 1]
    dependencies: dict[str, Any] = {
        "event": event,
        "ack_ranges": event.get("ranges"),
        "ack_received_at_ms": event.get("received_at_ms"),
        "ack_delay_ms": event.get("ack_delay_ms"),
        "rounding_entry_seqs": [],
    }
    if incremental is not None:
        dependencies["rounding_entry_seqs"] = incremental.get(
            "event_observations", {}
        ).get("rounding_entry_seqs", [])
    return dependencies


def _divergence_detail(
    *,
    scene: Scene,
    event_seq: int,
    source: str,
    incremental: dict[str, Any] | None,
    full: dict[str, Any] | None,
    difference: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "event_seq": event_seq,
        "time_ms": scene.events[event_seq - 1]["time_ms"],
        "source": source,
        "field": None if difference is None else difference["field"],
        "incremental_value": (
            None if difference is None else difference["incremental_value"]
        ),
        "full_value": None if difference is None else difference["full_value"],
        "incremental_snapshot": incremental,
        "full_snapshot": full,
        "dependencies": _dependency_payload(scene, event_seq, incremental),
    }


def _persist_divergence_and_raise(
    conn: Any,
    *,
    scene: Scene,
    session_id: str | None,
    version: int | None,
    detail: dict[str, Any],
) -> None:
    rollback(conn)
    begin_immediate(conn)
    insert_divergence_report(
        conn,
        scene_id=scene.scene_id,
        session_id=session_id,
        version=version,
        event_seq=detail["event_seq"],
        detail=detail,
    )
    commit(conn)
    raise EngineError(
        E_TRACE_DIVERGENCE,
        "event-driven and full-recompute traces diverged",
        event_seq=detail["event_seq"],
        details=detail,
    )


def execute_commands(
    scene_path: str | Path,
    db_path: str | Path,
    *,
    resume: bool = False,
    commit_every: int = 1,
    max_events: int | None = None,
    crash_after: int | None = None,
    restart: bool = False,
) -> dict[str, Any]:
    if commit_every < 1:
        raise EngineError(E_BAD_SCENE, "commit_every must be >= 1")
    if max_events is not None and max_events < 1:
        raise EngineError(E_BAD_SCENE, "max_events must be >= 1")
    if crash_after is not None and crash_after < 1:
        raise EngineError(E_BAD_SCENE, "crash_after must be >= 1")

    scene = load_scene(scene_path)
    # The full deterministic preflight is deliberately completed before the
    # database is opened, so an invalid scene cannot leave persistent state.
    simulate_scene(scene)

    effective_resume = resume
    applied_total = 0
    commits_written = 0
    last_row: Any | None = None

    while True:
        with file_lock(db_path, exclusive=True):
            with connect_write(db_path) as conn:
                begin_immediate(conn)
                try:
                    ensure_scene_row(conn, scene)
                    current = current_row(conn, scene.scene_id)

                    if current is not None:
                        if current["scene_hash"] != scene.scene_hash:
                            raise EngineError(
                                E_RECOVERY,
                                "current state belongs to a different scene hash",
                            )
                        if int(current["event_count"]) != scene.event_count:
                            raise EngineError(
                                E_RECOVERY,
                                "current state event count does not match the scene",
                            )
                        complete = (
                            int(current["watermark_event"]) == scene.event_count
                        )
                        if complete and not restart:
                            last_row = current
                            rollback(conn)
                            return _summary_from_row(
                                current, idempotent=True
                            )
                        if not effective_resume and not restart:
                            rollback(conn)
                            raise EngineError(
                                E_STATE_EXISTS,
                                "an incomplete run exists; use resume",
                                details={
                                    "session_id": current["session_id"],
                                    "watermark_event": current["watermark_event"],
                                },
                            )

                    if current is None or restart:
                        session_id = create_session(conn, scene)
                        engine = Engine(scene)
                        watermark = 0
                        source_run_id: str | None = None
                        planned_version = 1
                    else:
                        state_dict = recover_committed_state(conn, scene, current)
                        injected_state = EngineState.from_dict(state_dict)
                        if injected_state.events_processed != int(
                            current["watermark_event"]
                        ):
                            raise EngineError(
                                E_RECOVERY,
                                "engine state watermark does not match current pointer",
                            )
                        engine = Engine(scene, injected_state)
                        watermark = int(current["watermark_event"])
                        source_run_id = current["run_id"]
                        session_id = current["session_id"]
                        planned_version = next_version(conn, session_id)

                    if watermark == scene.event_count:
                        last_row = current
                        rollback(conn)
                        return _summary_from_row(
                            current, idempotent=True
                        )

                    batch_end = min(
                        scene.event_count,
                        watermark + commit_every,
                        watermark + max_events if max_events is not None else scene.event_count,
                    )
                    new_snapshots: list[
        tuple[int, int, dict[str, Any], dict[str, Any]]
                    ] = []
                    state_hashes: list[str] = []

                    for event_index in range(watermark, batch_end):
                        event = scene.events[event_index]
                        incremental_snapshot = engine.apply_event(event)
                        full_engine = Engine(scene)
                        full_snapshot: dict[str, Any] | None = None
                        for replay_index in range(event_index + 1):
                            full_snapshot = full_engine.apply_event(
                                scene.events[replay_index]
                            )
                        assert full_snapshot is not None
                        difference = diff_snapshots(
                            incremental_snapshot, full_snapshot
                        )
                        if difference is not None:
                            detail = _divergence_detail(
                                scene=scene,
                                event_seq=event["seq"],
                                source="dual_path_commit",
                                incremental=incremental_snapshot,
                                full=full_snapshot,
                                difference=difference,
                            )
                            _persist_divergence_and_raise(
                                conn,
                                scene=scene,
                                session_id=session_id,
                                version=planned_version,
                                detail=detail,
                            )
                        new_snapshots.append(
                            (
                                event["seq"],
                                event["time_ms"],
                                incremental_snapshot,
                                full_snapshot,
                            )
                        )
                        state_hashes.append(engine.state.state_hash())

                    state_dict = engine.state.to_dict()
                    state_hash = hash_json(state_dict)
                    failure_code = engine.state.connection.failure_code
                    status = _run_status(
                        batch_end, scene.event_count, failure_code
                    )
                    run_id = new_run_id()
                    insert_run(
                        conn,
                        run_id=run_id,
                        scene=scene,
                        session_id=session_id,
                        version=planned_version,
                        watermark_event=batch_end,
                        status=status,
                        state_hash=state_hash,
                    )
                    copy_trace_graph(conn, source_run_id, run_id)
                    for (
                        event_seq,
                        time_ms,
                        incremental_snapshot,
                        full_snapshot,
                    ) in new_snapshots:
                        if not diff_snapshots(incremental_snapshot, full_snapshot):
                            insert_trace_event(
                                conn,
                                run_id=run_id,
                                path="incremental",
                                event_seq=event_seq,
                                time_ms=time_ms,
                                snapshot=incremental_snapshot,
                            )
                            insert_trace_event(
                                conn,
                                run_id=run_id,
                                path="full",
                                event_seq=event_seq,
                                time_ms=time_ms,
                                snapshot=full_snapshot,
                            )
                            insert_verification(
                                conn,
                                run_id=run_id,
                                event_seq=event_seq,
                                consistent=True,
                                detail=None,
                            )
                    for offset, (event_seq, _, _, _) in enumerate(new_snapshots):
                        event = scene.events[event_seq - 1]
                        insert_event_commit(
                            conn,
                            session_id=session_id,
                            scene_id=scene.scene_id,
                            event_seq=event_seq,
                            event_hash=hash_json(event),
                            state_hash=state_hashes[offset],
                            version=planned_version,
                        )
                    for entry in engine.state.congestion.rounding_entries:
                        insert_rounding_entry(
                            conn,
                            session_id=session_id,
                            entry=entry,
                        )
                    replace_current_pointer(
                        conn,
                        scene=scene,
                        session_id=session_id,
                        run_id=run_id,
                        version=planned_version,
                        watermark_event=batch_end,
                        state=state_dict,
                        state_hash=state_hash,
                    )
                    set_session_status(conn, session_id, status)
                    commit(conn)
                    commits_written += 1
                    applied_total += batch_end - watermark
                    last_row = conn.execute(
                        "SELECT * FROM current_state WHERE scene_id=?",
                        (scene.scene_id,),
                    ).fetchone()
                except EngineError:
                    rollback(conn)
                    raise
                except Exception:
                    rollback(conn)
                    raise

        if crash_after is not None and commits_written >= crash_after:
            sys.stdout.write(
                canonical_json(
                    {
                        "status": "injected_crash",
                        "watermark_event": last_row["watermark_event"],
                        "version": last_row["version"],
                        "planned_recovery": "resume",
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
            os._exit(137)

        if last_row is not None and int(last_row["watermark_event"]) >= scene.event_count:
            break
        if max_events is not None and applied_total >= max_events:
            break
        effective_resume = True
        if restart:
            restart = False

    if last_row is None:
        with connect_read(db_path) as conn:
            last_row = choose_current_row(conn, scene.scene_id)
    summary = _summary_from_row(
        last_row,
        events_committed_this_invocation=applied_total,
        commits_written=commits_written,
    )
    summary["status"] = "failed" if summary.get("failure_code") else "ok"
    return summary


def status_payload(
    db_path: str | Path, scene_id: str | None = None
) -> dict[str, Any]:
    with file_lock(db_path, exclusive=False):
        with connect_read(db_path) as conn:
            conn.execute("BEGIN")
            try:
                row = choose_current_row(conn, scene_id)
                scene = load_stored_scene(conn, row["scene_id"])
                state_dict = json.loads(row["state_json"])
                if hash_json(state_dict) != row["state_hash"]:
                    raise EngineError(
                        E_RECOVERY,
                        "current state hash mismatch",
                        details={"run_id": row["run_id"]},
                    )
                state = EngineState.from_dict(state_dict)
                engine = Engine(scene, state)
                plan = engine._sendable_plan()
                run = run_row(conn, row["run_id"])
                if run is None:
                    raise EngineError(E_RECOVERY, "current run row is missing")
                if (
                    int(run["watermark_event"]) != int(row["watermark_event"])
                    or int(run["event_count"]) != scene.event_count
                    or int(row["event_count"]) != scene.event_count
                ):
                    raise EngineError(
                        E_RECOVERY,
                        "current pointer and run metadata disagree",
                        details={
                            "run_id": row["run_id"],
                            "run_watermark_event": run["watermark_event"],
                            "current_watermark_event": row["watermark_event"],
                        },
                    )
                spaces: dict[str, Any] = {}
                for name in PACKET_SPACES:
                    space = state.spaces[name]
                    inflight = [
                        {
                            "number": packet.number,
                            "sent_at_ms": packet.sent_at_ms,
                            "frames": packet.frames,
                        }
                        for packet in sorted(
                            space.packets.values(), key=lambda item: item.number
                        )
                        if packet.status == "in_flight"
                    ]
                    spaces[name] = {
                        "max_sent_packet_number": space.max_sent_pn,
                        "largest_acked_packet_number": space.largest_acked,
                        "inflight_count": len(inflight),
                        "inflight": inflight,
                    }
                payload = {
                    "scene_id": row["scene_id"],
                    "scene_hash": row["scene_hash"],
                    "session_id": row["session_id"],
                    "run_id": row["run_id"],
                    "version": row["version"],
                    "run_status": run["status"],
                    "watermark_event": row["watermark_event"],
                    "event_count": row["event_count"],
                    "complete": row["watermark_event"] == row["event_count"],
                    "logical_time_ms": state.time_ms,
                    "packet_spaces": spaces,
                    "inflight_digest": state.inflight_digest(),
                    "congestion": {
                        "cwnd_fixed": state.congestion.cwnd_fixed,
                        "cwnd_scale": SCALE,
                        "cwnd_packets_floor": state.congestion.cwnd_fixed // SCALE,
                        "cwnd_packets_repr": (
                            f"{state.congestion.cwnd_fixed // SCALE}."
                            f"{state.congestion.cwnd_fixed % SCALE:06d}"
                        ),
                        "ssthresh_fixed": state.congestion.ssthresh_fixed,
                        "ssthresh_packets_floor": state.congestion.ssthresh_fixed
                        // SCALE,
                        "inflight_packets": state.inflight_count(),
                        "remaining_packets": plan["cwnd_remaining_packets"],
                        "carry_fixed": state.congestion.carry_fixed,
                    },
                    "flow_control": {
                        "max_data": state.max_data,
                        "used_bytes": state.connection_used_bytes(),
                        "remaining_bytes": max(
                            0, state.max_data - state.connection_used_bytes()
                        ),
                        "streams": [
                            {
                                "stream_id": sid,
                                "max_stream_data": stream.max_stream_data,
                                "highest_sent_offset": stream.highest_sent_offset,
                                "remaining_bytes": max(
                                    0,
                                    stream.max_stream_data
                                    - stream.highest_sent_offset,
                                ),
                            }
                            for sid, stream in sorted(state.streams.items())
                        ],
                    },
                    "sendable": {
                        "sendable_packets": plan["sendable_packets"],
                        "sendable_bytes": plan["sendable_bytes"],
                        "cwnd_remaining_packets": plan["cwnd_remaining_packets"],
                        "connection_remaining_bytes": plan[
                            "connection_remaining_bytes"
                        ],
                        "limiters": plan["limiters"],
                    },
                    "probe": state.probe.to_dict(),
                    "connection": {
                        **state.connection.to_dict(),
                        "status": (
                            "failed"
                            if state.connection.failure_code
                            else "closed"
                            if state.connection.closed
                            else "open"
                        ),
                    },
                    "rounding_ledger_count": len(
                        state.congestion.rounding_entries
                    ),
                }
                return payload
            finally:
                try:
                    conn.execute("COMMIT")
                except Exception:
                    pass


def verify_payload(
    db_path: str | Path,
    scene_id: str | None = None,
    *,
    scene_path: str | Path | None = None,
) -> dict[str, Any]:
    with file_lock(db_path, exclusive=False):
        with connect_read(db_path) as conn:
            conn.execute("BEGIN")
            try:
                row = choose_current_row(conn, scene_id)
                scene = load_stored_scene(conn, row["scene_id"])
                if scene_path is not None:
                    external = load_scene(scene_path)
                    if external.scene_id != scene.scene_id:
                        raise EngineError(
                            E_SCENE_MISMATCH,
                            "scene id differs from the persisted scene",
                        )
                    if external.scene_hash != scene.scene_hash:
                        raise EngineError(
                            E_SCENE_MISMATCH,
                            "scene hash differs from the persisted scene",
                        )
                watermark = int(row["watermark_event"])
                event_count = int(row["event_count"])
                if row["scene_hash"] != scene.scene_hash:
                    raise EngineError(
                        E_RECOVERY,
                        "current state scene hash does not match the stored scene",
                    )
                if event_count != scene.event_count or watermark > event_count:
                    raise EngineError(
                        E_RECOVERY,
                        "current state watermark is outside the scene event range",
                        details={
                            "watermark_event": watermark,
                            "event_count": event_count,
                            "scene_event_count": scene.event_count,
                        },
                    )
                incremental_rows = {
                    item["event_seq"]: item
                    for item in read_trace(conn, row["run_id"], "incremental")
                }
                full_rows = {
                    item["event_seq"]: item
                    for item in read_trace(conn, row["run_id"], "full")
                }
                verification_rows = {
                    item["event_seq"]: item
                    for item in read_verifications(conn, row["run_id"])
                }
                checks = {
                    "trace_pairwise": True,
                    "trace_contiguous": True,
                    "independent_replay": True,
                    "state_hash": True,
                    "rounding_ledger": True,
                    "event_commit_ledger": True,
                    "verification_rows": True,
                }
                first_divergence: dict[str, Any] | None = None

                def record_divergence(detail: dict[str, Any]) -> None:
                    nonlocal first_divergence
                    if first_divergence is None:
                        first_divergence = detail

                if len(incremental_rows) != watermark:
                    checks["trace_contiguous"] = False
                if len(full_rows) != watermark:
                    checks["trace_contiguous"] = False
                if len(verification_rows) != watermark:
                    checks["verification_rows"] = False
                for path_name, rows_for_path in (
                    ("incremental", incremental_rows),
                    ("full", full_rows),
                    ("verification", verification_rows),
                ):
                    extra_sequences = sorted(
                        event_seq
                        for event_seq in rows_for_path
                        if event_seq > watermark
                    )
                    if extra_sequences:
                        record_divergence(
                            {
                                "event_seq": extra_sequences[0],
                                "source": f"persisted_{path_name}_extra_event",
                                "field": "event_seq",
                                "incremental_value": watermark,
                                "full_value": extra_sequences[0],
                                "dependencies": _dependency_payload(
                                    scene,
                                    min(extra_sequences[0], scene.event_count),
                                    None,
                                ),
                            }
                        )

                expected_state_hashes: dict[int, str] = {}
                engine = Engine(scene)
                for event_seq in range(1, watermark + 1):
                    if (
                        event_seq not in incremental_rows
                        or event_seq not in full_rows
                    ):
                        checks["trace_contiguous"] = False
                        record_divergence(
                            {
                                "event_seq": event_seq,
                                "source": "persisted_trace_missing",
                                "field": "trace_events",
                                "incremental_value": event_seq in incremental_rows,
                                "full_value": event_seq in full_rows,
                                "dependencies": _dependency_payload(
                                    scene, event_seq, None
                                ),
                            }
                        )
                        continue
                    incremental_persisted = json.loads(
                        incremental_rows[event_seq]["snapshot_json"]
                    )
                    full_persisted = json.loads(
                        full_rows[event_seq]["snapshot_json"]
                    )
                    expected_time_ms = scene.events[event_seq - 1]["time_ms"]
                    if (
                        int(incremental_rows[event_seq]["time_ms"])
                        != expected_time_ms
                        or int(full_rows[event_seq]["time_ms"]) != expected_time_ms
                    ):
                        checks["trace_pairwise"] = False
                        record_divergence(
                            {
                                "event_seq": event_seq,
                                "source": "persisted_event_time",
                                "field": "time_ms",
                                "incremental_value": incremental_rows[event_seq][
                                    "time_ms"
                                ],
                                "full_value": full_rows[event_seq]["time_ms"],
                                "dependencies": _dependency_payload(
                                    scene, event_seq, incremental_persisted
                                ),
                            }
                        )
                    if hash_json(incremental_persisted) != incremental_rows[
                        event_seq
                    ]["snapshot_hash"] or hash_json(full_persisted) != full_rows[
                        event_seq
                    ]["snapshot_hash"]:
                        checks["trace_pairwise"] = False
                        record_divergence(
                            {
                                "event_seq": event_seq,
                                "source": "persisted_snapshot_hash",
                                "field": "snapshot_hash",
                                "incremental_value": incremental_rows[event_seq][
                                    "snapshot_hash"
                                ],
                                "full_value": hash_json(full_persisted),
                                "incremental_snapshot": incremental_persisted,
                                "full_snapshot": full_persisted,
                                "dependencies": _dependency_payload(
                                    scene, event_seq, incremental_persisted
                                ),
                            }
                        )
                    pairwise_difference = diff_snapshots(
                        incremental_persisted, full_persisted
                    )
                    if pairwise_difference is not None:
                        checks["trace_pairwise"] = False
                        record_divergence(
                            _divergence_detail(
                                scene=scene,
                                event_seq=event_seq,
                                source="persisted_pairwise",
                                incremental=incremental_persisted,
                                full=full_persisted,
                                difference=pairwise_difference,
                            )
                        )

                    verification = verification_rows.get(event_seq)
                    if verification is None or int(verification["consistent"]) != 1:
                        checks["verification_rows"] = False
                        record_divergence(
                            {
                                "event_seq": event_seq,
                                "source": "persisted_verification_row",
                                "field": "consistent",
                                "incremental_value": None,
                                "full_value": None,
                                "dependencies": _dependency_payload(
                                    scene, event_seq, incremental_persisted
                                ),
                            }
                        )

                    expected_incremental = engine.apply_event(
                        scene.events[event_seq - 1]
                    )
                    expected_state_hashes[event_seq] = engine.state.state_hash()
                    full_engine = Engine(scene)
                    expected_full: dict[str, Any] | None = None
                    for replay_index in range(event_seq):
                        expected_full = full_engine.apply_event(
                            scene.events[replay_index]
                        )
                    assert expected_full is not None
                    independent_pair = diff_snapshots(
                        expected_incremental, expected_full
                    )
                    persisted_incremental_difference = diff_snapshots(
                        incremental_persisted, expected_incremental
                    )
                    persisted_full_difference = diff_snapshots(
                        full_persisted, expected_full
                    )
                    if (
                        independent_pair is not None
                        or persisted_incremental_difference is not None
                        or persisted_full_difference is not None
                    ):
                        checks["independent_replay"] = False
                        difference = (
                            independent_pair
                            or persisted_incremental_difference
                            or persisted_full_difference
                        )
                        record_divergence(
                            _divergence_detail(
                                scene=scene,
                                event_seq=event_seq,
                                source="independent_replay",
                                incremental=incremental_persisted,
                                full=full_persisted,
                                difference=difference,
                            )
                        )

                expected_state = engine.state.to_dict()
                if hash_json(expected_state) != row["state_hash"]:
                    checks["state_hash"] = False
                    if first_divergence is None:
                        last_seq = max(1, watermark)
                        record_divergence(
                            {
                                "event_seq": last_seq,
                                "source": "current_state_hash",
                                "field": "state_hash",
                                "incremental_value": row["state_hash"],
                                "full_value": hash_json(expected_state),
                                "dependencies": _dependency_payload(
                                    scene, last_seq, None
                                ),
                            }
                        )
                run = run_row(conn, row["run_id"])
                if (
                    run is None
                    or run["state_hash"] != row["state_hash"]
                    or int(run["watermark_event"]) != watermark
                    or int(run["event_count"]) != event_count
                ):
                    checks["state_hash"] = False
                    record_divergence(
                        {
                            "event_seq": max(1, watermark),
                            "source": "run_state_hash",
                            "field": "state_hash",
                            "incremental_value": row["state_hash"],
                            "full_value": None if run is None else run["state_hash"],
                            "run_watermark_event": (
                                None if run is None else run["watermark_event"]
                            ),
                            "expected_watermark_event": watermark,
                            "dependencies": _dependency_payload(
                                scene, max(1, watermark), None
                            ),
                        }
                    )

                ledger_rows = {
                    int(item["entry_seq"]): item
                    for item in read_rounding_ledger(conn, row["session_id"])
                }
                expected_ledger = engine.state.congestion.rounding_entries
                if len(ledger_rows) != len(expected_ledger):
                    checks["rounding_ledger"] = False
                    expected_sequences = {int(entry["seq"]) for entry in expected_ledger}
                    extra_sequences = sorted(set(ledger_rows) - expected_sequences)
                    if extra_sequences and first_divergence is None:
                        record_divergence(
                            {
                                "event_seq": ledger_rows[extra_sequences[0]][
                                    "event_seq"
                                ],
                                "source": "rounding_ledger_extra_entry",
                                "field": "rounding_entry_seq",
                                "incremental_value": len(expected_ledger),
                                "full_value": extra_sequences[0],
                                "dependencies": {
                                    "rounding_entry_seqs": [extra_sequences[0]]
                                },
                            }
                        )
                for entry in expected_ledger:
                    stored = ledger_rows.get(int(entry["seq"]))
                    if (
                        stored is None
                        or stored["entry_hash"] != hash_json(entry)
                        or json.loads(stored["entry_json"]) != entry
                    ):
                        checks["rounding_ledger"] = False
                        if first_divergence is None:
                            record_divergence(
                                {
                                    "event_seq": entry["event_seq"],
                                    "source": "rounding_ledger",
                                    "field": "rounding_entry",
                                    "incremental_value": entry,
                                    "full_value": (
                                        None
                                        if stored is None
                                        else json.loads(stored["entry_json"])
                                    ),
                                    "dependencies": {
                                        "rounding_entry_seqs": [entry["seq"]]
                                    },
                                }
                            )
                        break

                commits = conn.execute(
                    """
                    SELECT event_seq, event_hash, state_hash, version
                    FROM event_commits WHERE session_id=? ORDER BY event_seq
                    """,
                    (row["session_id"],),
                ).fetchall()
                if len(commits) != watermark:
                    checks["event_commit_ledger"] = False
                commit_sequences = [int(item["event_seq"]) for item in commits]
                expected_sequence_list = list(range(1, watermark + 1))
                if commit_sequences != expected_sequence_list:
                    checks["event_commit_ledger"] = False
                    if first_divergence is None:
                        mismatch_seq = min(
                            set(commit_sequences)
                            ^ set(expected_sequence_list),
                            default=max(1, watermark),
                        )
                        record_divergence(
                            {
                                "event_seq": min(mismatch_seq, scene.event_count),
                                "source": "event_commit_sequence",
                                "field": "event_seq",
                                "incremental_value": expected_sequence_list,
                                "full_value": commit_sequences,
                                "dependencies": _dependency_payload(
                                    scene,
                                    min(mismatch_seq, scene.event_count),
                                    None,
                                ),
                            }
                        )
                for item in commits:
                    event_seq = int(item["event_seq"])
                    if event_seq < 1 or event_seq > len(scene.events):
                        checks["event_commit_ledger"] = False
                        continue
                    if item["event_hash"] != hash_json(scene.events[event_seq - 1]):
                        checks["event_commit_ledger"] = False
                        if first_divergence is None:
                            record_divergence(
                                {
                                    "event_seq": event_seq,
                                    "source": "event_commit_hash",
                                    "field": "event_hash",
                                    "incremental_value": item["event_hash"],
                                    "full_value": hash_json(
                                        scene.events[event_seq - 1]
                                    ),
                                    "dependencies": _dependency_payload(
                                        scene, event_seq, None
                                    ),
                                }
                            )
                        break
                    if event_seq in expected_state_hashes and item[
                        "state_hash"
                    ] != expected_state_hashes[event_seq]:
                        checks["event_commit_ledger"] = False
                        if first_divergence is None:
                            record_divergence(
                                {
                                    "event_seq": event_seq,
                                    "source": "event_commit_state_hash",
                                    "field": "state_hash",
                                    "incremental_value": expected_state_hashes[
                                        event_seq
                                    ],
                                    "full_value": item["state_hash"],
                                    "dependencies": _dependency_payload(
                                        scene, event_seq, None
                                    ),
                                }
                            )
                        break

                consistent = all(checks.values())
                complete = watermark == event_count
                connection = expected_state.get("connection", {})
                return {
                    "scene_id": row["scene_id"],
                    "scene_hash": row["scene_hash"],
                    "session_id": row["session_id"],
                    "run_id": row["run_id"],
                    "version": row["version"],
                    "watermark_event": watermark,
                    "event_count": event_count,
                    "complete": complete,
                    "consistent": consistent,
                    "ok": consistent and complete,
                    "connection_status": (
                        "failed"
                        if connection.get("failure_code")
                        else "closed"
                        if connection.get("closed")
                        else "open"
                    ),
                    "failure_code": connection.get("failure_code"),
                    "checks": checks,
                    "first_divergence": first_divergence,
                    "verified_events": min(watermark, len(scene.events)),
                }
            finally:
                try:
                    conn.execute("COMMIT")
                except Exception:
                    pass
