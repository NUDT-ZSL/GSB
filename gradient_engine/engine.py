from __future__ import annotations

import math
import os
from typing import Any

from .canon import canonical_text, sha256_json
from .errors import EngineError, ErrorCode
from .model import GradientEvent, Injection, LoadedInputs
from .storage import Store


PHASE_RANK = {"reduce_scatter": 0, "all_gather": 1}

OUTCOME_COMPLETED = "SYNC_COMPLETED"
OUTCOME_BUDGET = "BUDGET_EXHAUSTED"
OUTCOME_CONCURRENCY_DEFERRED = "DEFERRED_CONCURRENCY"
OUTCOME_MEMORY_LAG = "MEMORY_LAG"
OUTCOME_NODE_FAULT = "NODE_FAULT_LAG"
OUTCOME_STALE_SPEC = "STALE_SPEC_REJECTED"


def build_stream(inputs: LoadedInputs) -> list[tuple[int, int, int, str, Any]]:
    stream: list[tuple[int, int, int, str, Any]] = []
    for event in inputs.events:
        stream.append((event.logic_clock_ms, 1, event.event_seq, "event", event))
    for injection in inputs.injections:
        rank = 0 if injection.kind == "spec_replace" else 2
        stream.append((injection.logic_clock_ms, rank, injection.order_index, "injection", injection))
    stream.sort(key=lambda item: (item[0], item[1], item[2]))
    return stream


def recover_store(store: Store) -> list[int]:
    open_rows = store.open_rounds()
    recovered: list[int] = []
    if not open_rows:
        return recovered
    with store.conn:
        for row in open_rows:
            round_no = row["round_no"]
            store.conn.execute(
                "INSERT INTO recovery_events(round_no,old_status,reason,clock_ms,recovered_clock_ms)"
                " VALUES(?,?,?,?,?)",
                (
                    round_no,
                    row["status"],
                    "uncommitted_round_detected" if row["status"] != "committed" else "unexpected",
                    row["logic_clock_ms"],
                    store.clock_watermark(),
                ),
            )
            store.conn.execute("DELETE FROM round_candidates WHERE round_no=?", (round_no,))
            store.conn.execute("DELETE FROM memory_ledger WHERE round_no=?", (round_no,))
            store.conn.execute("DELETE FROM rounds WHERE round_no=?", (round_no,))
            recovered.append(round_no)
        current = int(store.get_meta("recovery_count", "0") or 0)
        store.set_meta("recovery_count", current + len(recovered))
    return recovered


def run_to_completion(
    store: Store,
    inputs: LoadedInputs | None = None,
    *,
    crash: dict[str, int] | None = None,
    max_rounds: int = 100_000,
    recover: bool = True,
) -> dict[str, Any]:
    recovered = recover_store(store) if recover else []
    if inputs is None:
        inputs = store.load_inputs_from_db()

    stream = build_stream(inputs)
    concurrency = int(inputs.scheduling["max_concurrency"])
    budget = int(inputs.scheduling["time_budget_ms"])
    completed_rounds = 0
    force_next_stream = False

    while completed_rounds < max_rounds:
        pos = int(store.get_meta("stream_position", "0") or 0)
        clock = store.clock_watermark()
        active_version = store.active_spec_version()
        pending = pending_candidates(store.conn, active_version)
        eligible = [candidate for candidate in pending if _node_eligible(store.conn, candidate.node_id)]

        items: list[tuple[int, int, int, str, Any]] = []
        next_item = stream[pos] if pos < len(stream) else None
        if next_item is not None:
            next_clock = next_item[0]
            if next_clock <= clock or not eligible or force_next_stream:
                target_clock = max(clock, next_clock)
                end = pos
                while end < len(stream) and stream[end][0] <= target_clock:
                    items.append(stream[end])
                    end += 1
                clock = target_clock
                pos = end

        if not items and not eligible:
            break
        if not items and not pending:
            break

        report = execute_round(
            store,
            inputs,
            items,
            new_stream_position=pos,
            round_clock=clock,
            concurrency=concurrency,
            budget=budget,
            crash=crash,
        )
        completed_rounds += 1
        force_next_stream = report["started_count"] == 0 and not report["applied_event_seqs"] and not report["applied_injection_ids"]
        if force_next_stream and pos >= len(stream):
            break

    if completed_rounds >= max_rounds:
        raise EngineError(ErrorCode.STATE_INVALID, "round limit exceeded; refusing to spin forever")

    final_status = store.status()
    return {
        "ok": True,
        "recovered_rounds": recovered,
        "rounds_executed": completed_rounds,
        "last_committed_round": int(store.get_meta("last_committed_round", "0") or 0),
        "clock_watermark_ms": store.clock_watermark(),
        "active_spec_version": store.active_spec_version(),
        "completion_sets": final_status["completion_sets"],
        "node_count": final_status["node_count"],
    }


