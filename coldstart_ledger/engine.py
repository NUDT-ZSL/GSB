from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .errors import BusinessError, ErrorCode
from .fixed import canonical_decimal, convert_floor, decimal_ticks, residual_key
from .loader import InputBundle, load_inputs
from .models import InstanceSpec, LayerSpec, LedgerConfig
from .store import LedgerRevision, LedgerStore, canonical_json, sha256_text


STAGE_PENDING = "PENDING_APPROVAL"
STAGE_READING = "READING_LAYERS"
STAGE_WRITING = "WRITING_EPHEMERAL"
STAGE_READY = "READY"
STAGE_WITHDRAWN = "WITHDRAWN"
ACTIVE_STAGES = {STAGE_READING, STAGE_WRITING}
ALL_STAGES = {STAGE_PENDING, STAGE_READING, STAGE_WRITING, STAGE_READY, STAGE_WITHDRAWN}

CATEGORY_NOT_STARTED = "NOT_STARTED_WITHIN_BUDGET"
CATEGORY_IN_PROGRESS = "IN_PROGRESS"
CATEGORY_READY = "READY"
CATEGORY_FAILED_AFTER_START = "FAILED_AFTER_START"
CATEGORY_ABANDONED = "ABANDONED"
CATEGORY_DEADLINE = "DEADLINE_WITHDRAWN"

STAGE_RANK = {
    STAGE_PENDING: 0,
    STAGE_READING: 1,
    STAGE_WRITING: 2,
    STAGE_READY: 3,
    STAGE_WITHDRAWN: -1,
}
_SCORE_TIME_BASE = 10**12
_SCORE_STAGE_BASE = 10**9


@dataclass(frozen=True)
class RoundResult:
    data: dict[str, Any]
    revision: LedgerRevision
    applied: bool

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.data)
        payload["ledger_revision"] = self.revision.to_dict()
        payload["idempotent_replay"] = not self.applied
        return payload

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


def _biz(code: str, message: str, **details: Any) -> None:
    raise BusinessError(code, message, details)


def _require_nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _biz(ErrorCode.SCHEMA_INVALID, f"{field} must be a non-empty string", field=field)
    return value.strip()


def _require_int(value: Any, field: str) -> int:
    if type(value) is not int:
        _biz(ErrorCode.INVALID_INTEGER, f"{field} must be an integer", field=field, value=value)
    return value


def _stage_rank(stage: str) -> int:
    return STAGE_RANK.get(stage, -1)


def _fixed_score(priority: int, stage: str, read_ticks: int, write_ticks: int, scale: int) -> int:
    return (
        priority * _SCORE_TIME_BASE
        + _stage_rank(stage) * _SCORE_STAGE_BASE
        + read_ticks
        + write_ticks // scale
    )


def _decimal_score(
    priority: int, stage: str, decimal_read_ticks: Decimal, decimal_write_ticks: Decimal, scale: int
) -> Decimal:
    return (
        Decimal(priority) * Decimal(_SCORE_TIME_BASE)
        + Decimal(_stage_rank(stage)) * Decimal(_SCORE_STAGE_BASE)
        + decimal_read_ticks
        + decimal_write_ticks / Decimal(scale)
    )


