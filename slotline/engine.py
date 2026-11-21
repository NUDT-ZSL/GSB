from __future__ import annotations

import copy
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .cache import CacheStore
from .canonical import canonical_json_bytes, sha256_json
from .config import config_fingerprint, normalize_config
from .errors import (
    BusinessError,
    E_LINEAGE_BROKEN,
    E_OPERATION_ORDER,
    E_PENDING_SLOT_MISSING,
    E_THRESHOLD_CONFLICT,
    E_TURN_ALREADY_RETRACTED,
    E_TURN_NOT_FOUND,
)
from .lineage import LineageTracer
from .merge import merge_slot, public_candidate
from .models import Session, Turn
from .scoring import score_turn
from .snapshots import SnapshotStore


@dataclass
class BuildOutcome:
    state: dict[str, Any]
    state_bytes: bytes
    state_hash: str
    graph: dict[str, Any]
    computed_nodes: list[str]
    reused_nodes: list[str]
    node_keys: dict[str, str]
    cache_status: dict[str, str]
    snapshot: dict[str, Any]

    def snapshot_summary(self) -> dict[str, Any]:
        return {
            "version": self.snapshot["version"],
            "checksum": self.snapshot["checksum"],
            "cache_entries": len(self.snapshot["cache_manifest"]),
        }