class Candidate:
    __slots__ = (
        "spec_version", "node_id", "layer", "shard_index", "event_seq",
        "logic_clock_ms", "expected_commit_ms", "duration_ms", "phase",
    )

    def __init__(self, row: Any, phase: str):
        self.spec_version = row["spec_version"]
        self.node_id = row["node_id"]
        self.layer = row["layer"]
        self.shard_index = row["shard_index"]
        self.event_seq = row["event_seq"]
        self.logic_clock_ms = row["logic_clock_ms"]
        self.expected_commit_ms = row["expected_commit_ms"]
        self.duration_ms = row["duration_ms"]
        self.phase = phase

    @property
    def key(self) -> str:
        return f"v{self.spec_version}/{self.node_id}/{self.layer}#{self.shard_index}/{self.phase}"

    def sort_tuple(self) -> tuple[Any, ...]:
        return (self.node_id, self.shard_index, self.layer, PHASE_RANK[self.phase])

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_key": self.key,
            "spec_version": self.spec_version,
            "node_id": self.node_id,
            "layer": self.layer,
            "shard_index": self.shard_index,
            "phase": self.phase,
            "event_seq": self.event_seq,
        }


def pending_candidates(conn: Any, spec_version: int) -> list[Candidate]:
    rows = conn.execute(
        """
        SELECT r.spec_version, r.node_id, r.layer, r.shard_index, r.event_seq,
               e.logic_clock_ms, e.expected_commit_ms, e.duration_ms,
               rs.round_no AS rs_round, ag.round_no AS ag_round
          FROM reports r
          JOIN events e ON e.event_seq = r.event_seq
          LEFT JOIN phase_completions rs
            ON rs.spec_version=r.spec_version AND rs.node_id=r.node_id
           AND rs.layer=r.layer AND rs.shard_index=r.shard_index
           AND rs.phase='reduce_scatter'
          LEFT JOIN phase_completions ag
            ON ag.spec_version=r.spec_version AND ag.node_id=r.node_id
           AND ag.layer=r.layer AND ag.shard_index=r.shard_index
           AND ag.phase='all_gather'
         WHERE r.spec_version=?
        """,
        (spec_version,),
    ).fetchall()
    candidates: list[Candidate] = []
    for row in rows:
        if row["rs_round"] is None:
            candidates.append(Candidate(row, "reduce_scatter"))
        elif row["ag_round"] is None:
            candidates.append(Candidate(row, "all_gather"))
    candidates.sort(key=Candidate.sort_tuple)
    return candidates


def _node_eligible(conn: Any, node_id: str) -> bool:
    row = conn.execute("SELECT faulty,memory_blocked FROM node_state WHERE node_id=?", (node_id,)).fetchone()
    return row is not None and not row["faulty"] and not row["memory_blocked"]