class ColdStartEngine:
    """Deterministic cold-start quota ledger backed by SQLite snapshots."""

    def __init__(
        self,
        db_path: str | Path,
        bundle: InputBundle,
        *,
        busy_timeout_ms: int = 30_000,
        crash_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.bundle = bundle
        self.store = LedgerStore(db_path, busy_timeout_ms=busy_timeout_ms)
        self._crash_hook = crash_hook
        self.store.initialize(self._initial_state())

    @classmethod
    def from_directory(
        cls,
        input_directory: str | Path,
        db_path: str | Path,
        **kwargs: Any,
    ) -> "ColdStartEngine":
        return cls(db_path, load_inputs(input_directory), **kwargs)

    @property
    def config(self) -> LedgerConfig:
        return self.bundle.config

    def _initial_state(self) -> dict[str, Any]:
        bundle = self.bundle
        layers: dict[str, Any] = {}
        managed_by_node: dict[str, int] = {node.name: 0 for node in bundle.nodes}
        for layer in bundle.layers:
            layers[layer.digest] = {
                "digest": layer.digest,
                "bytes": layer.bytes,
                "node": layer.node,
                "ref_count": layer.ref_count,
            }
            managed_by_node[layer.node] += layer.bytes

        instances: dict[str, Any] = {}
        layers_by_digest = bundle.layers_by_digest
        for spec in bundle.instances:
            required_bytes = sum(layers_by_digest[digest].bytes for digest in spec.required_layers)
            instances[spec.instance_id] = {
                "instance_id": spec.instance_id,
                "service": spec.service,
                "priority": spec.priority,
                "requested_ephemeral_bytes": spec.requested_ephemeral_bytes,
                "required_layers": list(spec.required_layers),
                "deadline_ms": spec.deadline_ms,
                "node": spec.node,
                "stage": STAGE_PENDING,
                "outcome": "NOT_STARTED",
                "outcome_category": CATEGORY_NOT_STARTED,
                "outcome_detail": None,
                "last_round_id": None,
                "admitted_at_ms": None,
                "read_started_at_ms": None,
                "write_started_at_ms": None,
                "ready_at_ms": None,
                "withdrawn_at_ms": None,
                "reserved_write_bytes": 0,
                "write_reserved": False,
                "read_reserved": False,
                "read_ticks": 0,
                "node_read_ticks": 0,
                "write_ticks": 0,
                "node_write_ticks": 0,
                "decimal_read_ticks": "0",
                "decimal_write_ticks": "0",
                "score_fixed": 0,
                "score_decimal": "0",
                "consumed_read_ticks": 0,
                "consumed_write_ticks": 0,
                "committed_layers": [],
                "required_layer_bytes_at_admission": None,
                "required_layer_bytes_current": required_bytes,
                "preempted_by": None,
                "refund_ids": [],
            }

        nodes: dict[str, Any] = {}
        for spec in bundle.nodes:
            nodes[spec.name] = {
                "node": spec.name,
                "initial_available_ephemeral_bytes": spec.available_ephemeral_bytes,
                "available_ephemeral_bytes": spec.available_ephemeral_bytes,
                "charged_ephemeral_bytes": 0,
                "reserved_write_ticks": 0,
                "read_reserved_ticks": 0,
                "active_count": 0,
                "initial_image_layer_bytes": spec.image_layer_bytes,
                "image_layer_bytes": spec.image_layer_bytes,
                "base_unmanaged_image_layer_bytes": spec.image_layer_bytes - managed_by_node[spec.name],
                "max_concurrent_cold_starts": spec.max_concurrent_cold_starts,
            }

        fingerprint = sha256_text(canonical_json(bundle.to_dict()))
        return {
            "schema_version": 1,
            "input_fingerprint": fingerprint,
            "generation": 0,
            "clock_watermark_ms": 0,
            "last_injected_clock_ms": None,
            "clock_initialized": False,
            "round_watermark": 0,
            "last_round_id": None,
            "config": bundle.config.to_dict(),
            "layers": layers,
            "instances": instances,
            "nodes": nodes,
            "residuals": {},
            "conversions": [],
            "refunds": [],
            "precision_flags": [],
            "rounds": [],
            "error_ledger": {},
            "counters": {"conversion": 0, "refund": 0, "precision_flag": 0},
            "instance_order": [item.instance_id for item in bundle.instances],
            "node_order": [item.name for item in bundle.nodes],
            "layer_order": [item.digest for item in bundle.layers],
        }

    def _execute(
        self,
        *,
        instance_id: str,
        round_id: str,
        source: str,
        request: Any,
        commit_kind: str,
        mutator: Callable[[dict[str, Any]], dict[str, Any]],
    ):
        def guarded(state: dict[str, Any]) -> dict[str, Any]:
            result = mutator(state)
            report = self._audit_state(state)
            result = dict(result)
            result["audit"] = report
            return result

        return self.store.execute(
            instance_id=instance_id,
            round_id=round_id,
            source=source,
            request=request,
            commit_kind=commit_kind,
            mutator=guarded,
            crash_hook=self._crash_hook,
        )

    def _clock(self, state: dict[str, Any], now_ms: int | None) -> int:
        config = state["config"]
        if now_ms is None:
            now = state["clock_watermark_ms"] + config["logical_clock_step_ms"]
            if not state["clock_initialized"]:
                now = 0
        else:
            now = _require_int(now_ms, "now_ms")
            if now < 0:
                _biz(ErrorCode.INVALID_QUANTITY, "now_ms must be non-negative", now_ms=now)
        watermark = state["clock_watermark_ms"]
        threshold = config["clock_rollback_threshold_ms"]
        if state["clock_initialized"] and now < watermark - threshold:
            _biz(
                ErrorCode.CLOCK_ROLLBACK,
                "logical clock rollback exceeds the configured threshold",
                previous_ms=watermark,
                injected_ms=now,
                threshold_ms=threshold,
            )
        state["clock_initialized"] = True
        state["last_injected_clock_ms"] = now
        state["clock_watermark_ms"] = max(watermark, now)
        return now

    def _check_clock_read_only(self, state: dict[str, Any], now_ms: int | None) -> int:
        config = state["config"]
        if now_ms is None:
            if not state["clock_initialized"]:
                return 0
            return state["clock_watermark_ms"] + config["logical_clock_step_ms"]
        now = _require_int(now_ms, "now_ms")
        if now < 0:
            _biz(ErrorCode.INVALID_QUANTITY, "now_ms must be non-negative", now_ms=now)
        if state["clock_initialized"]:
            threshold = config["clock_rollback_threshold_ms"]
            if now < state["clock_watermark_ms"] - threshold:
                _biz(
                    ErrorCode.CLOCK_ROLLBACK,
                    "logical clock rollback exceeds the configured threshold",
                    previous_ms=state["clock_watermark_ms"],
                    injected_ms=now,
                    threshold_ms=threshold,
                )
        return now

    def _preview_quote(
        self,
        state: dict[str, Any],
        spec: InstanceSpec,
        round_id: str,
    ) -> dict[str, Any]:
        config = state["config"]
        bandwidth = config["layer_read_bandwidth_bytes_per_second"]
        scale = config["fixed_scale"]
        residuals = dict(state["residuals"])
        seq = state["counters"]["conversion"]
        entries: list[dict[str, Any]] = []
        instance_read_ticks = 0
        node_read_ticks = 0
        decimal_read = Decimal(0)

        for digest in spec.required_layers:
            layer = state["layers"][digest]
            base_numerator = layer["bytes"] * 1000
            decimal_read += decimal_ticks(base_numerator, bandwidth, scale)

            instance_key = residual_key("instance", spec.instance_id, "layer_read_ticks", scale)
            instance_result = convert_floor(
                base_numerator,
                bandwidth,
                scale,
                residuals.get(instance_key, 0),
            )
            residuals[instance_key] = instance_result.remainder
            seq += 1
            entries.append(
                self._conversion_entry(
                    seq=seq,
                    round_id=round_id,
                    instance_id=spec.instance_id,
                    node=spec.node,
                    metric="layer_read_ticks",
                    scope_type="instance",
                    scope_id=spec.instance_id,
                    layer_digest=digest,
                    result=instance_result,
                    base_numerator=base_numerator,
                )
            )
            instance_read_ticks += instance_result.ticks

            node_key = residual_key("node", spec.node, "layer_read_ticks", scale)
            node_result = convert_floor(
                base_numerator,
                bandwidth,
                scale,
                residuals.get(node_key, 0),
            )
            residuals[node_key] = node_result.remainder
            seq += 1
            entries.append(
                self._conversion_entry(
                    seq=seq,
                    round_id=round_id,
                    instance_id=spec.instance_id,
                    node=spec.node,
                    metric="layer_read_ticks",
                    scope_type="node",
                    scope_id=spec.node,
                    layer_digest=digest,
                    result=node_result,
                    base_numerator=base_numerator,
                )
            )
            node_read_ticks += node_result.ticks

        decimal_write = Decimal(spec.requested_ephemeral_bytes) * Decimal(scale)
        for scope_type, scope_id in (
            ("instance", spec.instance_id),
            ("node", spec.node),
        ):
            key = residual_key(scope_type, scope_id, "write_ticks", scale)
            result = convert_floor(
                spec.requested_ephemeral_bytes,
                1,
                scale,
                residuals.get(key, 0),
            )
            residuals[key] = result.remainder
            seq += 1
            entries.append(
                self._conversion_entry(
                    seq=seq,
                    round_id=round_id,
                    instance_id=spec.instance_id,
                    node=spec.node,
                    metric="write_ticks",
                    scope_type=scope_type,
                    scope_id=scope_id,
                    layer_digest=None,
                    result=result,
                    base_numerator=spec.requested_ephemeral_bytes,
                )
            )

        write_ticks = spec.requested_ephemeral_bytes * scale
        score_fixed = _fixed_score(spec.priority, STAGE_PENDING, instance_read_ticks, write_ticks, scale)
        score_decimal = _decimal_score(
            spec.priority,
            STAGE_PENDING,
            decimal_read,
            decimal_write,
            scale,
        )
        read_deviation = abs(Decimal(instance_read_ticks) - decimal_read)
        write_deviation = abs(Decimal(write_ticks) - decimal_write)
        cumulative = read_deviation + write_deviation
        sources = [
            {
                "instance_id": entry["instance_id"],
                "layer_digest": entry["layer_digest"],
                "conversion_seq": entry["seq"],
                "scope_type": entry["scope_type"],
                "scope_id": entry["scope_id"],
                "metric": entry["metric"],
                "remainder": entry["remainder"],
                "denominator": entry["denominator"],
            }
            for entry in entries
            if entry["scope_type"] == "instance"
        ]
        return {
            "instance_read_ticks": instance_read_ticks,
            "node_read_ticks": node_read_ticks,
            "read_ticks": instance_read_ticks,
            "write_ticks": write_ticks,
            "node_write_ticks": write_ticks,
            "decimal_read_ticks": canonical_decimal(decimal_read),
            "decimal_write_ticks": canonical_decimal(decimal_write),
            "score_fixed": score_fixed,
            "score_decimal": canonical_decimal(score_decimal),
            "read_abs_deviation": canonical_decimal(read_deviation),
            "write_abs_deviation": canonical_decimal(write_deviation),
            "score_abs_deviation": canonical_decimal(abs(Decimal(score_fixed) - score_decimal)),
            "cumulative_error_upper_bound": canonical_decimal(cumulative),
            "within_error_budget": cumulative <= Decimal(config["error_upper_bound_budget"]),
            "error_sources": sources,
            "entries": entries,
            "residuals": residuals,
        }

    @staticmethod
    def _conversion_entry(
        *,
        seq: int,
        round_id: str,
        instance_id: str,
        node: str,
        metric: str,
        scope_type: str,
        scope_id: str,
        layer_digest: str | None,
        result: Any,
        base_numerator: int,
    ) -> dict[str, Any]:
        return {
            "seq": seq,
            "round_id": round_id,
            "instance_id": instance_id,
            "node": node,
            "metric": metric,
            "scope_type": scope_type,
            "scope_id": scope_id,
            "layer_digest": layer_digest,
            "scale": result.scale,
            "base_numerator": base_numerator,
            "denominator": result.denominator,
            "carry_in": result.carry_in,
            "effective_numerator": result.effective_numerator,
            "quotient": result.ticks,
            "fixed_ticks": result.ticks,
            "remainder": result.remainder,
            "discarded_remainder": result.remainder,
            "direction": result.direction,
            "rounding_direction": result.direction,
        }

    def _commit_quote(self, state: dict[str, Any], quote: dict[str, Any]) -> None:
        expected = state["counters"]["conversion"] + len(quote["entries"])
        if quote["entries"] and quote["entries"][-1]["seq"] != expected:
            _biz(ErrorCode.INTERNAL_ERROR, "conversion sequence changed during admission")
        state["conversions"].extend(quote["entries"])
        state["residuals"] = dict(quote["residuals"])
        state["counters"]["conversion"] = expected

    def _record_instance_estimates(
        self,
        state: dict[str, Any],
        instance_id: str,
        quote: dict[str, Any],
        *,
        round_id: str | None = None,
        charged: bool = True,
    ) -> None:
        instance = state["instances"][instance_id]
        instance["read_ticks"] = quote["instance_read_ticks"]
        instance["node_read_ticks"] = quote["node_read_ticks"]
        instance["write_ticks"] = quote["write_ticks"]
        instance["node_write_ticks"] = quote["node_write_ticks"]
        instance["decimal_read_ticks"] = quote["decimal_read_ticks"]
        instance["decimal_write_ticks"] = quote["decimal_write_ticks"]
        instance["score_fixed"] = quote["score_fixed"]
        instance["score_decimal"] = quote["score_decimal"]
        instance["required_layer_bytes_at_admission"] = sum(
            state["layers"][digest]["bytes"] for digest in instance["required_layers"]
        )
        instance["required_layer_bytes_current"] = sum(
            state["layers"][digest]["bytes"] for digest in instance["required_layers"]
        )
        error_record = {
            "instance_id": instance_id,
            "round_id": round_id or instance.get("last_round_id"),
            "charged": charged,
            "fixed_read_ticks": quote["instance_read_ticks"],
            "decimal_read_ticks": quote["decimal_read_ticks"],
            "read_abs_deviation": quote["read_abs_deviation"],
            "fixed_write_ticks": quote["write_ticks"],
            "decimal_write_ticks": quote["decimal_write_ticks"],
            "write_abs_deviation": quote["write_abs_deviation"],
            "fixed_score": quote["score_fixed"],
            "decimal_score": quote["score_decimal"],
            "score_abs_deviation": quote["score_abs_deviation"],
            "cumulative_error_upper_bound": quote["cumulative_error_upper_bound"],
            "error_upper_bound_budget": state["config"]["error_upper_bound_budget"],
            "within_error_budget": quote["within_error_budget"],
            "sources": quote["error_sources"],
            "quote_entries": quote["entries"],
        }
        state["error_ledger"][instance_id] = error_record

    def _reclaim_candidates(self, state: dict[str, Any], spec: InstanceSpec) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for other_id in state["instance_order"]:
            other = state["instances"][other_id]
            if other["node"] != spec.node or other["stage"] not in ACTIVE_STAGES:
                continue
            if other["priority"] >= spec.priority:
                continue
            consumed_read = (
                other["read_ticks"]
                if other["stage"] == STAGE_READING
                else int(other["consumed_read_ticks"])
            )
            consumed_write = (
                other["write_ticks"]
                if other["stage"] == STAGE_WRITING
                else int(other["consumed_write_ticks"])
            )
            score = (
                other["priority"] * _SCORE_TIME_BASE
                + _stage_rank(other["stage"]) * _SCORE_STAGE_BASE
                + consumed_write
            )
            candidates.append(
                {
                    "instance_id": other_id,
                    "priority": other["priority"],
                    "stage": other["stage"],
                    "stage_rank": _stage_rank(other["stage"]),
                    "reserved_ephemeral_bytes": other["reserved_write_bytes"],
                    "read_ticks": other["read_ticks"],
                    "write_ticks": other["write_ticks"],
                    "consumed_read_ticks": consumed_read,
                    "consumed_write_ticks": consumed_write,
                    "reclaim_score_fixed": score,
                    "reclaim_score_decimal": canonical_decimal(Decimal(score)),
                    "sources": list(
                        state.get("error_ledger", {}).get(other_id, {}).get("sources", [])
                    ),
                }
            )
        candidates.sort(
            key=lambda item: (
                item["priority"],
                item["stage_rank"],
                item["consumed_write_ticks"],
                item["instance_id"],
            )
        )
        return candidates

    def _build_preemption_plan(
        self,
        state: dict[str, Any],
        spec: InstanceSpec,
        quote: dict[str, Any],
        *,
        round_id: str,
        now_ms: int,
    ) -> dict[str, Any]:
        node = state["nodes"][spec.node]
        need_bytes = spec.requested_ephemeral_bytes
        quota_limit = state["config"]["temp_storage_quota_limit_bytes"]
        global_charged = sum(item["charged_ephemeral_bytes"] for item in state["nodes"].values())
        candidates = self._reclaim_candidates(state, spec)
        selected: list[dict[str, Any]] = []
        freed_bytes = 0
        freed_active = 0
        precision_flags: list[dict[str, Any]] = []

        def can_admit() -> bool:
            return (
                node["available_ephemeral_bytes"] + freed_bytes >= need_bytes
                and global_charged - freed_bytes + need_bytes <= quota_limit
                and node["active_count"] - freed_active + 1 <= node["max_concurrent_cold_starts"]
            )

        for candidate in candidates:
            if can_admit():
                break
            selected.append(candidate)
            freed_bytes += candidate["reserved_ephemeral_bytes"]
            freed_active += 1

        alternative_chain: list[dict[str, Any]] | None = None
        if selected and len(candidates) > 1:
            alternative_order = [candidates[1]] + [
                item for item in candidates if item["instance_id"] != candidates[1]["instance_id"]
            ]
            alt_selected: list[dict[str, Any]] = []
            alt_freed_bytes = 0
            alt_freed_active = 0
            for candidate in alternative_order:
                if (
                    node["available_ephemeral_bytes"] + alt_freed_bytes >= need_bytes
                    and global_charged - alt_freed_bytes + need_bytes <= quota_limit
                    and node["active_count"] - alt_freed_active + 1
                    <= node["max_concurrent_cold_starts"]
                ):
                    break
                alt_selected.append(candidate)
                alt_freed_bytes += candidate["reserved_ephemeral_bytes"]
                alt_freed_active += 1
            alt_can = (
                node["available_ephemeral_bytes"] + alt_freed_bytes >= need_bytes
                and global_charged - alt_freed_bytes + need_bytes <= quota_limit
                and node["active_count"] - alt_freed_active + 1
                <= node["max_concurrent_cold_starts"]
            )
            if alt_can:
                alternative_chain = [
                    {
                        "instance_id": item["instance_id"],
                        "reclaim_score_fixed": item["reclaim_score_fixed"],
                        "sources": item["sources"],
                    }
                    for item in alt_selected
                ]
                if alt_selected:
                    primary_score = sum(
                        item["reclaim_score_fixed"] for item in selected
                    )
                    alternative_score = sum(
                        item["reclaim_score_fixed"] for item in alt_selected
                    )
                    if abs(primary_score - alternative_score) < state["config"][
                        "comparison_tolerance_ticks"
                    ]:
                        precision_flags.append(
                            {
                                "context": "preemption_chain_alternative",
                                "round_id": round_id,
                                "first_instance_id": selected[0]["instance_id"],
                                "second_instance_id": alt_selected[0]["instance_id"],
                                "tolerance_ticks": state["config"][
                                    "comparison_tolerance_ticks"
                                ],
                                "fixed_score_diff": abs(primary_score - alternative_score),
                                "decimal_score_diff": canonical_decimal(
                                    Decimal(abs(primary_score - alternative_score))
                                ),
                                "deciding": True,
                                "sources_first": selected[0]["sources"],
                                "sources_second": alt_selected[0]["sources"],
                            }
                        )

        can = can_admit()
        tolerance = state["config"]["comparison_tolerance_ticks"]
        for left, right in zip(selected, selected[1:]):
            score_diff = abs(left["reclaim_score_fixed"] - right["reclaim_score_fixed"])
            decimal_diff = abs(
                Decimal(left["reclaim_score_decimal"])
                - Decimal(right["reclaim_score_decimal"])
            )
            if score_diff < tolerance or decimal_diff < tolerance:
                precision_flags.append(
                    {
                        "context": "preemption_chain",
                        "round_id": round_id,
                        "first_instance_id": left["instance_id"],
                        "second_instance_id": right["instance_id"],
                        "tolerance_ticks": tolerance,
                        "fixed_score_diff": score_diff,
                        "decimal_score_diff": canonical_decimal(decimal_diff),
                        "deciding": True,
                        "sources_first": left["sources"],
                        "sources_second": right["sources"],
                    }
                )

        shortfall_bytes = max(
            0,
            need_bytes - (node["available_ephemeral_bytes"] + freed_bytes),
        )
        shortfall_slots = max(
            0,
            node["active_count"] - freed_active + 1 - node["max_concurrent_cold_starts"],
        )
        quota_after = global_charged - freed_bytes + need_bytes
        for item in selected:
            item["reason"] = {
                "priority_lower": f"{item['priority']} < {spec.priority}",
                "stage": item["stage"],
                "stage_rank": item["stage_rank"],
                "releases_ephemeral_bytes": item["reserved_ephemeral_bytes"],
                "consumed_read_ticks": item["consumed_read_ticks"],
                "consumed_write_ticks": item["consumed_write_ticks"],
                "reclaim_score_fixed": item["reclaim_score_fixed"],
            }

        return {
            "requesting_instance_id": spec.instance_id,
            "node": spec.node,
            "logical_clock_ms": now_ms,
            "requested_ephemeral_bytes": need_bytes,
            "node_available_before": node["available_ephemeral_bytes"],
            "node_charged_before": node["charged_ephemeral_bytes"],
            "global_quota_limit_bytes": quota_limit,
            "global_charged_before": global_charged,
            "reclaim_chain": selected,
            "freed_ephemeral_bytes": freed_bytes,
            "freed_active_slots": freed_active,
            "node_available_after": node["available_ephemeral_bytes"] + freed_bytes,
            "global_quota_after": quota_after,
            "can_admit_after": can,
            "shortfall_bytes": shortfall_bytes,
            "shortfall_active_slots": shortfall_slots,
            "precision_sensitive": bool(precision_flags),
            "precision_flags": precision_flags,
            "reclaim_chain_score_fixed": sum(
                item["reclaim_score_fixed"] for item in selected
            ),
            "alternative_reclaim_chain": alternative_chain,
            "basis": [
                "ascending priority",
                "ascending stage rank (read layers before write ephemeral)",
                "ascending consumed write ticks",
                "ascending instance id",
            ],
        }

    def _append_precision_flags(
        self,
        state: dict[str, Any],
        flags: list[dict[str, Any]],
    ) -> None:
        for flag in flags:
            state["counters"]["precision_flag"] += 1
            stored = dict(flag)
            stored["flag_seq"] = state["counters"]["precision_flag"]
            state["precision_flags"].append(stored)

    def _apply_preemption(
        self,
        state: dict[str, Any],
        plan: dict[str, Any],
        *,
        round_id: str,
        now_ms: int,
    ) -> None:
        for item in plan["reclaim_chain"]:
            instance = state["instances"][item["instance_id"]]
            if instance["stage"] not in ACTIVE_STAGES:
                _biz(
                    ErrorCode.INVALID_STAGE,
                    "preemption victim is no longer active",
                    instance_id=item["instance_id"],
                    stage=instance["stage"],
                )
            node = state["nodes"][instance["node"]]
            state["counters"]["refund"] += 1
            refund_id = f"refund-{state['counters']['refund']:08d}"
            refund = {
                "refund_id": refund_id,
                "instance_id": instance["instance_id"],
                "node": instance["node"],
                "round_id": round_id,
                "reason": "PREEMPTED",
                "preempted_by": plan["requesting_instance_id"],
                "read_ticks": instance["read_ticks"] if instance["read_reserved"] else 0,
                "write_ticks": instance["write_ticks"] if instance["write_reserved"] else 0,
                "consumed_read_ticks": (
                    instance["read_ticks"]
                    if instance["stage"] == STAGE_READING
                    else instance["consumed_read_ticks"]
                ),
                "consumed_write_ticks": (
                    instance["write_ticks"]
                    if instance["stage"] == STAGE_WRITING
                    else instance["consumed_write_ticks"]
                ),
                "released_ephemeral_bytes": instance["reserved_write_bytes"],
                "status": "UNSETTLED",
                "created_clock_ms": now_ms,
                "settled_clock_ms": None,
            }
            state["refunds"].append(refund)
            instance["refund_ids"].append(refund_id)

            node["available_ephemeral_bytes"] += instance["reserved_write_bytes"]
            node["charged_ephemeral_bytes"] -= instance["reserved_write_bytes"]
            node["reserved_write_ticks"] -= instance["write_ticks"]
            if instance["read_reserved"]:
                node["read_reserved_ticks"] -= instance["read_ticks"]
            node["active_count"] -= 1

            instance["reserved_write_bytes"] = 0
            instance["write_reserved"] = False
            instance["read_reserved"] = False
            instance["stage"] = STAGE_WITHDRAWN
            instance["outcome"] = "PREEMPTED"
            instance["outcome_category"] = CATEGORY_ABANDONED
            instance["outcome_detail"] = f"preempted_by:{plan['requesting_instance_id']}"
            instance["preempted_by"] = plan["requesting_instance_id"]
            instance["withdrawn_at_ms"] = now_ms
            instance["last_round_id"] = round_id

            if (
                node["available_ephemeral_bytes"] < 0
                or node["charged_ephemeral_bytes"] < 0
                or node["active_count"] < 0
            ):
                _biz(
                    ErrorCode.CONSERVATION_VIOLATION,
                    "preemption produced a negative node balance",
                    node=node["node"],
                )
        self._append_precision_flags(state, plan.get("precision_flags", []))

    def _mark_pending(
        self,
        state: dict[str, Any],
        instance: dict[str, Any],
        *,
        round_id: str,
        outcome: str,
        category: str,
        detail: str,
    ) -> None:
        instance["outcome"] = outcome
        instance["outcome_category"] = category
        instance["outcome_detail"] = detail
        instance["last_round_id"] = round_id

    def _withdraw_pending_deadline(
        self,
        state: dict[str, Any],
        instance: dict[str, Any],
        *,
        round_id: str,
        now_ms: int,
    ) -> None:
        instance["stage"] = STAGE_WITHDRAWN
        instance["outcome"] = "DEADLINE_MISSED"
        instance["outcome_category"] = CATEGORY_DEADLINE
        instance["outcome_detail"] = f"injected_clock_ms:{now_ms}>deadline_ms:{instance['deadline_ms']}"
        instance["last_round_id"] = round_id
        instance["withdrawn_at_ms"] = now_ms

    def _admit_into_state(
        self,
        state: dict[str, Any],
        spec: InstanceSpec,
        *,
        round_id: str,
        now_ms: int,
        quote: dict[str, Any] | None = None,
        allow_preemption: bool = True,
    ) -> dict[str, Any]:
        instance = state["instances"][spec.instance_id]
        if instance["stage"] != STAGE_PENDING:
            _biz(
                ErrorCode.INVALID_TRANSITION,
                "only a pending instance can be admitted",
                instance_id=spec.instance_id,
                stage=instance["stage"],
            )
        if now_ms > instance["deadline_ms"]:
            self._withdraw_pending_deadline(
                state,
                instance,
                round_id=round_id,
                now_ms=now_ms,
            )
            return {
                "admitted": False,
                "withdrawn": True,
                "reason": "DEADLINE_MISSED",
                "instance_id": spec.instance_id,
                "stage": instance["stage"],
                "outcome": instance["outcome"],
                "outcome_category": instance["outcome_category"],
            }

        quote = quote or self._preview_quote(state, spec, round_id)
        plan = self._build_preemption_plan(
            state,
            spec,
            quote,
            round_id=round_id,
            now_ms=now_ms,
        )
        if not plan["can_admit_after"]:
            if not allow_preemption and plan["reclaim_chain"]:
                reason = "PREEMPTION_DISABLED"
            else:
                reason = "NODE_CAPACITY"
            return {
                "admitted": False,
                "withdrawn": False,
                "reason": reason,
                "instance_id": spec.instance_id,
                "preemption_plan": plan,
            }

        if plan["reclaim_chain"]:
            self._apply_preemption(state, plan, round_id=round_id, now_ms=now_ms)

        node = state["nodes"][spec.node]
        if node["active_count"] >= node["max_concurrent_cold_starts"]:
            _biz(
                ErrorCode.NO_CAPACITY,
                "node concurrent cold-start limit is exhausted",
                node=spec.node,
                active_count=node["active_count"],
                max_concurrent=node["max_concurrent_cold_starts"],
            )
        if node["available_ephemeral_bytes"] < spec.requested_ephemeral_bytes:
            _biz(
                ErrorCode.NO_CAPACITY,
                "node ephemeral storage is insufficient after preemption",
                node=spec.node,
            )
        global_charged = sum(item["charged_ephemeral_bytes"] for item in state["nodes"].values())
        if global_charged + spec.requested_ephemeral_bytes > state["config"]["temp_storage_quota_limit_bytes"]:
            _biz(
                ErrorCode.NO_CAPACITY,
                "global temporary storage quota is insufficient after preemption",
            )

        self._commit_quote(state, quote)
        self._record_instance_estimates(state, spec.instance_id, quote)

        node["available_ephemeral_bytes"] -= spec.requested_ephemeral_bytes
        node["charged_ephemeral_bytes"] += spec.requested_ephemeral_bytes
        node["reserved_write_ticks"] += quote["write_ticks"]
        node["read_reserved_ticks"] += quote["instance_read_ticks"]
        node["active_count"] += 1

        instance["reserved_write_bytes"] = spec.requested_ephemeral_bytes
        instance["write_reserved"] = True
        instance["read_reserved"] = True
        instance["stage"] = STAGE_READING
        instance["outcome"] = "IN_PROGRESS"
        instance["outcome_category"] = CATEGORY_IN_PROGRESS
        instance["outcome_detail"] = None
        instance["last_round_id"] = round_id
        instance["admitted_at_ms"] = now_ms
        instance["read_started_at_ms"] = now_ms
        instance["committed_layers"] = []

        return {
            "admitted": True,
            "withdrawn": False,
            "instance_id": spec.instance_id,
            "stage": instance["stage"],
            "outcome": instance["outcome"],
            "outcome_category": instance["outcome_category"],
            "read_ticks": quote["instance_read_ticks"],
            "write_ticks": quote["write_ticks"],
            "score_fixed": quote["score_fixed"],
            "score_decimal": quote["score_decimal"],
            "preemption_plan": plan,
        }

    def admit(
        self,
        instance_id: str,
        round_id: str,
        now_ms: int | None = None,
        *,
        source: str = "admit",
        allow_preemption: bool = True,
    ) -> dict[str, Any]:
        instance_id = _require_nonempty(instance_id, "instance_id")
        round_id = _require_nonempty(round_id, "round_id")
        spec = self.bundle.instances_by_id.get(instance_id)
        if spec is None:
            _biz(ErrorCode.SCHEMA_INVALID, "unknown instance", instance_id=instance_id)

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            effective_now = self._clock(state, now_ms)
            result = self._admit_into_state(
                state,
                spec,
                round_id=round_id,
                now_ms=effective_now,
                allow_preemption=allow_preemption,
            )
            if not result["admitted"] and not result["withdrawn"]:
                plan = result["preemption_plan"]
                _biz(
                    ErrorCode.NO_CAPACITY,
                    "instance cannot be admitted under current quota",
                    instance_id=instance_id,
                    shortfall_bytes=plan["shortfall_bytes"],
                    shortfall_active_slots=plan["shortfall_active_slots"],
                    preemption_plan=plan,
                )
            return result

        execution = self._execute(
            instance_id=instance_id,
            round_id=round_id,
            source=source,
            request={
                "operation": "admit",
                "instance_id": instance_id,
                "now_ms": now_ms,
                "allow_preemption": allow_preemption,
            },
            commit_kind="ADMIT",
            mutator=mutate,
        )
        payload = dict(execution.result)
        payload["ledger_revision"] = execution.revision.to_dict()
        payload["idempotent_replay"] = not execution.applied
        return payload

    def plan_preemption(
        self,
        instance_id: str,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        instance_id = _require_nonempty(instance_id, "instance_id")
        spec = self.bundle.instances_by_id.get(instance_id)
        if spec is None:
            _biz(ErrorCode.SCHEMA_INVALID, "unknown instance", instance_id=instance_id)
        state, revision = self.store.read()
        effective_now = self._check_clock_read_only(state, now_ms)
        instance = state["instances"][instance_id]
        if instance["stage"] != STAGE_PENDING:
            _biz(
                ErrorCode.INVALID_STAGE,
                "preemption can only target a pending requester",
                instance_id=instance_id,
                stage=instance["stage"],
            )
        if effective_now > instance["deadline_ms"]:
            return {
                "requesting_instance_id": instance_id,
                "logical_clock_ms": effective_now,
                "can_admit_after": False,
                "reason": "DEADLINE_MISSED",
                "reclaim_chain": [],
                "precision_sensitive": False,
                "ledger_revision": revision.to_dict(),
            }
        quote = self._preview_quote(state, spec, f"plan:{instance_id}")
        plan = self._build_preemption_plan(
            state,
            spec,
            quote,
            round_id=f"plan:{instance_id}",
            now_ms=effective_now,
        )
        plan["quote"] = {
            "read_ticks": quote["instance_read_ticks"],
            "node_read_ticks": quote["node_read_ticks"],
            "write_ticks": quote["write_ticks"],
            "decimal_read_ticks": quote["decimal_read_ticks"],
            "score_fixed": quote["score_fixed"],
            "score_decimal": quote["score_decimal"],
        }
        plan["ledger_revision"] = revision.to_dict()
        return plan

    def _transition_common(
        self,
        *,
        operation: str,
        instance_id: str,
        round_id: str,
        source: str,
        request: dict[str, Any],
        now_ms: int | None,
        mutator: Callable[[dict[str, Any], int], dict[str, Any]],
    ) -> dict[str, Any]:
        execution = self._execute(
            instance_id=instance_id,
            round_id=round_id,
            source=source,
            request=request,
            commit_kind=operation,
            mutator=lambda state: mutator(state, self._clock(state, now_ms)),
        )
        payload = dict(execution.result)
        payload["ledger_revision"] = execution.revision.to_dict()
        payload["idempotent_replay"] = not execution.applied
        return payload

    def complete_reading(
        self,
        instance_id: str,
        round_id: str,
        committed_layers: list[str],
        now_ms: int | None = None,
        *,
        source: str = "complete_reading",
    ) -> dict[str, Any]:
        instance_id = _require_nonempty(instance_id, "instance_id")
        round_id = _require_nonempty(round_id, "round_id")
        if not isinstance(committed_layers, list) or any(not isinstance(x, str) for x in committed_layers):
            _biz(ErrorCode.SCHEMA_INVALID, "committed_layers must be an array of digests")
        committed = tuple(committed_layers)

        def mutate(state: dict[str, Any], now: int) -> dict[str, Any]:
            instance = state["instances"].get(instance_id)
            if instance is None:
                _biz(ErrorCode.SCHEMA_INVALID, "unknown instance", instance_id=instance_id)
            if instance["stage"] != STAGE_READING:
                _biz(
                    ErrorCode.INVALID_TRANSITION,
                    "complete_reading requires READING_LAYERS",
                    instance_id=instance_id,
                    stage=instance["stage"],
                )
            if now > instance["deadline_ms"]:
                self._release_active(
                    state,
                    instance,
                    round_id=round_id,
                    now_ms=now,
                    outcome="DEADLINE_EXCEEDED",
                    category=CATEGORY_DEADLINE,
                    detail="deadline exceeded while reading layers",
                )
                return {
                    "instance_id": instance_id,
                    "stage": instance["stage"],
                    "outcome": instance["outcome"],
                    "outcome_category": instance["outcome_category"],
                }
            expected = set(instance["required_layers"])
            actual_list = list(committed)
            if len(actual_list) != len(set(actual_list)):
                _biz(
                    ErrorCode.LAYER_DIGEST_MISMATCH,
                    "committed layer list contains duplicate digests",
                    instance_id=instance_id,
                )
            actual = set(actual_list)
            missing = sorted(expected - actual)
            if missing:
                _biz(
                    ErrorCode.LAYER_MISSING,
                    "committed layer set is missing required layers",
                    instance_id=instance_id,
                    missing_layers=missing,
                )
            unexpected = sorted(actual - expected)
            if unexpected:
                _biz(
                    ErrorCode.LAYER_DIGEST_MISMATCH,
                    "committed layer set contains unexpected digests",
                    instance_id=instance_id,
                    unexpected_layers=unexpected,
                )
            instance["stage"] = STAGE_WRITING
            instance["outcome"] = "IN_PROGRESS"
            instance["outcome_category"] = CATEGORY_IN_PROGRESS
            instance["outcome_detail"] = None
            instance["committed_layers"] = sorted(actual)
            instance["consumed_read_ticks"] = instance["read_ticks"]
            instance["write_started_at_ms"] = now
            instance["last_round_id"] = round_id
            return {
                "instance_id": instance_id,
                "stage": instance["stage"],
                "outcome": instance["outcome"],
                "outcome_category": instance["outcome_category"],
                "committed_layers": list(instance["committed_layers"]),
                "node": instance["node"],
            }

        return self._transition_common(
            operation="COMPLETE_READING",
            instance_id=instance_id,
            round_id=round_id,
            source=source,
            request={
                "operation": "complete_reading",
                "instance_id": instance_id,
                "committed_layers": sorted(committed),
                "now_ms": now_ms,
            },
            now_ms=now_ms,
            mutator=mutate,
        )

    def _release_active(
        self,
        state: dict[str, Any],
        instance: dict[str, Any],
        *,
        round_id: str,
        now_ms: int,
        outcome: str,
        category: str,
        detail: str | None,
    ) -> None:
        if instance["stage"] not in ACTIVE_STAGES:
            _biz(
                ErrorCode.INVALID_TRANSITION,
                "active quota release requires READING_LAYERS or WRITING_EPHEMERAL",
                instance_id=instance["instance_id"],
                stage=instance["stage"],
            )
        node = state["nodes"][instance["node"]]
        node["available_ephemeral_bytes"] += instance["reserved_write_bytes"]
        node["charged_ephemeral_bytes"] -= instance["reserved_write_bytes"]
        node["reserved_write_ticks"] -= instance["write_ticks"]
        if instance["read_reserved"]:
            node["read_reserved_ticks"] -= instance["read_ticks"]
        node["active_count"] -= 1
        instance["reserved_write_bytes"] = 0
        instance["write_reserved"] = False
        instance["read_reserved"] = False
        instance["stage"] = STAGE_WITHDRAWN
        instance["outcome"] = outcome
        instance["outcome_category"] = category
        instance["outcome_detail"] = detail
        instance["withdrawn_at_ms"] = now_ms
        instance["last_round_id"] = round_id
        if (
            node["available_ephemeral_bytes"] < 0
            or node["charged_ephemeral_bytes"] < 0
            or node["active_count"] < 0
        ):
            _biz(
                ErrorCode.CONSERVATION_VIOLATION,
                "quota release produced a negative node balance",
                node=node["node"],
            )

    def complete_writing(
        self,
        instance_id: str,
        round_id: str,
        now_ms: int | None = None,
        *,
        source: str = "complete_writing",
    ) -> dict[str, Any]:
        instance_id = _require_nonempty(instance_id, "instance_id")
        round_id = _require_nonempty(round_id, "round_id")

        def mutate(state: dict[str, Any], now: int) -> dict[str, Any]:
            instance = state["instances"].get(instance_id)
            if instance is None:
                _biz(ErrorCode.SCHEMA_INVALID, "unknown instance", instance_id=instance_id)
            if instance["stage"] != STAGE_WRITING:
                _biz(
                    ErrorCode.INVALID_TRANSITION,
                    "complete_writing requires WRITING_EPHEMERAL",
                    instance_id=instance_id,
                    stage=instance["stage"],
                )
            if now > instance["deadline_ms"]:
                self._release_active(
                    state,
                    instance,
                    round_id=round_id,
                    now_ms=now,
                    outcome="DEADLINE_EXCEEDED",
                    category=CATEGORY_DEADLINE,
                    detail="deadline exceeded while writing ephemeral data",
                )
                return {
                    "instance_id": instance_id,
                    "stage": instance["stage"],
                    "outcome": instance["outcome"],
                    "outcome_category": instance["outcome_category"],
                }
            node = state["nodes"][instance["node"]]
            node["active_count"] -= 1
            instance["stage"] = STAGE_READY
            instance["outcome"] = "READY"
            instance["outcome_category"] = CATEGORY_READY
            instance["outcome_detail"] = None
            instance["ready_at_ms"] = now
            instance["consumed_write_ticks"] = instance["write_ticks"]
            instance["last_round_id"] = round_id
            return {
                "instance_id": instance_id,
                "stage": instance["stage"],
                "outcome": instance["outcome"],
                "outcome_category": instance["outcome_category"],
                "reserved_ephemeral_bytes": instance["reserved_write_bytes"],
                "node": instance["node"],
            }

        return self._transition_common(
            operation="COMPLETE_WRITING",
            instance_id=instance_id,
            round_id=round_id,
            source=source,
            request={"operation": "complete_writing", "instance_id": instance_id, "now_ms": now_ms},
            now_ms=now_ms,
            mutator=mutate,
        )

    def fail_instance(
        self,
        instance_id: str,
        round_id: str,
        failure_code: str,
        now_ms: int | None = None,
        *,
        source: str = "fail_instance",
    ) -> dict[str, Any]:
        instance_id = _require_nonempty(instance_id, "instance_id")
        round_id = _require_nonempty(round_id, "round_id")
        failure_code = _require_nonempty(failure_code, "failure_code")

        def mutate(state: dict[str, Any], now: int) -> dict[str, Any]:
            instance = state["instances"].get(instance_id)
            if instance is None:
                _biz(ErrorCode.SCHEMA_INVALID, "unknown instance", instance_id=instance_id)
            if instance["stage"] not in ACTIVE_STAGES:
                _biz(
                    ErrorCode.INVALID_TRANSITION,
                    "failure after start requires an active instance",
                    instance_id=instance_id,
                    stage=instance["stage"],
                )
            outcome = "READ_LAYER_FAILED" if instance["stage"] == STAGE_READING else "FAILED_AFTER_START"
            self._release_active(
                state,
                instance,
                round_id=round_id,
                now_ms=now,
                outcome=outcome,
                category=CATEGORY_FAILED_AFTER_START,
                detail=failure_code,
            )
            return {
                "instance_id": instance_id,
                "stage": instance["stage"],
                "outcome": instance["outcome"],
                "outcome_category": instance["outcome_category"],
                "failure_code": failure_code,
            }

        return self._transition_common(
            operation="FAIL_INSTANCE",
            instance_id=instance_id,
            round_id=round_id,
            source=source,
            request={
                "operation": "fail_instance",
                "instance_id": instance_id,
                "failure_code": failure_code,
                "now_ms": now_ms,
            },
            now_ms=now_ms,
            mutator=mutate,
        )

    def abandon_instance(
        self,
        instance_id: str,
        round_id: str,
        now_ms: int | None = None,
        *,
        reason: str = "VOLUNTARY_ABANDON",
        source: str = "abandon_instance",
    ) -> dict[str, Any]:
        instance_id = _require_nonempty(instance_id, "instance_id")
        round_id = _require_nonempty(round_id, "round_id")
        reason = _require_nonempty(reason, "reason")

        def mutate(state: dict[str, Any], now: int) -> dict[str, Any]:
            instance = state["instances"].get(instance_id)
            if instance is None:
                _biz(ErrorCode.SCHEMA_INVALID, "unknown instance", instance_id=instance_id)
            if instance["stage"] in ACTIVE_STAGES:
                self._release_active(
                    state,
                    instance,
                    round_id=round_id,
                    now_ms=now,
                    outcome="ABANDONED",
                    category=CATEGORY_ABANDONED,
                    detail=reason,
                )
            elif instance["stage"] == STAGE_PENDING:
                instance["stage"] = STAGE_WITHDRAWN
                instance["outcome"] = "ABANDONED"
                instance["outcome_category"] = CATEGORY_ABANDONED
                instance["outcome_detail"] = reason
                instance["withdrawn_at_ms"] = now
                instance["last_round_id"] = round_id
            else:
                _biz(
                    ErrorCode.INVALID_TRANSITION,
                    "only pending or active instances can be abandoned",
                    instance_id=instance_id,
                    stage=instance["stage"],
                )
            return {
                "instance_id": instance_id,
                "stage": instance["stage"],
                "outcome": instance["outcome"],
                "outcome_category": instance["outcome_category"],
                "reason": reason,
            }

        return self._transition_common(
            operation="ABANDON_INSTANCE",
            instance_id=instance_id,
            round_id=round_id,
            source=source,
            request={
                "operation": "abandon_instance",
                "instance_id": instance_id,
                "reason": reason,
                "now_ms": now_ms,
            },
            now_ms=now_ms,
            mutator=mutate,
        )

    def withdraw_instance(
        self,
        instance_id: str,
        round_id: str,
        now_ms: int | None = None,
        *,
        reason: str = "MANUAL_WITHDRAW",
        source: str = "withdraw_instance",
    ) -> dict[str, Any]:
        instance_id = _require_nonempty(instance_id, "instance_id")
        round_id = _require_nonempty(round_id, "round_id")
        reason = _require_nonempty(reason, "reason")

        def mutate(state: dict[str, Any], now: int) -> dict[str, Any]:
            instance = state["instances"].get(instance_id)
            if instance is None:
                _biz(ErrorCode.SCHEMA_INVALID, "unknown instance", instance_id=instance_id)
            if instance["stage"] in ACTIVE_STAGES:
                self._release_active(
                    state,
                    instance,
                    round_id=round_id,
                    now_ms=now,
                    outcome="WITHDRAWN",
                    category=CATEGORY_ABANDONED,
                    detail=reason,
                )
            elif instance["stage"] == STAGE_PENDING:
                instance["stage"] = STAGE_WITHDRAWN
                instance["outcome"] = "WITHDRAWN"
                instance["outcome_category"] = CATEGORY_ABANDONED
                instance["outcome_detail"] = reason
                instance["withdrawn_at_ms"] = now
                instance["last_round_id"] = round_id
            else:
                _biz(
                    ErrorCode.INVALID_TRANSITION,
                    "only pending or active instances can be withdrawn",
                    instance_id=instance_id,
                    stage=instance["stage"],
                )
            return {
                "instance_id": instance_id,
                "stage": instance["stage"],
                "outcome": instance["outcome"],
                "outcome_category": instance["outcome_category"],
                "reason": reason,
            }

        return self._transition_common(
            operation="WITHDRAW_INSTANCE",
            instance_id=instance_id,
            round_id=round_id,
            source=source,
            request={
                "operation": "withdraw_instance",
                "instance_id": instance_id,
                "reason": reason,
                "now_ms": now_ms,
            },
            now_ms=now_ms,
            mutator=mutate,
        )

    def _precision_flags_for_candidates(
        self,
        state: dict[str, Any],
        *,
        round_id: str,
        candidates: list[InstanceSpec],
        quotes: dict[str, dict[str, Any]],
        deciding: bool,
    ) -> list[dict[str, Any]]:
        if not deciding:
            return []
        tolerance = state["config"]["comparison_tolerance_ticks"]
        flags: list[dict[str, Any]] = []
        for left, right in zip(candidates, candidates[1:]):
            left_quote = quotes[left.instance_id]
            right_quote = quotes[right.instance_id]
            fixed_diff = abs(left_quote["score_fixed"] - right_quote["score_fixed"])
            decimal_diff = abs(
                Decimal(left_quote["score_decimal"]) - Decimal(right_quote["score_decimal"])
            )
            if fixed_diff < tolerance or decimal_diff < tolerance:
                flags.append(
                    {
                        "context": "admission_order",
                        "round_id": round_id,
                        "first_instance_id": left.instance_id,
                        "second_instance_id": right.instance_id,
                        "tolerance_ticks": tolerance,
                        "fixed_score_diff": fixed_diff,
                        "decimal_score_diff": canonical_decimal(decimal_diff),
                        "deciding": True,
                        "sources_first": left_quote["error_sources"],
                        "sources_second": right_quote["error_sources"],
                    }
                )
        return flags

    def run_round(self, round_id: str, now_ms: int | None = None) -> RoundResult:
        round_id = _require_nonempty(round_id, "round_id")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            now = self._clock(state, now_ms)
            config = state["config"]
            scale = config["fixed_scale"]
            budget_ticks = config["round_logical_time_budget_ms"] * scale
            pending = [
                self.bundle.instances_by_id[item_id]
                for item_id in state["instance_order"]
                if state["instances"][item_id]["stage"] == STAGE_PENDING
            ]
            by_node: dict[str, list[InstanceSpec]] = {name: [] for name in state["node_order"]}
            for spec in pending:
                by_node[spec.node].append(spec)

            outcome_rows: list[dict[str, Any]] = []
            schedule_by_node: dict[str, Any] = {}
            all_flags: list[dict[str, Any]] = []

            for node_name in state["node_order"]:
                node = state["nodes"][node_name]
                candidates = sorted(
                    by_node[node_name],
                    key=lambda item: (-item.priority, item.deadline_ms, item.instance_id),
                )
                if not candidates:
                    schedule_by_node[node_name] = {
                        "candidates": [],
                        "admitted": [],
                        "slot_finish_ticks": [],
                    }
                    continue

                initial_quotes = {
                    spec.instance_id: self._preview_quote(state, spec, round_id)
                    for spec in candidates
                }
                for spec in candidates:
                    self._record_instance_estimates(
                        state,
                        spec.instance_id,
                        initial_quotes[spec.instance_id],
                        round_id=round_id,
                        charged=False,
                    )
                free_slots = max(0, node["max_concurrent_cold_starts"] - node["active_count"])
                constrained = len(candidates) > free_slots or (
                    sum(quote["read_ticks"] for quote in initial_quotes.values()) > budget_ticks
                )
                all_flags.extend(
                    self._precision_flags_for_candidates(
                        state,
                        round_id=round_id,
                        candidates=candidates,
                        quotes=initial_quotes,
                        deciding=constrained,
                    )
                )

                slots = [0] * free_slots
                admitted_rows: list[dict[str, Any]] = []
                budget_exhausted = False
                for spec in candidates:
                    instance = state["instances"][spec.instance_id]
                    if instance["stage"] != STAGE_PENDING:
                        continue
                    if now > instance["deadline_ms"]:
                        self._withdraw_pending_deadline(
                            state,
                            instance,
                            round_id=round_id,
                            now_ms=now,
                        )
                        outcome_rows.append(
                            {
                                "instance_id": spec.instance_id,
                                "node": node_name,
                                "outcome": instance["outcome"],
                                "outcome_category": instance["outcome_category"],
                                "stage": instance["stage"],
                            }
                        )
                        continue
                    if budget_exhausted:
                        self._mark_pending(
                            state,
                            instance,
                            round_id=round_id,
                            outcome="NOT_STARTED_BUDGET",
                            category=CATEGORY_NOT_STARTED,
                            detail="ROUND_BUDGET_EXHAUSTED",
                        )
                        outcome_rows.append(
                            {
                                "instance_id": spec.instance_id,
                                "node": node_name,
                                "outcome": instance["outcome"],
                                "outcome_category": instance["outcome_category"],
                                "stage": instance["stage"],
                            }
                        )
                        continue

                    quote = self._preview_quote(state, spec, round_id)
                    start_tick = min(slots) if slots else 0
                    if start_tick + quote["read_ticks"] > budget_ticks:
                        budget_exhausted = True
                        self._mark_pending(
                            state,
                            instance,
                            round_id=round_id,
                            outcome="NOT_STARTED_BUDGET",
                            category=CATEGORY_NOT_STARTED,
                            detail="ROUND_BUDGET_EXHAUSTED",
                        )
                        outcome_rows.append(
                            {
                                "instance_id": spec.instance_id,
                                "node": node_name,
                                "outcome": instance["outcome"],
                                "outcome_category": instance["outcome_category"],
                                "stage": instance["stage"],
                            }
                        )
                        continue

                    admission = self._admit_into_state(
                        state,
                        spec,
                        round_id=round_id,
                        now_ms=now,
                        quote=quote,
                        allow_preemption=True,
                    )
                    if admission["admitted"]:
                        if slots:
                            slot_index = min(range(len(slots)), key=lambda index: slots[index])
                            slots[slot_index] = start_tick + quote["read_ticks"]
                        else:
                            slots = [quote["read_ticks"]]
                        admitted_rows.append(admission)
                        outcome_rows.append(
                            {
                                "instance_id": spec.instance_id,
                                "node": node_name,
                                "outcome": admission["outcome"],
                                "outcome_category": admission["outcome_category"],
                                "stage": admission["stage"],
                                "start_tick": start_tick,
                                "estimated_read_ticks": quote["read_ticks"],
                                "preempted": [
                                    item["instance_id"]
                                    for item in admission["preemption_plan"]["reclaim_chain"]
                                ],
                            }
                        )
                    elif admission["withdrawn"]:
                        outcome_rows.append(
                            {
                                "instance_id": spec.instance_id,
                                "node": node_name,
                                "outcome": admission["outcome"],
                                "outcome_category": admission["outcome_category"],
                                "stage": admission["stage"],
                            }
                        )
                    else:
                        self._mark_pending(
                            state,
                            instance,
                            round_id=round_id,
                            outcome="NOT_STARTED_CAPACITY",
                            category=CATEGORY_NOT_STARTED,
                            detail=admission["reason"],
                        )
                        outcome_rows.append(
                            {
                                "instance_id": spec.instance_id,
                                "node": node_name,
                                "outcome": instance["outcome"],
                                "outcome_category": instance["outcome_category"],
                                "stage": instance["stage"],
                                "shortfall_bytes": admission["preemption_plan"]["shortfall_bytes"],
                                "shortfall_active_slots": admission["preemption_plan"][
                                    "shortfall_active_slots"
                                ],
                            }
                        )
                schedule_by_node[node_name] = {
                    "candidates": [spec.instance_id for spec in candidates],
                    "admitted": [row["instance_id"] for row in admitted_rows],
                    "slot_finish_ticks": slots,
                }

            self._append_precision_flags(state, all_flags)
            state["round_watermark"] += 1
            state["last_round_id"] = round_id
            summary = {
                "not_started_within_budget": [
                    row["instance_id"]
                    for row in outcome_rows
                    if row["outcome_category"] == CATEGORY_NOT_STARTED
                ],
                "budget_not_started": [
                    row["instance_id"]
                    for row in outcome_rows
                    if row["outcome_category"] == CATEGORY_NOT_STARTED
                ],
                "failed_after_start": [
                    row["instance_id"]
                    for row in outcome_rows
                    if row["outcome_category"] == CATEGORY_FAILED_AFTER_START
                ],
                "abandoned": [
                    row["instance_id"]
                    for row in outcome_rows
                    if row["outcome_category"] == CATEGORY_ABANDONED
                ],
                "admitted": [
                    row["instance_id"]
                    for row in outcome_rows
                    if row.get("stage") == STAGE_READING
                ],
                "precision_sensitive": bool(all_flags),
            }
            round_row = {
                "round_id": round_id,
                "round_watermark": state["round_watermark"],
                "logical_clock_ms": now,
                "logical_clock_watermark_ms": state["clock_watermark_ms"],
                "budget_ticks": budget_ticks,
                "outcomes": outcome_rows,
                "summary": summary,
                "schedule_by_node": schedule_by_node,
            }
            state["rounds"].append(round_row)
            return round_row

        execution = self._execute(
            instance_id="__scheduler__",
            round_id=round_id,
            source="run_round",
            request={"operation": "run_round", "round_id": round_id, "now_ms": now_ms},
            commit_kind="ROUND",
            mutator=mutate,
        )
        return RoundResult(execution.result, execution.revision, execution.applied)

    def apply_layer_change(
        self,
        digest: str,
        new_bytes: int,
        round_id: str,
        now_ms: int | None = None,
        *,
        source: str = "layer_change",
    ) -> dict[str, Any]:
        digest = _require_nonempty(digest, "digest")
        new_bytes = _require_int(new_bytes, "new_bytes")
        if new_bytes <= 0:
            _biz(ErrorCode.INVALID_QUANTITY, "new_bytes must be positive", new_bytes=new_bytes)
        round_id = _require_nonempty(round_id, "round_id")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            now = self._clock(state, now_ms)
            layer = state["layers"].get(digest)
            if layer is None:
                _biz(ErrorCode.LAYER_MISSING, "layer manifest entry is missing", digest=digest)
            delta = new_bytes - layer["bytes"]
            node = state["nodes"][layer["node"]]
            if node["image_layer_bytes"] + delta < 0:
                _biz(
                    ErrorCode.INVALID_QUANTITY,
                    "layer change would make node image-layer occupancy negative",
                    digest=digest,
                )
            layer["bytes"] = new_bytes
            node["image_layer_bytes"] += delta
            affected: list[str] = []
            for instance_id in state["instance_order"]:
                instance = state["instances"][instance_id]
                if instance["stage"] in ACTIVE_STAGES and digest in instance["required_layers"]:
                    self._release_active(
                        state,
                        instance,
                        round_id=round_id,
                        now_ms=now,
                        outcome="LAYER_CHANGED",
                        category=CATEGORY_ABANDONED,
                        detail=f"layer_changed:{digest}:{new_bytes}",
                    )
                    instance["required_layer_bytes_current"] = sum(
                        state["layers"][item]["bytes"] for item in instance["required_layers"]
                    )
                    affected.append(instance_id)
            return {
                "digest": digest,
                "old_bytes": new_bytes - delta,
                "new_bytes": new_bytes,
                "node": layer["node"],
                "withdrawn_instances": affected,
            }

        execution = self._execute(
            instance_id=digest,
            round_id=round_id,
            source=source,
            request={
                "operation": "layer_change",
                "digest": digest,
                "new_bytes": new_bytes,
                "now_ms": now_ms,
            },
            commit_kind="LAYER_CHANGE",
            mutator=mutate,
        )
        payload = dict(execution.result)
        payload["ledger_revision"] = execution.revision.to_dict()
        payload["idempotent_replay"] = not execution.applied
        return payload

    def settle_refund(
        self,
        refund_id: str,
        round_id: str,
        now_ms: int | None = None,
        *,
        source: str = "settle_refund",
    ) -> dict[str, Any]:
        refund_id = _require_nonempty(refund_id, "refund_id")
        round_id = _require_nonempty(round_id, "round_id")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            now = self._clock(state, now_ms)
            for refund in state["refunds"]:
                if refund["refund_id"] == refund_id:
                    if refund["status"] == "SETTLED":
                        return {"refund_id": refund_id, "status": "SETTLED", "already_settled": True}
                    refund["status"] = "SETTLED"
                    refund["settled_clock_ms"] = now
                    return {
                        "refund_id": refund_id,
                        "instance_id": refund["instance_id"],
                        "status": refund["status"],
                        "already_settled": False,
                    }
            _biz(ErrorCode.SCHEMA_INVALID, "unknown refund id", refund_id=refund_id)

        execution = self._execute(
            instance_id=refund_id,
            round_id=round_id,
            source=source,
            request={"operation": "settle_refund", "refund_id": refund_id, "now_ms": now_ms},
            commit_kind="SETTLE_REFUND",
            mutator=mutate,
        )
        payload = dict(execution.result)
        payload["ledger_revision"] = execution.revision.to_dict()
        payload["idempotent_replay"] = not execution.applied
        return payload

    def _audit_state(self, state: dict[str, Any]) -> dict[str, Any]:
        violations: list[str] = []
        config = state["config"]
        scale = config["fixed_scale"]
        quota_limit = config["temp_storage_quota_limit_bytes"]
        global_charged = 0
        node_reports: dict[str, Any] = {}

        for layer in state["layers"].values():
            if type(layer["bytes"]) is not int or layer["bytes"] <= 0:
                violations.append(f"layer {layer['digest']} has non-positive bytes")

        for node_name in state["node_order"]:
            node = state["nodes"][node_name]
            expected_active = sum(
                1
                for instance in state["instances"].values()
                if instance["node"] == node_name and instance["stage"] in ACTIVE_STAGES
            )
            expected_charged = sum(
                instance["reserved_write_bytes"]
                for instance in state["instances"].values()
                if instance["node"] == node_name
            )
            expected_write_ticks = sum(
                instance["write_ticks"]
                for instance in state["instances"].values()
                if instance["node"] == node_name and instance["write_reserved"]
            )
            expected_read_ticks = sum(
                instance["read_ticks"]
                for instance in state["instances"].values()
                if instance["node"] == node_name and instance["read_reserved"]
            )
            expected_layer_bytes = node["base_unmanaged_image_layer_bytes"] + sum(
                layer["bytes"]
                for layer in state["layers"].values()
                if layer["node"] == node_name
            )
            checks = {
                "available_nonnegative": node["available_ephemeral_bytes"] >= 0,
                "charged_nonnegative": node["charged_ephemeral_bytes"] >= 0,
                "read_reserved_nonnegative": node["read_reserved_ticks"] >= 0,
                "write_reserved_nonnegative": node["reserved_write_ticks"] >= 0,
                "balance_conserved": (
                    node["available_ephemeral_bytes"] + node["charged_ephemeral_bytes"]
                    == node["initial_available_ephemeral_bytes"]
                ),
                "charged_matches_instances": expected_charged == node["charged_ephemeral_bytes"],
                "write_ticks_match_instances": expected_write_ticks == node["reserved_write_ticks"],
                "read_ticks_match_instances": expected_read_ticks == node["read_reserved_ticks"],
                "active_count_matches": expected_active == node["active_count"],
                "active_within_limit": node["active_count"] <= node["max_concurrent_cold_starts"],
                "image_layer_bytes_match": expected_layer_bytes == node["image_layer_bytes"],
            }
            for check, passed in checks.items():
                if not passed:
                    violations.append(f"{node_name}:{check}")
            global_charged += node["charged_ephemeral_bytes"]
            node_reports[node_name] = {
                "available_ephemeral_bytes": node["available_ephemeral_bytes"],
                "charged_ephemeral_bytes": node["charged_ephemeral_bytes"],
                "active_count": node["active_count"],
                "max_concurrent_cold_starts": node["max_concurrent_cold_starts"],
                "read_reserved_ticks": node["read_reserved_ticks"],
                "reserved_write_ticks": node["reserved_write_ticks"],
                "checks": checks,
            }

        if global_charged > quota_limit:
            violations.append("global temporary storage quota exceeded")

        conversion_seqs: set[int] = set()
        last_residual: dict[str, int] = {}
        for conversion in state["conversions"]:
            seq = conversion["seq"]
            if seq in conversion_seqs:
                violations.append(f"duplicate conversion sequence {seq}")
            conversion_seqs.add(seq)
            denominator = conversion["denominator"]
            scale_value = conversion["scale"]
            recomputed_effective = conversion["base_numerator"] * scale_value + conversion["carry_in"]
            quotient, remainder = divmod(recomputed_effective, denominator)
            if recomputed_effective != conversion["effective_numerator"]:
                violations.append(f"conversion {seq} effective numerator mismatch")
            if quotient != conversion["quotient"] or remainder != conversion["remainder"]:
                violations.append(f"conversion {seq} quotient/remainder mismatch")
            if conversion["direction"] != "floor":
                violations.append(f"conversion {seq} has unsupported rounding direction")
            if not 0 <= remainder < denominator:
                violations.append(f"conversion {seq} remainder is outside one fixed unit")
            key = residual_key(
                conversion["scope_type"],
                conversion["scope_id"],
                conversion["metric"],
                scale_value,
            )
            last_residual[key] = remainder
        for key, residual in state["residuals"].items():
            if type(residual) is not int or residual < 0:
                violations.append(f"residual {key} is invalid")
            elif last_residual.get(key, 0) != residual:
                violations.append(f"residual {key} does not match its last conversion")
            if ":" in key:
                scale_text = key.rsplit(":", 1)[-1]
                if scale_text.isdigit() and residual >= int(scale_text) and "layer_read_ticks" in key:
                    # The remainder unit is the conversion denominator, not the
                    # output scale; this only catches obviously invalid large values.
                    pass

        for instance_id in state["instance_order"]:
            instance = state["instances"][instance_id]
            if instance["stage"] not in ALL_STAGES:
                violations.append(f"{instance_id}: unknown stage {instance['stage']}")
            if instance["stage"] in ACTIVE_STAGES:
                current_bytes = sum(
                    state["layers"][digest]["bytes"] for digest in instance["required_layers"]
                )
                if current_bytes != instance["required_layer_bytes_current"]:
                    violations.append(f"{instance_id}: active layer byte set changed without withdrawal")
                expected_layers = set(instance["required_layers"])
                if not expected_layers.issubset(state["layers"]):
                    violations.append(f"{instance_id}: active instance references a missing layer")
            if instance["write_reserved"] and not instance["reserved_write_bytes"] > 0:
                violations.append(f"{instance_id}: write reservation flag without bytes")
            if instance["read_reserved"] and instance["read_ticks"] < 0:
                violations.append(f"{instance_id}: invalid read reservation")
            if instance["stage"] == STAGE_READY and instance["write_reserved"] is not True:
                violations.append(f"{instance_id}: ready instance lost its storage reservation")
            if instance["stage"] == STAGE_WITHDRAWN and (
                instance["write_reserved"] or instance["read_reserved"]
            ):
                violations.append(f"{instance_id}: withdrawn instance retains quota")

        for refund in state["refunds"]:
            if refund["read_ticks"] < 0 or refund["write_ticks"] < 0:
                violations.append(f"{refund['refund_id']}: negative refund")
            if refund["status"] not in {"UNSETTLED", "SETTLED"}:
                violations.append(f"{refund['refund_id']}: invalid refund status")

        report = {
            "ok": not violations,
            "violations": violations,
            "global_charged_ephemeral_bytes": global_charged,
            "temp_storage_quota_limit_bytes": quota_limit,
            "conversion_count": len(state["conversions"]),
            "residual_count": len(state["residuals"]),
            "refund_unsettled_count": sum(
                1 for refund in state["refunds"] if refund["status"] == "UNSETTLED"
            ),
            "nodes": node_reports,
        }
        if violations:
            _biz(
                ErrorCode.CONSERVATION_VIOLATION,
                "ledger conservation audit failed",
                violations=violations,
            )
        return report

    def audit(self) -> dict[str, Any]:
        state, revision = self.store.read()
        report = self._audit_state(state)
        report["ledger_revision"] = revision.to_dict()
        return report

    def snapshot(self) -> dict[str, Any]:
        state, revision = self.store.read()
        return {
            "state": state,
            "revision": revision.to_dict(),
            "state_sha256": sha256_text(canonical_json(state)),
        }

    def recover(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        report = self._audit_state(snapshot["state"])
        snapshot["audit"] = report
        snapshot["recovered"] = True
        return snapshot

    def ledger_revision(self) -> dict[str, Any]:
        _, revision = self.store.read()
        return revision.to_dict()

