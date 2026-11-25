from __future__ import annotations

from pathlib import Path
from typing import Any

from .ledger import build_generation_ledger, compare_adjacent, convert_fixed
from .util import canonical_hash
from .validation import effective_v1_zero_ms, load_seed_manifest, validate_v1, validate_v2


SOURCE_PRIORITY = {"v1": 1, "v2": 2, "v2_tombstone": 3, "wal": 4}


def _normalize_v1(snapshot: dict[str, Any], zero_ms: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    conversions: list[dict[str, Any]] = []
    for raw in snapshot["entries"]:
        entry_id = raw["entry_id"]
        relative_seconds = int(raw["relative_commit_seconds"])
        conversion = convert_fixed(
            conversion_id=f"migrate-{entry_id}",
            generation=1,
            kind="v1_relative_seconds_to_absolute_ms",
            raw_value=relative_seconds,
            scale=1000,
            rounding="nearest",
            table_name=raw["table_name"],
            row_key=raw["row_key"],
        )
        conversion["input_unit"] = "second"
        conversion["output_unit"] = "millisecond"
        conversion["pre_round_raw_value"] = conversion["raw_text"]
        conversion["pre_round_scale"] = 1000
        conversions.append(conversion)
        offset_ms = int(conversion["scaled_value"])
        entries.append(
            {
                "entry_id": entry_id,
                "source_format": "v1",
                "source_priority": SOURCE_PRIORITY["v1"],
                "declared_clock": int(raw.get("record_clock", 0)),
                "generation": 1,
                "canonical_seq": int(raw["txn_seq"]),
                "order_rank": 0,
                "txn_seq": int(raw["txn_seq"]),
                "table_name": raw["table_name"],
                "row_key": raw["row_key"],
                "op": "upsert" if raw["op"] == "upsert" else raw["op"],
                "before": raw.get("before"),
                "after": raw.get("after"),
                "commit_ms": int(zero_ms + offset_ms),
                "checkpoint_id": None,
                "checkpoint_watermark": None,
                "digest": canonical_hash(raw),
            }
        )
    return entries, conversions


def _normalize_v2(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in entries:
        normalized.append(
            {
                "entry_id": raw["entry_id"],
                "source_format": "v2",
                "source_priority": SOURCE_PRIORITY["v2"],
                "declared_clock": int(raw.get("record_clock", 0)),
                "generation": int(raw["generation"]),
                "canonical_seq": int(raw["canonical_seq"]),
                "order_rank": 0,
                "txn_seq": int(raw["txn_seq"]),
                "table_name": raw["table_name"],
                "row_key": raw["row_key"],
                "op": raw["op"],
                "before": raw.get("before"),
                "after": raw.get("after"),
                "commit_ms": int(raw["commit_ms"]),
                "checkpoint_id": raw.get("checkpoint_id"),
                "checkpoint_watermark": raw.get("checkpoint_watermark"),
                "digest": canonical_hash(raw),
            }
        )
    return normalized


def _tombstone_entries(
    v2_manifest: dict[str, Any], v2_entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {entry["entry_id"]: entry for entry in v2_entries}
    result: list[dict[str, Any]] = []
    for tombstone in v2_manifest.get("_tombstones", []):
        target = by_id.get(tombstone["entry_id"], {})
        result.append(
            {
                "entry_id": tombstone["tombstone_id"],
                "source_format": "v2_tombstone",
                "source_priority": SOURCE_PRIORITY["v2_tombstone"],
                "declared_clock": int(target.get("record_clock", 0)),
                "generation": int(tombstone["generation"]),
                "canonical_seq": int(target.get("canonical_seq", 0)),
                "order_rank": 1,
                "txn_seq": int(target.get("txn_seq", 0)),
                "table_name": tombstone["table_name"],
                "row_key": tombstone["row_key"],
                "op": "delete",
                "before": target.get("before"),
                "after": None,
                "commit_ms": int(target.get("commit_ms", 0)),
                "checkpoint_id": target.get("checkpoint_id"),
                "checkpoint_watermark": target.get("checkpoint_watermark"),
                "tombstone_id": tombstone["tombstone_id"],
                "digest": canonical_hash(tombstone),
            }
        )
    return result


def adjudicate(
    v1_entries: list[dict[str, Any]], v2_entries: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: dict[str, list[dict[str, Any]]] = {}
    for entry in [*v1_entries, *v2_entries]:
        candidates.setdefault(entry["entry_id"], []).append(entry)

    selected: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for entry_id in sorted(candidates):
        versions = candidates[entry_id]

        def rank(entry: dict[str, Any]) -> tuple[int, int, int]:
            return (
                int(entry["declared_clock"]),
                int(entry["source_priority"]),
                int(entry["generation"]),
            )

        winner = max(versions, key=rank)
        if len(versions) == 1:
            reason = f"only_available_in_{winner['source_format']}"
        else:
            loser = sorted(versions, key=rank)[-2]
            if winner["declared_clock"] != loser["declared_clock"]:
                reason = "higher_declared_logical_clock"
            elif winner["source_priority"] != loser["source_priority"]:
                reason = "equal_clock_source_format_priority_v2_over_v1"
            else:
                reason = "stable_generation_tie_break"
        winner_key = (
            f"clock={winner['declared_clock']};"
            f"source={winner['source_format']};"
            f"generation={winner['generation']}"
        )
        decision = {
            "entry_id": entry_id,
            "selected_source": winner["source_format"],
            "winner_key": winner_key,
            "reason": reason,
            "candidate_keys": [
                f"clock={item['declared_clock']};source={item['source_format']};"
                f"generation={item['generation']}"
                for item in sorted(versions, key=rank)
            ],
        }
        winner["selection"] = decision
        selected.append(winner)
        decisions.append(decision)
    return selected, decisions


def sort_chain(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        entries,
        key=lambda entry: (
            int(entry["generation"]),
            int(entry["canonical_seq"]),
            int(entry.get("order_rank", 0)),
            int(entry["txn_seq"]),
            str(entry["table_name"]),
            str(entry["row_key"]),
            str(entry["entry_id"]),
        ),
    )


def build_merge(root: Path, clock_ms: int) -> dict[str, Any]:
    seed = load_seed_manifest(root)
    v1_path = root / seed["paths"]["v1"]
    v2_manifest_path = root / seed["paths"]["v2_manifest"]
    v1 = validate_v1(v1_path)
    v2_manifest, v2_raw_entries, checkpoints = validate_v2(root)
    zero_ms = effective_v1_zero_ms(seed, clock_ms)
    v1_entries, conversions = _normalize_v1(v1, zero_ms)
    v2_entries = _normalize_v2(v2_raw_entries)
    selected, decisions = adjudicate(v1_entries, v2_entries)
    tombstones = _tombstone_entries(v2_manifest, v2_raw_entries)
    chain = sort_chain([*selected, *tombstones])
    for index, entry in enumerate(chain):
        entry["chain_order"] = index

    generation_commit: dict[int, int] = {}
    for entry in chain:
        generation = int(entry["generation"])
        generation_commit[generation] = max(
            generation_commit.get(generation, int(entry["commit_ms"])),
            int(entry["commit_ms"]),
        )

    scale = int(seed.get("ledger_scale", 100))
    ledgers: list[dict[str, Any]] = []
    parent: dict[str, Any] | None = None
    for generation in sorted(generation_commit):
        actual_ms = generation_commit[generation]
        lower_raw = actual_ms - __import__("fractions").Fraction(1, 1000)
        ledger = build_generation_ledger(
            generation=generation,
            actual_commit_ms=actual_ms,
            theoretical_lower_bound_ms=lower_raw,
            scale=scale,
            parent=parent,
        )
        ledgers.append(ledger)
        parent = ledger

    precision_flags: list[dict[str, Any]] = []
    tolerance = seed.get("precision_tolerance_ms", 2)
    for older, newer in zip(ledgers, ledgers[1:]):
        precision_flags.append(
            compare_adjacent(
                older,
                newer,
                tolerance_ms=tolerance,
                decision_depends_on_gap=True,
            )
        )

    clock_gates: list[dict[str, Any]] = []
    checkpoints_by_generation = {
        int(checkpoint["generation"]): checkpoint
        for checkpoint in checkpoints.values()
        if isinstance(checkpoint, dict) and "generation" in checkpoint
    }
    for ledger in ledgers:
        generation = int(ledger["generation"])
        checkpoint = checkpoints_by_generation.get(generation, {})
        commit_ms = int(checkpoint.get("commit_ms", ledger["actual_commit_ms"]))
        eligible = clock_ms >= commit_ms
        clock_gates.append(
            {
                "generation": generation,
                "checkpoint_id": checkpoint.get("checkpoint_id", f"cp-g{generation}"),
                "checkpoint_watermark": checkpoint.get("watermark"),
                "commit_ms": commit_ms,
                "injected_clock_ms": clock_ms,
                "clock_delta_ms": clock_ms - commit_ms,
                "eligible_to_commit": eligible,
                "decision": "commit_checkpoint" if eligible else "wait_for_injected_clock",
            }
        )

    boundary: dict[str, Any] | None = None
    if len(ledgers) >= 2:
        older = ledgers[0]
        newer = ledgers[1]
        gate_by_generation = {gate["generation"]: gate for gate in clock_gates}
        newer_gate = gate_by_generation[int(newer["generation"])]
        sensitive = any(flag["precision_sensitive"] for flag in precision_flags)
        if not newer_gate["eligible_to_commit"]:
            boundary_decision = "hold_before_generation_boundary"
        elif sensitive:
            boundary_decision = "advance_atomically_with_pinned_canonical_order"
        else:
            boundary_decision = "advance_at_generation_boundary"
        boundary = {
            "adjacent_generations": [int(older["generation"]), int(newer["generation"])],
            "injected_clock_ms": clock_ms,
            "checkpoint_commit_ms": newer_gate["commit_ms"],
            "checkpoint_watermark": newer_gate["checkpoint_watermark"],
            "watermark_replayed": True,
            "precision_sensitive": sensitive,
            "decision": boundary_decision,
            "mixed_generation_read_allowed": False,
        }

    merge_payload = {
        "format": "logchain-merged-view",
        "seed_id": seed["seed_id"],
        "logical_clock_ms": clock_ms,
        "effective_v1_zero_ms": zero_ms,
        "v1_clock_shift_ms": clock_ms - int(seed["logical_clock_ms"]),
        "migration_conversions": conversions,
        "decisions": decisions,
        "checkpoints": checkpoints,
        "v2_tombstones": v2_manifest.get("_tombstones", []),
        "generation_commit_ms": {str(key): value for key, value in generation_commit.items()},
        "ledger": {
            "scale": scale,
            "tolerance_ms": tolerance,
            "generations": ledgers,
            "precision_flags": precision_flags,
            "clock_gates": clock_gates,
            "generation_boundary": boundary,
            "adjudication_basis": "injected_logical_clock_ms_only",
        },
        "entries": chain,
    }
    merge_payload["chain_digest"] = canonical_hash(
        {key: value for key, value in merge_payload.items() if key != "chain_digest"}
    )
    merge_payload["view_id"] = f"view-{merge_payload['chain_digest'][:16]}"
    merge_payload["ledger_digest"] = canonical_hash(merge_payload["ledger"])
    return merge_payload