def execute_round(
    store: Store,
    inputs: LoadedInputs,
    items: list[tuple[int, int, int, str, Any]],
    *,
    new_stream_position: int,
    round_clock: int,
    concurrency: int,
    budget: int,
    crash: dict[str, int] | None,
) -> dict[str, Any]:
    conn = store.conn
    round_no = int(conn.execute("SELECT COALESCE(MAX(round_no),0)+1 AS n FROM rounds WHERE status='committed'").fetchone()["n"])
    active_version_before = store.active_spec_version()

    with conn:
        conn.execute(
            "INSERT INTO rounds(round_no,status,logic_clock_ms,active_spec_version,"
            "max_concurrency,time_budget_ms) VALUES(?, 'prepared', ?, ?, ?, ?)",
            (round_no, round_clock, active_version_before, concurrency, budget),
        )

    _maybe_crash(crash, "after_prepare_round", round_no)

    candidate_decisions: list[dict[str, Any]] = []
    lag_events: list[dict[str, Any]] = []
    takeover_events: list[dict[str, Any]] = []
    applied_event_seqs: list[int] = []
    applied_injection_ids: list[str] = []
    clock_after = round_clock

    with conn:
        for _, _, _, kind, payload in items:
            if kind == "event":
                event: GradientEvent = payload
                applied_event_seqs.append(event.event_seq)
                result = ingest_event(conn, event, round_no, round_clock)
                if result.get("lag"):
                    lag_events.append(result["lag"])
            else:
                injection: Injection = payload
                applied_injection_ids.append(injection.injection_id)
                result = apply_injection(conn, inputs, injection, round_no, injection.logic_clock_ms)
                clock_after = max(clock_after, injection.logic_clock_ms)
                lag_events.extend(result.get("lags", []))
                takeover_events.extend(result.get("takeovers", []))

        active_version = store.active_spec_version()
        spec_views = inputs.spec_versions()
        active_spec = spec_views[active_version]
        pending = pending_candidates(conn, active_version)
        eligible = [candidate for candidate in pending if _node_eligible(conn, candidate.node_id)]

        used_budget = 0
        started_count = 0
        budget_exhausted = False

        for ordinal, candidate in enumerate(eligible):
            cost = phase_cost_ms(candidate)
            decision = candidate.to_dict()
            decision["ordinal"] = ordinal
            decision["cost_ms"] = cost

            if budget_exhausted:
                outcome = OUTCOME_BUDGET
                detail = {"reason": "round_budget_already_exhausted"}
                completed_ms = None
            elif started_count >= concurrency:
                outcome = OUTCOME_CONCURRENCY_DEFERRED
                detail = {"reason": "max_concurrency_reached"}
                completed_ms = None
            elif used_budget + cost > budget:
                budget_exhausted = True
                outcome = OUTCOME_BUDGET
                detail = {"reason": "insufficient_remaining_budget", "remaining_budget_ms": budget - used_budget}
                completed_ms = None
            else:
                completed_ms = phase_completed_ms(candidate, round_clock)
                apply_candidate_completion(conn, active_spec, candidate, round_no, completed_ms)
                used_budget += cost
                started_count += 1
                clock_after = max(clock_after, completed_ms)
                outcome = OUTCOME_COMPLETED
                detail = {"first_submission_event_seq": candidate.event_seq}

            decision.update(
                {
                    "outcome_code": outcome,
                    "completed_ms": completed_ms,
                    "detail": detail,
                }
            )
            candidate_decisions.append(decision)
            conn.execute(
                "INSERT INTO round_candidates(round_no,ordinal,candidate_key,spec_version,node_id,"
                "layer,shard_index,phase,outcome_code,cost_ms,completed_ms,event_seq,detail_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    round_no, ordinal, candidate.key, candidate.spec_version, candidate.node_id,
                    candidate.layer, candidate.shard_index, candidate.phase, outcome, cost,
                    completed_ms, candidate.event_seq, canonical_text(detail),
                ),
            )

        memory_rows, memory_lags = audit_memory(conn, round_no, clock_after)
        lag_events.extend(memory_lags)

        node_outcomes = derive_node_outcomes(candidate_decisions, memory_lags, lag_events)
        for node_id, code in node_outcomes.items():
            conn.execute(
                "UPDATE node_state SET last_outcome_code=?, last_round_no=?, updated_clock=? WHERE node_id=?",
                (code, round_no, clock_after, node_id),
            )

        report = {
            "round_no": round_no,
            "status": "committed",
            "logic_clock_ms": round_clock,
            "clock_watermark_after_ms": clock_after,
            "active_spec_version": active_version,
            "stream_position": new_stream_position,
            "applied_event_seqs": applied_event_seqs,
            "applied_injection_ids": applied_injection_ids,
            "max_concurrency": concurrency,
            "time_budget_ms": budget,
            "used_budget_ms": used_budget,
            "started_count": started_count,
            "budget_exhausted": budget_exhausted,
            "pending_count": len(pending),
            "eligible_count": len(eligible),
            "candidates": candidate_decisions,
            "lag_events": lag_events,
            "takeover_events": takeover_events,
            "memory_ledger": [
                {key: value for key, value in row.items() if key != "entries"} | {"entries": row["entries"]}
                for row in memory_rows
            ],
        }
        report["digest"] = sha256_json({k: v for k, v in report.items() if k != "digest"})

        for ledger in memory_rows:
            conn.execute(
                "INSERT INTO memory_ledger(round_no,node_id,spec_versions,memory_bytes,"
                "memory_limit_bytes,excess_bytes,blocked,entries_json,digest)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    round_no, ledger["node_id"], canonical_text(ledger["spec_versions"]),
                    ledger["memory_bytes"], ledger["memory_limit_bytes"], ledger["excess_bytes"],
                    int(ledger["blocked"]), canonical_text(ledger["entries"]), ledger["digest"],
                ),
            )

        conn.execute(
            "UPDATE rounds SET status='committed', logic_clock_ms=?, active_spec_version=?,"
            "used_budget_ms=?, started_count=?, report_json=?, report_digest=? WHERE round_no=?",
            (
                round_clock, active_version, used_budget, started_count,
                canonical_text(report), report["digest"], round_no,
            ),
        )
        store.set_meta("stream_position", new_stream_position)
        store.set_meta("clock_watermark", clock_after)
        store.set_meta("last_committed_round", round_no)

        _maybe_crash(crash, "before_commit_round", round_no)

    _maybe_crash(crash, "after_commit_round", round_no)
    return report


def _maybe_crash(crash: dict[str, int] | None, point: str, round_no: int) -> None:
    if crash and crash.get(point) == round_no:
        os._exit(86)


def phase_cost_ms(candidate: Candidate) -> int:
    total = candidate.duration_ms
    if total is None:
        total = max(1, candidate.expected_commit_ms - candidate.logic_clock_ms)
    total = max(1, int(total))
    reduce_cost = max(1, math.ceil(total / 2))
    gather_cost = max(1, total - reduce_cost)
    if candidate.phase == "reduce_scatter":
        return reduce_cost
    return gather_cost


def phase_completed_ms(candidate: Candidate, round_clock: int) -> int:
    total = candidate.duration_ms
    if total is None:
        total = max(1, candidate.expected_commit_ms - candidate.logic_clock_ms)
    total = max(1, int(total))
    reduce_cost = max(1, math.ceil(total / 2))
    if candidate.phase == "reduce_scatter":
        planned = candidate.logic_clock_ms + reduce_cost
    else:
        gather_cost = max(1, total - reduce_cost)
        planned = max(
            candidate.expected_commit_ms,
            candidate.logic_clock_ms + reduce_cost + gather_cost,
        )
    return max(round_clock, planned)


