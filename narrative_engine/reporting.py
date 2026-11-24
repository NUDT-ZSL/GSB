from __future__ import annotations

from typing import Any

from .encoding import canonical_digest
from .engine import ReplayEngine
from .schema import Script


def build_run_report(
    script: Script,
    engine: ReplayEngine,
    *,
    sequence_id: str = "primary",
    planned_choice_count: int | None = None,
    recovery: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    node = script.node_map[engine.current_node_id]
    enabled_choices = [item for item in engine.choice_statuses() if item["enabled"]]
    planned = engine.choice_index if planned_choice_count is None else planned_choice_count

    if node.terminal:
        path_status = "terminal"
    elif not enabled_choices:
        path_status = "deadlocked"
    elif engine.choice_index >= planned:
        path_status = "choice_sequence_exhausted"
    else:
        path_status = "incomplete"

    final_exact = engine.state.exact()
    return {
        "ok": True,
        "report_type": "single_path_run",
        "sequence_id": sequence_id,
        "version_id": script.version_id,
        "fixed_point_scale": script.scale,
        "residual_tolerance": format(script.residual_tolerance, "f"),
        "status": path_status,
        "start_node": script.start_node,
        "current_node_id": engine.current_node_id,
        "node_sequence": list(engine.node_sequence),
        "choice_ids": list(engine.choice_ids),
        "choice_records": engine.choice_records,
        "node_count": len(engine.node_sequence),
        "unique_node_count": len(set(engine.node_sequence)),
        "event_count": engine.event_seq,
        "choice_count": engine.choice_index,
        "triggered_event_ids": [record["event_id"] for record in engine.event_records],
        "execution_order": [record["event_id"] for record in engine.event_records],
        "triggerable_event_set": sorted({record["event_id"] for record in engine.event_records}),
        "event_records": engine.event_records,
        "causal_chain": engine.causal_chain,
        "first_residual_exceedance": engine.first_residual_exceedance,
        "residual_exceedances": engine.residual_exceedances,
        "float64_reference_final": engine.float_trace.snapshot(),
        "world_state": engine.state.native_values(script),
        "world_state_decimal": engine.state.decimal_values(script),
        "world_state_exact": final_exact,
        "world_state_digest": engine.state.digest(),
        "enabled_choice_ids": [item["choice_id"] for item in enabled_choices],
        "terminal_node": node.terminal,
        "recovery": recovery,
        "warnings": warnings or [],
    }


def verify_report_correspondence(report: dict[str, Any], script: Script | None = None) -> list[str]:
    """Check causal pointers and reconstruct exact state from effect logs."""

    errors: list[str] = []
    records = report.get("event_records", [])
    by_seq = {record.get("seq"): record for record in records if isinstance(record, dict)}
    reconstructed: dict[str, dict[str, int | bool]] = {
        "fixed": dict(script.initial_fixed) if script else {},
        "integers": dict(script.initial_integers) if script else {},
        "booleans": dict(script.initial_booleans) if script else {},
    }

    for seq in sorted(by_seq):
        record = by_seq[seq]
        if record.get("event_ref") != f"event:{seq}":
            errors.append(f"event {seq} has invalid ref {record.get('event_ref')!r}")
        for effect in record.get("effects", []):
            key = effect["key"]
            base = effect["base"]
            bucket = "booleans" if base == "boolean" else ("integers" if base == "integer" else "fixed")
            before_field = {
                "fixed": "before_fixed",
                "integer": "before_integer",
                "boolean": "before_boolean",
            }[base]
            after_field = {
                "fixed": "after_fixed",
                "integer": "after_integer",
                "boolean": "after_boolean",
            }[base]
            before = effect[before_field]
            after = effect[after_field]
            if reconstructed[bucket].get(key, before) != before:
                errors.append(f"event {seq} effect {key} before does not match prior log")
            reconstructed[bucket][key] = after  # type: ignore[index]

        exact_after = record.get("state_after_exact")
        if canonical_digest(exact_after) != record.get("state_after_digest"):
            errors.append(f"event {seq} state_after digest does not match exact state")

    final_exact = report.get("world_state_exact", {})
    for bucket in ("fixed", "integers", "booleans"):
        for key, value in final_exact.get(bucket, {}).items():
            if key in reconstructed.get(bucket, {}) and reconstructed[bucket][key] != value:
                errors.append(f"final state for {key} does not match effect reconstruction")

    for item in report.get("causal_chain", []):
        seq = item.get("event_seq")
        if item.get("source_type") == "event":
            if not isinstance(seq, int) or seq not in by_seq:
                errors.append(f"causal change {item.get('change_seq')} has dangling event_seq {seq!r}")
                continue
            record = by_seq[seq]
            if item.get("event_ref") != f"event:{seq}":
                errors.append(f"causal change {item.get('change_seq')} has wrong event_ref")
            if item.get("event_id") != record.get("event_id"):
                errors.append(f"causal change {item.get('change_seq')} event_id mismatch")
            effect = item.get("effect", {})
            matching = [
                candidate
                for candidate in record.get("effects", [])
                if candidate.get("key") == effect.get("key")
                and candidate.get("after_value") == effect.get("after_value")
            ]
            if not matching:
                errors.append(
                    f"causal change {item.get('change_seq')} does not correspond to a real effect record"
                )

    if canonical_digest(final_exact) != report.get("world_state_digest"):
        errors.append("report world_state_digest does not match exact state")
    return errors