class Engine:
    """Deterministic DAG engine for slot state, invalidation and lineage."""

    def __init__(
        self,
        session: Session,
        *,
        config: dict[str, Any] | None = None,
        runtime_dir: str | Path = ".runtime",
    ) -> None:
        self.session = session
        self.config = normalize_config(config)
        runtime = Path(runtime_dir)
        self.cache = CacheStore(runtime / "cache")
        self.snapshots = SnapshotStore(runtime / "snapshots", self.cache)
        self.retracted: set[str] = set()
        self._lock = threading.RLock()
        self._last_outcome: BuildOutcome | None = None
        self._last_operation: dict[str, Any] | None = None
        self.operation_history: deque[dict[str, Any]] = deque(maxlen=20)
        self.recent_errors: deque[dict[str, Any]] = deque(maxlen=20)
        self._error_sequence = 0

    # ------------------------------------------------------------------
    # Public state
    # ------------------------------------------------------------------
    @property
    def state(self) -> dict[str, Any]:
        if self._last_outcome is None:
            self.build()
        assert self._last_outcome is not None
        return self._last_outcome.state

    @property
    def graph(self) -> dict[str, Any]:
        if self._last_outcome is None:
            self.build()
        assert self._last_outcome is not None
        return self._last_outcome.graph

    @property
    def last_operation(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._last_operation)

    def active_turns(self) -> list[Turn]:
        return [
            turn
            for turn in self.session.ordered_turns()
            if turn.turn_id not in self.retracted
        ]

    def _record_error(self, error: BusinessError) -> None:
        self._error_sequence += 1
        record = {"seq": self._error_sequence}
        record.update(error.to_dict())
        self.recent_errors.appendleft(record)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------
    def build(self) -> BuildOutcome:
        with self._lock:
            try:
                outcome = self._build_internal()
                self._last_outcome = outcome
                return outcome
            except BusinessError as exc:
                self._record_error(exc)
                raise

    def _build_internal(self) -> BuildOutcome:
        active_turns = self.active_turns()
        computed_nodes: list[str] = []
        reused_nodes: list[str] = []
        node_keys: dict[str, str] = {}
        cache_status: dict[str, str] = {}
        nodes: list[dict[str, Any]] = []

        def remember(
            *,
            node_id: str,
            stage: str,
            label: str,
            key: str,
            input_digest: str,
            upstream: list[dict[str, str]],
            hit: bool,
        ) -> None:
            node_keys[node_id] = key
            cache_status[node_id] = "reused" if hit else "computed"
            if hit:
                reused_nodes.append(node_id)
            else:
                computed_nodes.append(node_id)
            nodes.append(
                {
                    "id": node_id,
                    "stage": stage,
                    "label": label,
                    "cache_key": key,
                    "input_digest": input_digest,
                    "upstream": [item["node"] for item in upstream],
                    "cache_status": cache_status[node_id],
                }
            )

        score_by_turn: dict[str, dict[str, Any]] = {}
        for turn in active_turns:
            if turn.role != "user":
                continue
            node_id = f"score:{turn.turn_id}"
            input_value = turn.to_dict()
            input_digest = sha256_json(input_value)
            result = self.cache.get_or_compute(
                stage="score",
                node_id=node_id,
                input_digest=input_digest,
                config=self.config,
                upstream=[],
                compute=lambda turn=turn: score_turn(turn, self.config),
            )
            annotated_candidates = [
                {
                    **candidate,
                    "origin_score_node": node_id,
                    "origin_score_cache_key": result.key,
                }
                for candidate in result.payload["candidates"]
            ]
            score_by_turn[turn.turn_id] = {
                "turn": turn,
                "node": node_id,
                "key": result.key,
                "candidates": annotated_candidates,
            }
            remember(
                node_id=node_id,
                stage="score",
                label=f"解析/候选打分 · {turn.turn_id}",
                key=result.key,
                input_digest=input_digest,
                upstream=[],
                hit=result.hit,
            )

        latest_merge: dict[str, dict[str, Any]] = {}
        merge_chains: dict[str, list[dict[str, str]]] = {}
        for turn in active_turns:
            if turn.role != "user":
                continue
            grouped: dict[str, list[dict[str, Any]]] = {}
            for candidate in score_by_turn[turn.turn_id]["candidates"]:
                grouped.setdefault(candidate["key"], []).append(candidate)
            for slot_key in sorted(grouped):
                current_candidates = grouped[slot_key]
                previous = latest_merge.get(slot_key)
                node_id = f"merge:{turn.turn_id}:{slot_key}"
                upstream: list[dict[str, str]] = [
                    {
                        "node": score_by_turn[turn.turn_id]["node"],
                        "key": score_by_turn[turn.turn_id]["key"],
                    }
                ]
                if previous:
                    upstream.append(
                        {"node": previous["node"], "key": previous["key"]}
                    )
                input_value = {
                    "slot_key": slot_key,
                    "turn_id": turn.turn_id,
                    "turn_index": turn.index,
                    "current_candidates": current_candidates,
                    "previous_merge_cache_key": (
                        previous["key"] if previous else None
                    ),
                }
                input_digest = sha256_json(input_value)
                previous_payload = previous["payload"] if previous else None
                result = self.cache.get_or_compute(
                    stage="merge",
                    node_id=node_id,
                    input_digest=input_digest,
                    config=self.config,
                    upstream=upstream,
                    compute=lambda slot_key=slot_key,
                    current_candidates=current_candidates,
                    previous_payload=previous_payload,
                    turn=turn,
                    node_id=node_id,
                    previous=previous: merge_slot(
                        slot_key,
                        previous_payload,
                        current_candidates,
                        turn_id=turn.turn_id,
                        turn_index=turn.index,
                        score_node=score_by_turn[turn.turn_id]["node"],
                        score_cache_key=score_by_turn[turn.turn_id]["key"],
                        previous_node=previous["node"] if previous else None,
                        previous_cache_key=previous["key"] if previous else None,
                    ),
                )
                latest_merge[slot_key] = {
                    "node": node_id,
                    "key": result.key,
                    "payload": result.payload,
                }
                merge_chains.setdefault(slot_key, []).append(
                    {"node": node_id, "key": result.key}
                )
                remember(
                    node_id=node_id,
                    stage="merge",
                    label=f"槽位合并 · {turn.turn_id} · {slot_key}",
                    key=result.key,
                    input_digest=input_digest,
                    upstream=upstream,
                    hit=result.hit,
                )

        requests: list[dict[str, Any]] = []
        for turn in active_turns:
            if turn.role != "assistant":
                continue
            for request in turn.clarifications:
                requests.append(
                    {
                        "turn_id": turn.turn_id,
                        "turn_index": turn.index,
                        "slot": request.slot,
                        "reason": request.reason,
                    }
                )
        requests.sort(key=lambda item: (item["turn_index"], item["slot"], item["reason"]))
        requested_slots = sorted({item["slot"] for item in requests})
        pending_upstream: list[dict[str, str]] = []
        for slot_key in requested_slots:
            if slot_key in latest_merge:
                pending_upstream.append(
                    {
                        "node": latest_merge[slot_key]["node"],
                        "key": latest_merge[slot_key]["key"],
                    }
                )
        pending_input = {
            "requests": requests,
            "clarification_threshold": self.config["clarification_threshold"],
        }
        pending_digest = sha256_json(pending_input)
        pending_result = self.cache.get_or_compute(
            stage="pending",
            node_id="pending",
            input_digest=pending_digest,
            config=self.config,
            upstream=pending_upstream,
            compute=lambda: self._compute_pending(
                requested_slots=requested_slots,
                requests=requests,
                merges=latest_merge,
            ),
        )
        remember(
            node_id="pending",
            stage="pending",
            label="待澄清集合",
            key=pending_result.key,
            input_digest=pending_digest,
            upstream=pending_upstream,
            hit=pending_result.hit,
        )

        slots: dict[str, dict[str, Any]] = {}
        for slot_key in sorted(latest_merge):
            merge = latest_merge[slot_key]
            winner = merge["payload"]["winner"]
            slots[slot_key] = {
                "key": slot_key,
                "value": winner["value"],
                "confidence": winner["confidence"],
                "score": winner["score"],
                "source_turn": winner["source_turn"],
                "extractor_version": winner["extractor_version"],
                "conflict": merge["payload"]["conflict"],
                "conflict_reasons": merge["payload"]["conflict_reasons"],
                "merge_node": merge["node"],
                "merge_cache_key": merge["key"],
                "score_node": winner["origin_score_node"],
                "score_cache_key": winner["origin_score_cache_key"],
                "merge_chain": merge_chains[slot_key],
                "config_fingerprint": config_fingerprint(self.config),
                "extractor_versions": self.config["extractor_versions"],
                "clarification_threshold": self.config[
                    "clarification_threshold"
                ],
            }

        summary_base = {
            "schema_version": 1,
            "session_id": self.session.session_id,
            "active_turn_ids": [turn.turn_id for turn in active_turns],
            "slots": slots,
            "pending": pending_result.payload["items"],
            "pending_slots": pending_result.payload["slots"],
            "extractor_versions": self.config["extractor_versions"],
            "clarification_threshold": self.config["clarification_threshold"],
            "config_fingerprint": config_fingerprint(self.config),
        }
        summary_upstream = [
            {"node": latest_merge[slot_key]["node"], "key": latest_merge[slot_key]["key"]}
            for slot_key in sorted(latest_merge)
        ]
        summary_upstream.append({"node": "pending", "key": pending_result.key})
        summary_digest = sha256_json(summary_base)
        summary_result = self.cache.get_or_compute(
            stage="summary",
            node_id="summary",
            input_digest=summary_digest,
            config=self.config,
            upstream=summary_upstream,
            compute=lambda: copy.deepcopy(summary_base),
        )
        remember(
            node_id="summary",
            stage="summary",
            label="状态摘要",
            key=summary_result.key,
            input_digest=summary_digest,
            upstream=summary_upstream,
            hit=summary_result.hit,
        )

        edges = []
        for node in nodes:
            for upstream_node in node["upstream"]:
                edges.append({"from": upstream_node, "to": node["id"]})
        graph = {
            "nodes": nodes,
            "edges": edges,
            "digest": sha256_json(
                {
                    "nodes": [
                        {
                            "id": node["id"],
                            "stage": node["stage"],
                            "cache_key": node["cache_key"],
                            "upstream": node["upstream"],
                        }
                        for node in nodes
                    ],
                    "edges": edges,
                }
            ),
        }

        state = summary_result.payload
        state_bytes = canonical_json_bytes(state)
        snapshot = self.snapshots.commit(
            config=self.config,
            cache_keys=node_keys.values(),
        )
        return BuildOutcome(
            state=state,
            state_bytes=state_bytes,
            state_hash=sha256_json(state),
            graph=graph,
            computed_nodes=computed_nodes,
            reused_nodes=reused_nodes,
            node_keys=node_keys,
            cache_status=cache_status,
            snapshot=snapshot,
        )

    def _compute_pending(
        self,
        *,
        requested_slots: list[str],
        requests: list[dict[str, Any]],
        merges: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        threshold = float(self.config["clarification_threshold"])
        items: list[dict[str, Any]] = []
        for slot_key in requested_slots:
            slot_requests = [
                request for request in requests if request["slot"] == slot_key
            ]
            if slot_key not in merges:
                raise BusinessError(
                    E_PENDING_SLOT_MISSING,
                    "clarification request references a missing slot",
                    slot=slot_key,
                    node="pending",
                    details={
                        "request_turns": [
                            request["turn_id"] for request in slot_requests
                        ]
                    },
                )
            merge = merges[slot_key]
            winner = merge["payload"]["winner"]
            if float(winner["confidence"]) >= threshold:
                raise BusinessError(
                    E_THRESHOLD_CONFLICT,
                    "clarification slot winner is not below the threshold",
                    slot=slot_key,
                    node="pending",
                    details={
                        "winner_confidence": winner["confidence"],
                        "threshold": threshold,
                        "request_turns": [
                            request["turn_id"] for request in slot_requests
                        ],
                    },
                )
            items.append(
                {
                    "slot": slot_key,
                    "winner": public_candidate(winner),
                    "winner_confidence": winner["confidence"],
                    "threshold": threshold,
                    "merge_node": merge["node"],
                    "merge_cache_key": merge["key"],
                    "requests": slot_requests,
                }
            )
        return {
            "threshold": threshold,
            "slots": requested_slots,
            "items": items,
        }

    # ------------------------------------------------------------------
    # Incremental operations
    # ------------------------------------------------------------------
    def _turn_for_retract(self, turn_id: str) -> Turn:
        if turn_id in self.retracted:
            raise BusinessError(
                E_TURN_ALREADY_RETRACTED,
                "turn has already been retracted",
                node=turn_id,
                details={"turn_id": turn_id},
            )
        for turn in self.session.ordered_turns():
            if turn.turn_id == turn_id:
                return turn
        raise BusinessError(
            E_TURN_NOT_FOUND,
            "turn does not exist in this session",
            node=turn_id,
            details={"turn_id": turn_id},
        )

    @staticmethod
    def _affected_slots(turn: Turn) -> list[str]:
        if turn.role == "user":
            return sorted({candidate.key for candidate in turn.candidates})
        return sorted({request.slot for request in turn.clarifications})

    def _affected_graph_nodes(
        self,
        *,
        affected_slots: Iterable[str],
        start_index: int | None,
    ) -> list[str]:
        affected = set(affected_slots)
        result: set[str] = {"pending", "summary"}
        for turn in self.session.ordered_turns():
            if start_index is not None and turn.index < start_index:
                continue
            if turn.role != "user":
                continue
            for slot_key in sorted({candidate.key for candidate in turn.candidates}):
                if slot_key in affected:
                    result.add(f"merge:{turn.turn_id}:{slot_key}")
        return sorted(result)

    def _old_node_ids(self) -> set[str]:
        if self._last_outcome is None:
            return set()
        return {node["id"] for node in self._last_outcome.graph["nodes"]}

    def _finish_operation(
        self,
        *,
        operation: str,
        affected_slots: list[str],
        outcome: BuildOutcome,
        old_node_ids: set[str],
        start_index: int | None,
        turn_id: str | None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current_ids = {node["id"] for node in outcome.graph["nodes"]}
        result = {
            "operation": operation,
            "turn_id": turn_id,
            "affected_slots": sorted(affected_slots),
            "recomputed_slots": sorted(affected_slots),
            "affected_graph_nodes": self._affected_graph_nodes(
                affected_slots=affected_slots,
                start_index=start_index,
            ),
            "recomputed_nodes": list(outcome.computed_nodes),
            "reused_nodes": list(outcome.reused_nodes),
            "removed_nodes": sorted(old_node_ids - current_ids),
            "state_hash": outcome.state_hash,
            "snapshot": outcome.snapshot_summary(),
            "declared_simultaneous_order": list(
                self.config["simultaneous_order"]
            ),
        }
        if extra:
            result.update(extra)
        self._last_operation = result
        self.operation_history.appendleft(copy.deepcopy(result))
        return result

    def retract(self, turn_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                turn = self._turn_for_retract(turn_id)
                affected_slots = self._affected_slots(turn)
                old_node_ids = self._old_node_ids()
                old_session = self.session
                old_retracted = set(self.retracted)
                self.retracted.add(turn_id)
                try:
                    outcome = self._build_internal()
                except BusinessError:
                    self.session = old_session
                    self.retracted = old_retracted
                    raise
                self._last_outcome = outcome
                return self._finish_operation(
                    operation="retract",
                    affected_slots=affected_slots,
                    outcome=outcome,
                    old_node_ids=old_node_ids,
                    start_index=turn.index,
                    turn_id=turn_id,
                )
            except BusinessError as exc:
                self._record_error(exc)
                raise

    def append_turn(self, turn: Turn) -> dict[str, Any]:
        with self._lock:
            try:
                if turn.turn_id in self.retracted:
                    raise BusinessError(
                        E_TURN_ALREADY_RETRACTED,
                        "cannot append a retracted turn",
                        node=turn.turn_id,
                    )
                old_node_ids = self._old_node_ids()
                affected_slots = self._affected_slots(turn)
                old_session = self.session
                self.session = self.session.append_turn(turn)
                try:
                    outcome = self._build_internal()
                except BusinessError:
                    self.session = old_session
                    raise
                self._last_outcome = outcome
                committed_turn_ids = {
                    existing.turn_id
                    for existing in old_session.turns
                    if existing.turn_id not in self.retracted
                }
                committed_rescored = sorted(
                    node_id
                    for node_id in outcome.computed_nodes
                    if node_id.startswith("score:")
                    and node_id.split(":", 1)[1] in committed_turn_ids
                )
                return self._finish_operation(
                    operation="append",
                    affected_slots=affected_slots,
                    outcome=outcome,
                    old_node_ids=old_node_ids,
                    start_index=turn.index,
                    turn_id=turn.turn_id,
                    extra={
                        "committed_turns_rescored": committed_rescored,
                        "order_log": [f"append:{turn.turn_id}"],
                    },
                )
            except BusinessError as exc:
                self._record_error(exc)
                raise

    def apply_batch(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        """Apply one retract and one append in the declared canonical order."""

        with self._lock:
            try:
                if not operations:
                    raise BusinessError(
                        E_OPERATION_ORDER,
                        "operation batch must not be empty",
                        node="batch",
                    )
                kinds = [operation.get("op") for operation in operations]
                if any(kind not in {"retract", "append"} for kind in kinds):
                    raise BusinessError(
                        E_OPERATION_ORDER,
                        "operation batch contains an unsupported operation",
                        node="batch",
                        details={"kinds": kinds},
                    )
                if "retract" in kinds and "append" in kinds:
                    last_retract = max(
                        index for index, kind in enumerate(kinds) if kind == "retract"
                    )
                    first_append = kinds.index("append")
                    if first_append < last_retract:
                        raise BusinessError(
                            E_OPERATION_ORDER,
                            "simultaneous operations must be retract then append",
                            node="batch",
                            details={
                                "received_order": kinds,
                                "declared_order": self.config[
                                    "simultaneous_order"
                                ],
                            },
                        )

                old_session = self.session
                old_retracted = set(self.retracted)
                old_node_ids = self._old_node_ids()
                working_session = self.session
                working_retracted = set(self.retracted)
                affected: set[str] = set()
                order_log: list[str] = []
                first_index: int | None = None

                for operation in operations:
                    kind = operation["op"]
                    if kind == "retract":
                        turn_id = str(operation.get("turn_id", ""))
                        if turn_id in working_retracted:
                            raise BusinessError(
                                E_TURN_ALREADY_RETRACTED,
                                "turn has already been retracted",
                                node=turn_id,
                            )
                        target = next(
                            (
                                turn
                                for turn in working_session.ordered_turns()
                                if turn.turn_id == turn_id
                            ),
                            None,
                        )
                        if target is None:
                            raise BusinessError(
                                E_TURN_NOT_FOUND,
                                "turn does not exist in this session",
                                node=turn_id,
                            )
                        working_retracted.add(turn_id)
                        affected.update(self._affected_slots(target))
                        first_index = (
                            target.index
                            if first_index is None
                            else min(first_index, target.index)
                        )
                        order_log.append(f"retract:{turn_id}")
                    else:
                        raw_turn = operation.get("turn")
                        if not isinstance(raw_turn, dict):
                            raise BusinessError(
                                E_OPERATION_ORDER,
                                "append operation requires a turn object",
                                node="batch",
                            )
                        turn = Turn.from_dict(raw_turn)
                        working_session = working_session.append_turn(turn)
                        affected.update(self._affected_slots(turn))
                        first_index = (
                            turn.index
                            if first_index is None
                            else min(first_index, turn.index)
                        )
                        order_log.append(f"append:{turn.turn_id}")

                self.session = working_session
                self.retracted = working_retracted
                try:
                    outcome = self._build_internal()
                except BusinessError:
                    self.session = old_session
                    self.retracted = old_retracted
                    raise
                self._last_outcome = outcome
                return self._finish_operation(
                    operation="batch",
                    affected_slots=sorted(affected),
                    outcome=outcome,
                    old_node_ids=old_node_ids,
                    start_index=first_index,
                    turn_id=None,
                    extra={
                        "order_log": order_log,
                        "operations": copy.deepcopy(operations),
                    },
                )
            except BusinessError as exc:
                self._record_error(exc)
                raise

    # ------------------------------------------------------------------
    # Trace and diagnostics
    # ------------------------------------------------------------------
    def trace_slot(
        self,
        slot_key: str,
        *,
        record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            try:
                state = self.state
                if record is None:
                    if slot_key not in state["slots"]:
                        raise BusinessError(
                            E_LINEAGE_BROKEN,
                            "slot is absent from the final state",
                            slot=slot_key,
                            node="summary",
                        )
                    record = state["slots"][slot_key]
                tracer = LineageTracer(self.cache, self.config)
                return tracer.trace(slot_key, record)
            except BusinessError as exc:
                self._record_error(exc)
                raise

    def snapshot_summary(self) -> dict[str, Any]:
        current = self.snapshots.load_current()
        if current is None:
            return {"version": 0, "checksum": None, "cache_entries": 0}
        return {
            "version": current["version"],
            "checksum": current["checksum"],
            "cache_entries": len(current["cache_manifest"]),
            "config_fingerprint": current["config_fingerprint"],
        }

    def dashboard(self) -> dict[str, Any]:
        if self._last_outcome is None:
            self.build()
        assert self._last_outcome is not None
        try:
            snapshot = self.snapshot_summary()
        except BusinessError as exc:
            self._record_error(exc)
            snapshot = {
                "version": None,
                "checksum": None,
                "error": exc.to_dict(),
            }
        active = self.active_turns()
        return {
            "session": {
                "session_id": self.session.session_id,
                "seed": self.session.seed,
                "generator_version": self.session.generator_version,
                "turn_count": len(self.session.turns),
                "active_turn_count": len(active),
                "active_turns": [
                    {
                        "turn_id": turn.turn_id,
                        "index": turn.index,
                        "role": turn.role,
                        "text": turn.text,
                        "candidate_keys": sorted(
                            {candidate.key for candidate in turn.candidates}
                        ),
                        "clarification_slots": sorted(
                            {
                                request.slot
                                for request in turn.clarifications
                            }
                        ),
                    }
                    for turn in active
                ],
                "retracted_turn_ids": sorted(self.retracted),
                "demo": self.session.metadata.get("demo", {}),
            },
            "state": self._last_outcome.state,
            "state_hash": self._last_outcome.state_hash,
            "graph": self._last_outcome.graph,
            "last_operation": copy.deepcopy(self._last_operation),
            "operation_history": list(self.operation_history),
            "recent_errors": list(self.recent_errors),
            "snapshot": snapshot,
            "cache": self.cache.stats(),
            "config": self.config,
        }