def ingest_event(conn: Any, event: GradientEvent, round_no: int, clock_ms: int) -> dict[str, Any]:
    active_version = int(
        conn.execute("SELECT value FROM meta WHERE key='active_spec_version'").fetchone()["value"]
    )
    existing = conn.execute(
        "SELECT event_seq FROM reports WHERE spec_version=? AND node_id=? AND layer=? AND shard_index=?",
        (event.spec_version, event.node_id, event.layer, event.shard_index),
    ).fetchone()
    if existing is not None:
        return {"duplicate": True, "first_event_seq": existing["event_seq"]}

    if event.spec_version != active_version:
        lag = insert_lag(
            conn,
            round_no=round_no,
            clock_ms=clock_ms,
            code=OUTCOME_STALE_SPEC,
            node_id=event.node_id,
            spec_version=event.spec_version,
            layer=event.layer,
            shard_index=event.shard_index,
            candidate_key=f"v{event.spec_version}/{event.node_id}/{event.layer}#{event.shard_index}",
            detail={"active_spec_version": active_version, "event_seq": event.event_seq},
        )
        return {"lag": lag}

    conn.execute(
        "INSERT INTO reports(spec_version,node_id,layer,shard_index,event_seq,round_no,committed_clock)"
        " VALUES(?,?,?,?,?,?,?)",
        (
            event.spec_version, event.node_id, event.layer, event.shard_index,
            event.event_seq, round_no, clock_ms,
        ),
    )
    faulty = conn.execute("SELECT faulty FROM node_state WHERE node_id=?", (event.node_id,)).fetchone()["faulty"]
    if not faulty:
        byte_size = _shard_bytes(conn, event.spec_version, event.layer, event.shard_index)
        upsert_holding(
            conn,
            spec_version=event.spec_version,
            node_id=event.node_id,
            layer=event.layer,
            shard_index=event.shard_index,
            kind="source",
            byte_size=byte_size,
            round_no=round_no,
            clock_ms=clock_ms,
        )
    return {"accepted": True}


def _shard_bytes(conn: Any, spec_version: int, layer: str, shard_index: int) -> int:
    row = conn.execute("SELECT spec_json FROM specs WHERE version=?", (spec_version,)).fetchone()
    spec = json_loads(row["spec_json"])
    for layer_spec in spec["layers"]:
        if layer_spec["name"] == layer:
            for shard in layer_spec["shards"]:
                if shard["index"] == shard_index:
                    return int(shard["byte_size"])
    raise EngineError(ErrorCode.DB_INVALID, "persisted spec is missing referenced shard")


def json_loads(text: str) -> Any:
    import json

    return json.loads(text)


def upsert_holding(
    conn: Any,
    *,
    spec_version: int,
    node_id: str,
    layer: str,
    shard_index: int,
    kind: str,
    byte_size: int,
    round_no: int,
    clock_ms: int,
) -> None:
    conn.execute(
        """
        INSERT INTO holdings(spec_version,node_id,layer,shard_index,kind,byte_size,active,
                             round_no,updated_clock)
        VALUES(?,?,?,?,?,?,1,?,?)
        ON CONFLICT(spec_version,node_id,layer,shard_index,kind)
        DO UPDATE SET active=1, byte_size=excluded.byte_size, round_no=excluded.round_no,
                      updated_clock=excluded.updated_clock, lag_code=NULL
        """,
        (spec_version, node_id, layer, shard_index, kind, byte_size, round_no, clock_ms),
    )


def deactivate_holdings(
    conn: Any,
    *,
    spec_version: int,
    node_id: str,
    layer: str,
    shard_index: int,
    kinds: tuple[str, ...],
    code: str,
) -> None:
    placeholders = ",".join("?" for _ in kinds)
    conn.execute(
        f"UPDATE holdings SET active=0, lag_code=? WHERE spec_version=? AND node_id=? "
        f"AND layer=? AND shard_index=? AND active=1 AND kind IN ({placeholders})",
        (code, spec_version, node_id, layer, shard_index, *kinds),
    )


