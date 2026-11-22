from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .join import RuntimeData, pair_join_count, rows_digest, table_key_stats
from .models import BatchSpec, JoinPredicate, SampleSpec, Shape
from .precision import ErrorTracker, FLOAT_EPS, product_with_bound, ratio_with_bound, relative_difference_with_bound


@dataclass(frozen=True)
class Observation:
    seq: int
    arrival: int
    committed_count: int
    input_rows: int
    output_rows: int
    observed_selectivity: float
    drift_sources: list[dict[str, Any]]
    error_chain: dict[str, Any]
    result_digest: str
    shape_version: int
    edge_selectivities: dict[tuple[str, str, str, str], float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "arrival": self.arrival,
            "committed_count": self.committed_count,
            "input_rows": self.input_rows,
            "output_rows": self.output_rows,
            "observed_selectivity": self.observed_selectivity,
            "drift_sources": self.drift_sources,
            "error_chain": self.error_chain,
            "result_digest": self.result_digest,
            "shape_version": self.shape_version,
            "edge_selectivities": {
                "|".join(edge): value for edge, value in sorted(self.edge_selectivities.items())
            },
        }


def _table_batch_interval(
    committed_specs: list[BatchSpec],
    table: str,
) -> tuple[int, int] | None:
    arrivals = [batch.arrival for batch in committed_specs if batch.table == table]
    if not arrivals:
        return None
    return (min(arrivals), max(arrivals))


def _predicate_dict(predicate: JoinPredicate) -> dict[str, str]:
    return {
        "left": f"{predicate.left_table}.{predicate.left_column}",
        "right": f"{predicate.right_table}.{predicate.right_column}",
    }


def _estimated_join_selectivity(
    sample: SampleSpec,
    predicate: JoinPredicate,
) -> float:
    direct = predicate.edge_key
    reverse = (predicate.right_table, predicate.right_column, predicate.left_table, predicate.left_column)
    return sample.statistics.join_selectivities.get(
        direct, sample.statistics.join_selectivities.get(reverse, 1.0)
    )


def build_observation(
    sample: SampleSpec,
    data: RuntimeData,
    shape: Shape,
    committed_specs: list[BatchSpec],
    result_rows: list[dict[str, Any]],
    shape_version: int,
    seq: int,
) -> Observation:
    arrival = committed_specs[-1].arrival
    global_interval = (committed_specs[0].arrival, committed_specs[-1].arrival)
    tracker = ErrorTracker()
    table_counts = {table.name: data.row_count(table.name) for table in sample.tables}
    input_rows = sum(table_counts.values())
    input_product = product_with_bound(
        [max(1, count) for count in table_counts.values()],
        "observed_input_product",
        table_counts,
        global_interval,
        tracker,
    )
    output_rows = len(result_rows)
    observed_selectivity = ratio_with_bound(
        float(output_rows),
        0.0,
        input_product,
        math.ulp(input_product) if input_product else 0.0,
        "observed_join_selectivity",
        table_counts,
        global_interval,
        tracker,
    )

    edge_selectivities: dict[tuple[str, str, str, str], float] = {}
    source_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for predicate in sample.plan.joins:
        left_rows = data.tables.get(predicate.left_table, [])
        right_rows = data.tables.get(predicate.right_table, [])
        if not left_rows or not right_rows:
            continue
        left_key = f"{predicate.left_table}.{predicate.left_column}"
        right_key = f"{predicate.right_table}.{predicate.right_column}"
        left_counts, left_ndv, left_skew = table_key_stats(left_rows, left_key)
        right_counts, right_ndv, right_skew = table_key_stats(right_rows, right_key)
        pair_count = pair_join_count(left_rows, left_key, right_rows, right_key)
        denominator = max(1, len(left_rows)) * max(1, len(right_rows))
        observed_join_selectivity = ratio_with_bound(
            float(pair_count),
            0.0,
            float(denominator),
            math.ulp(float(denominator)),
            f"pair_selectivity:{left_key}={right_key}",
            (predicate.left_table, predicate.right_table),
            global_interval,
            tracker,
            left_rows=len(left_rows),
            right_rows=len(right_rows),
        )
        edge_selectivities[predicate.edge_key] = observed_join_selectivity
        estimated_join_selectivity = _estimated_join_selectivity(sample, predicate)
        side_specs = [
            (predicate.left_table, predicate.left_column, left_ndv, left_skew),
            (predicate.right_table, predicate.right_column, right_ndv, right_skew),
        ]
        for table, column, observed_ndv, observed_skew in side_specs:
            stats = sample.statistics.tables[table].columns[column]
            interval = _table_batch_interval(committed_specs, table)
            local_tracker = ErrorTracker()
            ndv_drift, _ = relative_difference_with_bound(
                float(observed_ndv),
                float(stats.ndv),
                f"ndv_drift:{table}.{column}",
                (table,),
                interval,
                local_tracker,
            )
            selectivity_drift, _ = relative_difference_with_bound(
                observed_join_selectivity,
                estimated_join_selectivity,
                f"join_selectivity_drift:{table}.{column}",
                (predicate.left_table, predicate.right_table),
                interval,
                local_tracker,
            )
            components = {
                "ndv_drift": ndv_drift,
                "join_selectivity_drift": selectivity_drift,
            }
            if stats.skew_fraction is not None:
                skew_drift, _ = relative_difference_with_bound(
                    observed_skew,
                    stats.skew_fraction,
                    f"skew_drift:{table}.{column}",
                    (table,),
                    interval,
                    local_tracker,
                )
                components["skew_drift"] = skew_drift
            drift_ratio = max(components.values())
            local_chain = local_tracker.to_chain()
            for step in local_chain["steps"]:
                tracker.steps.append(step)
            tracker.total_bound = math.fsum([tracker.total_bound, local_chain["accumulated_error_bound"]])
            candidate = {
                "source": {"table": table, "join_key": column},
                "predicate": _predicate_dict(predicate),
                "drift_ratio": drift_ratio,
                "error_bound": local_chain["accumulated_error_bound"],
                "components": components,
                "observed": {
                    "ndv": observed_ndv,
                    "join_selectivity": observed_join_selectivity,
                    "max_key_frequency": observed_skew,
                    "table_rows": table_counts[table],
                },
                "estimated": {
                    "ndv": stats.ndv,
                    "join_selectivity": estimated_join_selectivity,
                    "skew_fraction": stats.skew_fraction,
                    "table_rows": sample.statistics.tables[table].row_count,
                },
                "batch_interval": list(interval) if interval else None,
                "error_chain": local_chain,
            }
            key = (table, column)
            previous = source_by_key.get(key)
            if previous is None or candidate["drift_ratio"] > previous["drift_ratio"]:
                source_by_key[key] = candidate

    drift_sources = sorted(
        source_by_key.values(),
        key=lambda source: (-source["drift_ratio"], source["source"]["table"], source["source"]["join_key"]),
    )
    chain = tracker.to_chain()
    chain["precision_sensitive"] = False
    chain["error_bound_budget"] = sample.config.error_bound_budget
    chain["error_budget_exceeded"] = chain["accumulated_error_bound"] > sample.config.error_bound_budget
    return Observation(
        seq=seq,
        arrival=arrival,
        committed_count=len(committed_specs),
        input_rows=input_rows,
        output_rows=output_rows,
        observed_selectivity=observed_selectivity,
        drift_sources=drift_sources,
        error_chain=chain,
        result_digest=rows_digest(result_rows),
        shape_version=shape_version,
        edge_selectivities=edge_selectivities,
    )
