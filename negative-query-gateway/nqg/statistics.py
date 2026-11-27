"""Per-generation, per-lab and residual-risk statistics.

Every number is derived only from the per-query :class:`QueryRecord` list, so a
report can be recomputed line by line from the recorded verdicts.
"""

from __future__ import annotations

from dataclasses import asdict

from .bloom import Generation
from .engine import EngineResult, QueryRecord


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def generation_statistics(result: EngineResult) -> list[dict]:
    """One statistics row per generation, including filter occupancy."""

    rows: list[dict] = []
    for generation in result.generations:
        queries = [q for q in result.queries if q.generation_id == generation.generation_id]
        negatives = [q for q in queries if q.backend_exists is False or q.decision == "intercepted"]
        released = [q for q in queries if q.decision == "released"]
        false_positives = [q for q in queries if q.false_positive]
        intercepted = [q for q in queries if q.decision == "intercepted"]
        rows.append(
            {
                "generation_id": generation.generation_id,
                "activated_at": generation.activated_at,
                "deactivated_at": generation.deactivated_at,
                "migrated_keys": generation.migrated_keys,
                "inserted_count": generation.inserted_count,
                "bit_array_size": generation.bit_array_size,
                "set_bit_count": generation.set_bit_count(),
                "occupancy_ratio": round(generation.occupancy_ratio(), 6),
                "total_queries": len(queries),
                "intercepted": len(intercepted),
                "released": len(released),
                "negative_queries": len(negatives),
                "false_positives": len(false_positives),
                # 拦截率 = 拦截数 / 该代查询总数
                "intercept_rate": round(_ratio(len(intercepted), len(queries)), 6),
                # 实测误判率 = 误判数 / 该代负查询数
                "measured_false_positive_rate": round(
                    _ratio(len(false_positives), len(negatives)), 6
                ),
                # 放行后不存在占比 = 误判数 / 放行数
                "release_error_rate": round(_ratio(len(false_positives), len(released)), 6),
            }
        )
    return rows


def _lab_of(key: bytes, lab_prefixes: tuple[str, ...]) -> str | None:
    for prefix in lab_prefixes:
        if key.startswith(prefix.encode("ascii")):
            return prefix
    return None


def lab_acceleration(result: EngineResult, lab_prefixes: tuple[str, ...]) -> list[dict]:
    """Negative-query acceleration broken down by research-group prefix."""

    rows: list[dict] = []
    for prefix in lab_prefixes:
        prefix_bytes = prefix.encode("ascii")
        queries = [q for q in result.queries if q.key.startswith(prefix_bytes)]
        negatives = [
            q for q in queries if q.backend_exists is False or q.decision == "intercepted"
        ]
        intercepted = [q for q in queries if q.decision == "intercepted"]
        false_positives = [q for q in queries if q.false_positive]
        released = [q for q in queries if q.decision == "released"]
        rows.append(
            {
                "lab_prefix": prefix,
                "total_queries": len(queries),
                "negative_queries": len(negatives),
                "intercepted": len(intercepted),
                "released": len(released),
                "false_positives": len(false_positives),
                "negative_intercept_rate": round(
                    _ratio(
                        len([q for q in negatives if q.decision == "intercepted"]),
                        len(negatives),
                    ),
                    6,
                ),
                "measured_false_positive_rate": round(
                    _ratio(len(false_positives), len(negatives)), 6
                ),
                "backend_lookups_saved": len(intercepted),
            }
        )
    return rows


def residual_scan(
    result: EngineResult,
    engine: "object",
    candidate_keys: list[bytes],
    backend_live: set[bytes],
) -> dict:
    """Structured residual scan split by protection mechanism."""

    from .bloom import CountingBloom

    current = result.generations[-1]
    bloom = CountingBloom(result.spec)
    deleted_keys = {record.key for record in result.deletes}
    active_tombstones = dict(getattr(engine, "tombstones", {}))

    protected_deleted: list[dict] = []
    residual_negatives: list[dict] = []
    for key in candidate_keys:
        if key in backend_live:
            continue
        counters = bloom.counter_snapshot(current, key)
        if not all(value > 0 for value in counters.values()):
            continue
        entry = {
            "key": key.decode("ascii", errors="replace"),
            "positions": bloom.positions(key),
            "residual_counters": dict(counters),
        }
        if key in deleted_keys:
            entry["protected_by_tombstone"] = key in active_tombstones
            protected_deleted.append(entry)
        else:
            residual_negatives.append(entry)
    return {
        "current_generation_id": current.generation_id,
        "protected_deleted_keys": protected_deleted,
        "residual_negative_candidates": residual_negatives,
    }


def recompute_totals(queries: list[QueryRecord]) -> dict:
    """Independent totals recomputed straight from the verdict stream."""

    total = len(queries)
    intercepted = sum(1 for q in queries if q.decision == "intercepted")
    released = total - intercepted
    false_positives = sum(1 for q in queries if q.false_positive)
    negatives = sum(
        1 for q in queries if q.backend_exists is False or q.decision == "intercepted"
    )
    return {
        "total_queries": total,
        "intercepted": intercepted,
        "released": released,
        "negative_queries": negatives,
        "false_positives": false_positives,
        "intercept_rate": round(_ratio(intercepted, total), 6),
        "measured_false_positive_rate": round(_ratio(false_positives, negatives), 6),
        "backend_lookups": released,
        "backend_lookups_saved": intercepted,
    }


def query_records_as_dicts(result: EngineResult) -> list[dict]:
    rows = []
    for record in result.queries:
        row = asdict(record)
        row["key"] = record.key.decode("ascii", errors="replace")
        row["positions"] = list(record.positions)
        rows.append(row)
    return rows