def apply_candidate_completion(
    conn: Any,
    spec: Any,
    candidate: Candidate,
    round_no: int,
    completed_ms: int,
) -> None:
    byte_size = spec.layer(candidate.layer).shard_bytes(candidate.shard_index)
    conn.execute(
        "INSERT OR IGNORE INTO phase_completions(spec_version,node_id,layer,shard_index,phase,"
        "event_seq,round_no,completed_ms) VALUES(?,?,?,?,?,?,?,?)",
        (
            candidate.spec_version, candidate.node_id, candidate.layer, candidate.shard_index,
            candidate.phase, candidate.event_seq, round_no, completed_ms,
        ),
    )

    key_args = dict(
        spec_version=candidate.spec_version,
        node_id=candidate.node_id,
        layer=candidate.layer,
        shard_index=candidate.shard_index,
    )
    if candidate.phase == "reduce_scatter":
        deactivate_holdings(conn, **key_args, kinds=("source",), code="PROGRESSED_TO_REDUCE_SCATTER")
        upsert_holding(
            conn, **key_args, kind="reduced", byte_size=byte_size,
            round_no=round_no, clock_ms=completed_ms,
        )
    else:
        deactivate_holdings(
            conn, **key_args, kinds=("source", "reduced", "takeover"),
            code="PROGRESSED_TO_ALL_GATHER",
        )
        upsert_holding(
            conn, **key_args, kind="gathered", byte_size=byte_size,
            round_no=round_no, clock_ms=completed_ms,
        )

    participant_count = conn.execute(
        "SELECT COUNT(*) AS n FROM reports WHERE spec_version=? AND node_id IS NOT NULL "
        "AND layer=? AND shard_index=?",
        (candidate.spec_version, candidate.layer, candidate.shard_index),
    ).fetchone()["n"]
    completed_count = conn.execute(
        "SELECT COUNT(*) AS n, MAX(completed_ms) AS completed_ms FROM phase_completions "
        "WHERE spec_version=? AND layer=? AND shard_index=? AND phase=?",
        (candidate.spec_version, candidate.layer, candidate.shard_index, candidate.phase),
    ).fetchone()
    if participant_count > 0 and completed_count["n"] >= participant_count:
        conn.execute(
            "INSERT OR IGNORE INTO global_phase_completions(spec_version,layer,shard_index,phase,"
            "round_no,completed_ms,participant_count) VALUES(?,?,?,?,?,?,?)",
            (
                candidate.spec_version, candidate.layer, candidate.shard_index, candidate.phase,
                round_no, completed_count["completed_ms"] or completed_ms, participant_count,
            ),
        )
    update_cursor(conn, spec, candidate.layer, candidate.phase, round_no)


def update_cursor(conn: Any, spec: Any, layer_name: str, phase: str, round_no: int) -> None:
    layer = spec.layer(layer_name)
    next_index = 0
    for shard in layer.shards:
        exists = conn.execute(
            "SELECT 1 FROM global_phase_completions WHERE spec_version=? AND layer=? "
            "AND shard_index=? AND phase=?",
            (spec.version, layer_name, shard.index, phase),
        ).fetchone()
        if exists and shard.index == next_index:
            next_index += 1
        elif not exists:
            break
    conn.execute(
        "INSERT INTO cursors(spec_version,layer,phase,next_index,round_no) VALUES(?,?,?,?,?) "
        "ON CONFLICT(spec_version,layer,phase) DO UPDATE SET next_index=excluded.next_index,"
        "round_no=excluded.round_no",
        (spec.version, layer_name, phase, next_index, round_no),
    )


def ensure_spec_cursors(conn: Any, spec: Any, round_no: int) -> None:
    for layer in spec.layers:
        for phase in ("reduce_scatter", "all_gather"):
            conn.execute(
                "INSERT OR IGNORE INTO cursors(spec_version,layer,phase,next_index,round_no)"
                " VALUES(?,?,?,0,?)",
                (spec.version, layer.name, phase, round_no),
            )


def insert_lag(conn: Any, **values: Any) -> dict[str, Any]:
    detail = values.get("detail") or {}
    cur = conn.execute(
        "INSERT INTO lag_records(round_no,logic_clock_ms,code,node_id,spec_version,layer,"
        "shard_index,candidate_key,detail_json) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            values["round_no"], values["clock_ms"], values["code"], values.get("node_id"),
            values.get("spec_version"), values.get("layer"), values.get("shard_index"),
            values.get("candidate_key"), canonical_text(detail),
        ),
    )
    return {
        "lag_id": cur.lastrowid,
        "round_no": values["round_no"],
        "code": values["code"],
        "node_id": values.get("node_id"),
        "spec_version": values.get("spec_version"),
        "layer": values.get("layer"),
        "shard_index": values.get("shard_index"),
        "candidate_key": values.get("candidate_key"),
        "detail": detail,
    }


def apply_injection(
    conn: Any,
    inputs: LoadedInputs,
    injection: Injection,
    round_no: int,
    clock_ms: int,
) -> dict[str, Any]:
    conn.execute(
        "UPDATE injections SET applied=1, applied_round=?, applied_clock=? WHERE order_index=?",
        (round_no, clock_ms, injection.order_index),
    )
    if injection.kind == "spec_replace":
        return apply_spec_replace(conn, inputs, injection, round_no, clock_ms)
    if injection.kind == "node_fault":
        return apply_node_fault(conn, inputs, injection, round_no, clock_ms)
    return apply_node_recover(conn, injection, round_no, clock_ms)


def apply_spec_replace(
    conn: Any,
    inputs: LoadedInputs,
    injection: Injection,
    round_no: int,
    clock_ms: int,
) -> dict[str, Any]:
    assert injection.spec is not None
    new_spec = injection.spec
    old_version = int(conn.execute("SELECT value FROM meta WHERE key='active_spec_version'").fetchone()["value"])
    conn.execute("UPDATE specs SET status='retired', retired_round=? WHERE version=?", (round_no, old_version))
    conn.execute(
        "UPDATE specs SET status='active' WHERE version=?",
        (new_spec.version,),
    )
    conn.execute(
        "UPDATE holdings SET active=0, lag_code='SPEC_RETIRED' WHERE spec_version=? AND active=1",
        (old_version,),
    )
    conn.execute(
        "UPDATE meta SET value=? WHERE key='active_spec_version'",
        (str(new_spec.version),),
    )
    ensure_spec_cursors(conn, new_spec, round_no)
    insert_lag(
        conn,
        round_no=round_no,
        clock_ms=clock_ms,
        code="SPEC_REPLACED",
        spec_version=new_spec.version,
        detail={"old_version": old_version, "injection_id": injection.injection_id},
    )
    return {"spec_replaced": True, "old_version": old_version, "new_version": new_spec.version}


def apply_node_fault(
    conn: Any,
    inputs: LoadedInputs,
    injection: Injection,
    round_no: int,
    clock_ms: int,
) -> dict[str, Any]:
    node_id = injection.node_id
    assert node_id is not None
    holdings = conn.execute(
        "SELECT spec_version,layer,shard_index,kind,byte_size FROM holdings "
        "WHERE node_id=? AND active=1 ORDER BY spec_version,layer,shard_index,kind",
        (node_id,),
    ).fetchall()
    lags: list[dict[str, Any]] = []
    takeovers: list[dict[str, Any]] = []

    for holding in holdings:
        confirmed = conn.execute(
            "SELECT 1 FROM phase_completions WHERE spec_version=? AND node_id=? AND layer=? "
            "AND shard_index=? AND phase='all_gather'",
            (holding["spec_version"], node_id, holding["layer"], holding["shard_index"]),
        ).fetchone()
        key_args = dict(
            spec_version=holding["spec_version"], layer=holding["layer"],
            shard_index=holding["shard_index"],
        )
        if confirmed:
            successor = choose_successor(conn, inputs, node_id)
            if successor is not None:
                already_held = conn.execute(
                    "SELECT 1 FROM holdings WHERE spec_version=? AND node_id=? AND layer=? "
                    "AND shard_index=? AND active=1 LIMIT 1",
                    (holding["spec_version"], successor, holding["layer"], holding["shard_index"]),
                ).fetchone()
                if not already_held:
                    upsert_holding(
                        conn,
                        spec_version=holding["spec_version"],
                        node_id=successor,
                        layer=holding["layer"],
                        shard_index=holding["shard_index"],
                        kind="takeover",
                        byte_size=holding["byte_size"],
                        round_no=round_no,
                        clock_ms=clock_ms,
                    )
                conn.execute(
                    "INSERT INTO takeovers(round_no,logic_clock_ms,injection_id,from_node_id,"
                    "to_node_id,spec_version,layer,shard_index,reason) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        round_no, clock_ms, injection.injection_id, node_id, successor,
                        holding["spec_version"], holding["layer"], holding["shard_index"],
                        "confirmed_shard_takeover" if not already_held else "replica_already_held",
                    ),
                )
                takeovers.append({"from_node_id": node_id, "to_node_id": successor, **key_args})
        else:
            lag = insert_lag(
                conn,
                round_no=round_no,
                clock_ms=clock_ms,
                code=OUTCOME_NODE_FAULT,
                node_id=node_id,
                spec_version=holding["spec_version"],
                layer=holding["layer"],
                shard_index=holding["shard_index"],
                candidate_key=(
                    f"v{holding['spec_version']}/{node_id}/{holding['layer']}"
                    f"#{holding['shard_index']}"
                ),
                detail={"holding_kind": holding["kind"], "rollback_to": "pending"},
            )
            lags.append(lag)

    conn.execute(
        "UPDATE holdings SET active=0, lag_code='NODE_FAULT_RESET' WHERE node_id=? AND active=1",
        (node_id,),
    )
    conn.execute(
        "UPDATE node_state SET faulty=1, memory_blocked=0, last_outcome_code=?, last_round_no=?,"
        "updated_clock=? WHERE node_id=?",
        (OUTCOME_NODE_FAULT, round_no, clock_ms, node_id),
    )
    return {"lags": lags, "takeovers": takeovers}


def choose_successor(conn: Any, inputs: LoadedInputs, failed_node: str) -> str | None:
    rows = conn.execute(
        """
        SELECT n.node_id, n.memory_limit_bytes,
               COALESCE(SUM(CASE WHEN h.active=1 THEN h.byte_size ELSE 0 END),0) AS memory_bytes,
               ns.faulty, ns.memory_blocked
          FROM nodes n
          JOIN node_state ns ON ns.node_id=n.node_id
          LEFT JOIN holdings h ON h.node_id=n.node_id
         WHERE n.node_id != ?
         GROUP BY n.node_id
         ORDER BY ns.faulty ASC, ns.memory_blocked ASC,
                  (n.memory_limit_bytes - memory_bytes) DESC, n.node_id ASC
        """,
        (failed_node,),
    ).fetchall()
    healthy = [row for row in rows if not row["faulty"] and not row["memory_blocked"]]
    if healthy:
        return healthy[0]["node_id"]
    nonfaulty = [row for row in rows if not row["faulty"]]
    return nonfaulty[0]["node_id"] if nonfaulty else None


def apply_node_recover(conn: Any, injection: Injection, round_no: int, clock_ms: int) -> dict[str, Any]:
    node_id = injection.node_id
    assert node_id is not None
    conn.execute(
        "UPDATE node_state SET faulty=0, memory_blocked=0, last_outcome_code='NODE_RECOVERED',"
        "last_round_no=?, updated_clock=? WHERE node_id=?",
        (round_no, clock_ms, node_id),
    )
    conn.execute(
        "UPDATE lag_records SET resolved=1 WHERE node_id=? AND code=? AND resolved=0",
        (node_id, OUTCOME_NODE_FAULT),
    )
    return {"node_recovered": node_id}


def audit_memory(conn: Any, round_no: int, clock_ms: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    node_rows = conn.execute("SELECT * FROM nodes ORDER BY node_id").fetchall()
    ledger_rows: list[dict[str, Any]] = []
    lag_events: list[dict[str, Any]] = []

    for node in node_rows:
        node_id = node["node_id"]
        limit = node["memory_limit_bytes"]
        holding_rows = conn.execute(
            "SELECT spec_version,layer,shard_index,kind,byte_size,lag_code FROM holdings "
            "WHERE node_id=? AND active=1 "
            "ORDER BY spec_version, layer, shard_index, kind",
            (node_id,),
        ).fetchall()
        entries = [
            {
                "spec_version": row["spec_version"],
                "layer": row["layer"],
                "shard_index": row["shard_index"],
                "kind": row["kind"],
                "byte_size": row["byte_size"],
                "lag_code": row["lag_code"],
            }
            for row in holding_rows
        ]
        memory_bytes = sum(entry["byte_size"] for entry in entries)
        excess_bytes = max(0, memory_bytes - limit)
        blocked = excess_bytes > 0
        spec_versions = sorted({entry["spec_version"] for entry in entries})

        cumulative = 0
        for entry, row in zip(entries, holding_rows):
            cumulative += entry["byte_size"]
            if cumulative > limit and entry["lag_code"] != OUTCOME_MEMORY_LAG:
                conn.execute(
                    "UPDATE holdings SET lag_code=? WHERE spec_version=? AND node_id=? "
                    "AND layer=? AND shard_index=? AND kind=?",
                    (
                        OUTCOME_MEMORY_LAG, entry["spec_version"], node_id, entry["layer"],
                        entry["shard_index"], entry["kind"],
                    ),
                )
                lag = insert_lag(
                    conn,
                    round_no=round_no,
                    clock_ms=clock_ms,
                    code=OUTCOME_MEMORY_LAG,
                    node_id=node_id,
                    spec_version=entry["spec_version"],
                    layer=entry["layer"],
                    shard_index=entry["shard_index"],
                    candidate_key=(
                        f"v{entry['spec_version']}/{node_id}/{entry['layer']}"
                        f"#{entry['shard_index']}/{entry['kind']}"
                    ),
                    detail={
                        "byte_size": entry["byte_size"],
                        "cumulative_bytes": cumulative,
                        "memory_limit_bytes": limit,
                        "excess_bytes": excess_bytes,
                    },
                )
                lag_events.append(lag)

        state = conn.execute("SELECT memory_blocked,faulty FROM node_state WHERE node_id=?", (node_id,)).fetchone()
        if blocked:
            conn.execute(
                "UPDATE node_state SET memory_blocked=1, last_outcome_code=?, last_round_no=? "
                "WHERE node_id=?",
                (OUTCOME_MEMORY_LAG, round_no, node_id),
            )
        elif state["memory_blocked"]:
            conn.execute(
                "UPDATE node_state SET memory_blocked=0, last_outcome_code='MEMORY_PRESSURE_RESOLVED',"
                "last_round_no=? WHERE node_id=?",
                (round_no, node_id),
            )

        ledger = {
            "round_no": round_no,
            "node_id": node_id,
            "spec_versions": spec_versions,
            "memory_bytes": memory_bytes,
            "memory_limit_bytes": limit,
            "excess_bytes": excess_bytes,
            "blocked": blocked,
            "entries": entries,
        }
        ledger["digest"] = sha256_json(
            {key: value for key, value in ledger.items() if key != "digest"}
        )
        ledger_rows.append(ledger)

    return ledger_rows, lag_events


def derive_node_outcomes(
    candidates: list[dict[str, Any]],
    memory_lags: list[dict[str, Any]],
    all_lags: list[dict[str, Any]],
) -> dict[str, str]:
    priority = {
        OUTCOME_MEMORY_LAG: 4,
        OUTCOME_NODE_FAULT: 5,
        OUTCOME_BUDGET: 3,
        OUTCOME_STALE_SPEC: 4,
        OUTCOME_COMPLETED: 1,
        OUTCOME_CONCURRENCY_DEFERRED: 0,
    }
    result: dict[str, str] = {}
    for decision in candidates:
        node_id = decision["node_id"]
        code = decision["outcome_code"]
        if priority.get(code, 0) > priority.get(result.get(node_id, ""), -1):
            result[node_id] = code
    for lag in all_lags:
        node_id = lag.get("node_id")
        code = lag.get("code")
        if node_id and code in priority and priority[code] > priority.get(result.get(node_id, ""), -1):
            result[node_id] = code
    return result


def verify_replay(store: Store, inputs: LoadedInputs | None = None) -> dict[str, Any]:
    if inputs is None:
        inputs = store.load_inputs_from_db()
    recover_store(store)
    expected_store = Store.connect(":memory:")
    try:
        expected_store.initialize(inputs)
        run_to_completion(expected_store, inputs)
        expected_trace = expected_store.canonical_trace()
        expected_ledger = expected_store.ledger_summaries()
    finally:
        expected_store.close()

    actual_trace = store.canonical_trace()
    actual_ledger = store.ledger_summaries()

    mismatch = find_trace_mismatch(expected_trace, actual_trace)
    ledger_mismatch = find_ledger_mismatch(expected_ledger, actual_ledger)
    ok = mismatch is None and ledger_mismatch is None
    result: dict[str, Any] = {
        "ok": ok,
        "verified_rounds": min(len(expected_trace), len(actual_trace)),
        "expected_round_count": len(expected_trace),
        "actual_round_count": len(actual_trace),
        "expected_ledger_entries": len(expected_ledger),
        "actual_ledger_entries": len(actual_ledger),
        "trace_digest": sha256_json(actual_trace),
        "ledger_digest": sha256_json(actual_ledger),
    }
    if mismatch is not None:
        result.update(mismatch)
    elif ledger_mismatch is not None:
        round_no = ledger_mismatch.get("first_mismatch_round")
        expected_round = next((r for r in expected_trace if r.get("round_no") == round_no), None)
        actual_round = next((r for r in actual_trace if r.get("round_no") == round_no), None)
        ledger_mismatch["first_mismatch_event_seq"] = _first_event_seq(expected_round, actual_round)
        result.update(ledger_mismatch)
    return result


def find_trace_mismatch(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(expected) != len(actual):
        index = min(len(expected), len(actual))
        expected_round = expected[index] if index < len(expected) else None
        actual_round = actual[index] if index < len(actual) else None
        return {
            "reason": "round_count_mismatch",
            "first_mismatch_round": index + 1,
            "first_mismatch_event_seq": _first_event_seq(expected_round, actual_round),
            "expected_round_digest": expected_round.get("digest") if expected_round else None,
            "actual_round_digest": actual_round.get("digest") if actual_round else None,
            "expected_ledger_summary": _round_ledger_digest(expected_round),
            "actual_ledger_summary": _round_ledger_digest(actual_round),
        }
    for index, (expected_round, actual_round) in enumerate(zip(expected, actual)):
        if canonical_text(expected_round) != canonical_text(actual_round):
            return {
                "reason": "round_decision_mismatch",
                "first_mismatch_round": expected_round.get("round_no", index + 1),
                "first_mismatch_event_seq": _first_event_seq(expected_round, actual_round),
                "expected_round_digest": expected_round.get("digest"),
                "actual_round_digest": actual_round.get("digest"),
                "expected_ledger_summary": _round_ledger_digest(expected_round),
                "actual_ledger_summary": _round_ledger_digest(actual_round),
            }
    return None


def find_ledger_mismatch(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> dict[str, Any] | None:
    expected_map = {(row["round_no"], row["node_id"]): row for row in expected}
    actual_map = {(row["round_no"], row["node_id"]): row for row in actual}
    keys = sorted(set(expected_map) | set(actual_map))
    for key in keys:
        expected_row = expected_map.get(key)
        actual_row = actual_map.get(key)
        if expected_row is None or actual_row is None or expected_row.get("digest") != actual_row.get("digest"):
            return {
                "reason": "memory_ledger_mismatch",
                "first_mismatch_round": key[0],
                "first_mismatch_event_seq": None,
                "expected_ledger_summary": _ledger_summary(expected_row),
                "actual_ledger_summary": _ledger_summary(actual_row),
            }
    return None


def _first_event_seq(*rounds: dict[str, Any] | None) -> int | None:
    for round_report in rounds:
        if not round_report:
            continue
        events = round_report.get("applied_event_seqs") or []
        if events:
            return int(events[0])
        for candidate in round_report.get("candidates") or []:
            if candidate.get("event_seq") is not None:
                return int(candidate["event_seq"])
    return None


def _round_ledger_digest(round_report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not round_report:
        return None
    return {
        "round_no": round_report.get("round_no"),
        "ledgers": [
            {
                "node_id": item.get("node_id"),
                "memory_bytes": item.get("memory_bytes"),
                "excess_bytes": item.get("excess_bytes"),
                "digest": item.get("digest"),
            }
            for item in round_report.get("memory_ledger", [])
        ],
    }


def _ledger_summary(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "round_no": row.get("round_no"),
        "node_id": row.get("node_id"),
        "memory_bytes": row.get("memory_bytes"),
        "memory_limit_bytes": row.get("memory_limit_bytes"),
        "excess_bytes": row.get("excess_bytes"),
        "blocked": row.get("blocked"),
        "digest": row.get("digest"),
    }
