from __future__ import annotations

import bisect
import hashlib
import json
import random
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

import numpy as np

from .db import json_dumps, json_loads
from .generator import parse_ts
from .model import PredicateEstimate, SummaryIssue


BASE_TABLES: tuple[str, ...] = (
    "regions",
    "bands",
    "rangers",
    "terminals",
    "routes",
    "base_stations",
    "patrol_logs",
)

STAT_COLUMNS: dict[str, tuple[str, ...]] = {
    "regions": ("region_id", "remoteness", "risk_level"),
    "bands": ("band_id", "freq_low_mhz", "freq_high_mhz"),
    "rangers": ("ranger_id", "home_region_id", "active"),
    "terminals": ("terminal_id", "ranger_id", "home_region_id", "band_id", "active"),
    "routes": ("route_id", "region_id", "patrol_km"),
    "base_stations": ("station_id", "region_id", "band_id", "coverage_km"),
    "patrol_logs": (
        "log_id",
        "ranger_id",
        "terminal_id",
        "route_id",
        "region_id",
        "band_id",
        "station_id",
        "logged_at",
        "fire_risk",
        "signal_dbm",
        "duration_s",
    ),
}

HISTOGRAM_COLUMNS: tuple[tuple[str, str], ...] = (
    ("patrol_logs", "region_id"),
    ("patrol_logs", "band_id"),
    ("patrol_logs", "logged_at"),
    ("patrol_logs", "terminal_id"),
    ("patrol_logs", "ranger_id"),
    ("patrol_logs", "route_id"),
    ("patrol_logs", "station_id"),
    ("terminals", "band_id"),
    ("terminals", "home_region_id"),
    ("rangers", "home_region_id"),
    ("routes", "region_id"),
    ("base_stations", "band_id"),
)

JOIN_DOMAIN_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("patrol_logs", "ranger_id", "rangers", "ranger_id"),
    ("patrol_logs", "terminal_id", "terminals", "terminal_id"),
    ("patrol_logs", "route_id", "routes", "route_id"),
    ("patrol_logs", "region_id", "regions", "region_id"),
    ("patrol_logs", "band_id", "bands", "band_id"),
    ("patrol_logs", "station_id", "base_stations", "station_id"),
    ("terminals", "ranger_id", "rangers", "ranger_id"),
    ("terminals", "home_region_id", "regions", "region_id"),
    ("terminals", "band_id", "bands", "band_id"),
    ("routes", "region_id", "regions", "region_id"),
    ("base_stations", "region_id", "regions", "region_id"),
    ("base_stations", "band_id", "bands", "band_id"),
)

COMBO_SPEC = ("patrol_logs", "region_id", "patrol_logs", "band_id")
MAX_SAMPLE_BLEND = 0.30


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def value_for_stats(table: str, column: str, value: Any) -> Any:
    if value is None:
        return None
    if table == "patrol_logs" and column == "logged_at":
        return int(parse_ts(str(value)).timestamp())
    return value


def display_value(column: str, value: Any) -> Any:
    if column == "logged_at" and value is not None:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    return value


def _sample_seed(seed: int, version: str, table: str, column: str) -> int:
    text = f"{seed}:{version}:{table}.{column}".encode("utf-8")
    return int(hashlib.sha256(text).hexdigest()[:16], 16)


def _fetch_column_values(
    conn: sqlite3.Connection, table: str, column: str
) -> list[Any]:
    rows = conn.execute(
        f"SELECT {column} FROM {table} ORDER BY rowid"
    ).fetchall()
    return [value_for_stats(table, column, row[0]) for row in rows]


def _sample_values(
    values: list[Any], sample_size: int, seed: int
) -> list[Any]:
    clean = values
    if sample_size <= 0 or not clean:
        return []
    size = min(sample_size, len(clean))
    indices = sorted(random.Random(seed).sample(range(len(clean)), size))
    return [clean[index] for index in indices]


def build_equal_depth_histogram(
    values: Iterable[Any], requested_buckets: int
) -> list[dict[str, Any]]:
    clean = np.asarray(
        [
            float(value)
            for value in values
            if value is not None and value == value
        ],
        dtype=np.float64,
    )
    if clean.size == 0:
        return []
    clean.sort()
    bucket_total = max(1, min(int(requested_buckets), int(clean.size)))
    if float(clean[0]) == float(clean[-1]):
        return [
            {
                "bucket_no": 0,
                "lower_value": float(clean[0]),
                "upper_value": float(clean[0]),
                "lower_inclusive": 1,
                "upper_inclusive": 1,
                "observed_count": int(clean.size),
                "ndv": 1,
            }
        ]

    edges = [float(clean[0])]
    for index in range(1, bucket_total):
        sorted_index = (index * int(clean.size)) // bucket_total - 1
        sorted_index = max(0, min(int(clean.size) - 1, sorted_index))
        edges.append(float(clean[sorted_index]))
    edges.append(float(clean[-1]))
    edge_array = np.asarray(edges, dtype=np.float64)
    if edge_array.size < 2:
        edge_array = np.asarray([float(clean[0]), float(clean[-1])])
    assignment = np.searchsorted(edge_array, clean, side="left") - 1
    assignment = np.clip(assignment, 0, edge_array.size - 2)
    counts = np.bincount(assignment, minlength=edge_array.size - 1).astype(int)
    distinct_counts = np.asarray(
        [
            np.unique(clean[assignment == bucket_no]).size
            for bucket_no in range(edge_array.size - 1)
        ],
        dtype=int,
    )

    nonempty = [
        bucket_no
        for bucket_no, observed_count in enumerate(counts)
        if int(observed_count) > 0
    ]
    buckets: list[dict[str, Any]] = []
    for compact_no, bucket_no in enumerate(nonempty):
        buckets.append(
            {
                "bucket_no": compact_no,
                "lower_value": float(edge_array[bucket_no]),
                "upper_value": float(edge_array[bucket_no + 1]),
                "lower_inclusive": 1 if compact_no == 0 else 0,
                "upper_inclusive": 1,
                "observed_count": int(counts[bucket_no]),
                "ndv": int(distinct_counts[bucket_no]),
            }
        )
    return buckets


def _combo_counts(
    conn: sqlite3.Connection,
    *,
    sample_size: int | None,
    seed: int,
) -> tuple[list[tuple[int, int, int]], int]:
    rows = conn.execute(
        """
        SELECT log_id, region_id, band_id
        FROM patrol_logs
        ORDER BY log_id
        """
    ).fetchall()
    population = len(rows)
    if sample_size is None:
        selected = rows
    else:
        size = min(max(0, sample_size), population)
        indices = sorted(random.Random(seed).sample(range(population), size))
        selected = [rows[index] for index in indices]
    counts: dict[tuple[int, int], int] = {}
    for row in selected:
        key = (int(row["region_id"]), int(row["band_id"]))
        counts[key] = counts.get(key, 0) + 1
    return (
        [
            (left, right, count)
            for (left, right), count in sorted(counts.items())
        ],
        len(selected),
    )


def _domain_rows(
    conn: sqlite3.Connection, table: str, column: str, where: str | None = None
) -> tuple[int, set[Any]]:
    where_sql = f" AND {where}" if where else ""
    rows = conn.execute(
        f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL{where_sql}"
    ).fetchall()
    return len(rows), {row[0] for row in rows}


def build_statistics(
    conn: sqlite3.Connection,
    version: str,
    *,
    sample_size: int = 512,
    combo_sample_size: int = 256,
    bucket_count: int = 8,
    combo_source: str = "sample",
    note: str = "",
    replace: bool = False,
) -> None:
    if combo_source not in {"sample", "full"}:
        raise ValueError("combo_source must be 'sample' or 'full'")
    exists = conn.execute(
        "SELECT 1 FROM stat_versions WHERE version = ?", (version,)
    ).fetchone()
    if exists and not replace:
        raise ValueError(f"statistics version already exists: {version}")
    if exists:
        conn.execute("DELETE FROM stat_versions WHERE version = ?", (version,))

    seed_row = conn.execute(
        "SELECT seed FROM generation_meta WHERE singleton = 1"
    ).fetchone()
    seed = int(seed_row[0]) if seed_row else 0
    created_at = utc_now()
    conn.execute(
        """
        INSERT INTO stat_versions
            (version, created_at, seed, note, sample_size, combo_source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (version, created_at, seed, note, sample_size, combo_source),
    )

    for table in BASE_TABLES:
        row_count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        conn.execute(
            "INSERT INTO stat_row_counts (version, table_name, row_count) VALUES (?, ?, ?)",
            (version, table, row_count),
        )

    values_cache: dict[tuple[str, str], list[Any]] = {}
    for table, columns in STAT_COLUMNS.items():
        for column in columns:
            values = _fetch_column_values(conn, table, column)
            values_cache[(table, column)] = values
            non_null = [value for value in values if value is not None]
            sampled = _sample_values(
                values,
                sample_size,
                _sample_seed(seed, version, table, column),
            )
            min_value = min(non_null) if non_null else None
            max_value = max(non_null) if non_null else None
            conn.execute(
                """
                INSERT INTO stat_column_stats
                    (version, table_name, column_name, ndv, null_count,
                     min_value, max_value, sample_size, population_rows)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version,
                    table,
                    column,
                    len(set(non_null)),
                    len(values) - len(non_null),
                    str(min_value) if min_value is not None else None,
                    str(max_value) if max_value is not None else None,
                    len(sampled),
                    len(values),
                ),
            )
            conn.execute(
                """
                INSERT INTO stat_samples
                    (version, table_name, column_name, sample_size,
                     population_rows, values_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    version,
                    table,
                    column,
                    len(sampled),
                    len(values),
                    json_dumps(sampled),
                ),
            )

    for table, column in HISTOGRAM_COLUMNS:
        values = values_cache[(table, column)]
        buckets = build_equal_depth_histogram(values, bucket_count)
        sample_size_for_column = int(
            conn.execute(
                """
                SELECT sample_size FROM stat_samples
                WHERE version = ? AND table_name = ? AND column_name = ?
                """,
                (version, table, column),
            ).fetchone()[0]
        )
        cursor = conn.execute(
            """
            INSERT INTO stat_histograms
                (version, table_name, column_name, requested_bucket_count,
                 bucket_count, total_rows, null_count, sample_size, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version,
                table,
                column,
                bucket_count,
                len(buckets),
                len(values),
                sum(1 for value in values if value is None),
                sample_size_for_column,
                created_at,
            ),
        )
        hist_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO stat_histogram_buckets
                (hist_id, bucket_no, lower_value, upper_value,
                 lower_inclusive, upper_inclusive, observed_count, ndv)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    hist_id,
                    bucket["bucket_no"],
                    bucket["lower_value"],
                    bucket["upper_value"],
                    bucket["lower_inclusive"],
                    bucket["upper_inclusive"],
                    bucket["observed_count"],
                    bucket["ndv"],
                )
                for bucket in buckets
            ),
        )

    left_table, left_column, right_table, right_column = COMBO_SPEC
    combo_rows = int(conn.execute(f"SELECT COUNT(*) FROM {left_table}").fetchone()[0])
    actual_combo_sample_size = None if combo_source == "full" else combo_sample_size
    counts, sampled_rows = _combo_counts(
        conn,
        sample_size=actual_combo_sample_size,
        seed=_sample_seed(seed, version, left_table, left_column + "+" + right_column),
    )
    cursor = conn.execute(
        """
        INSERT INTO stat_combo_histograms
            (version, left_table, left_column, right_table, right_column,
             total_rows, sample_size, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version,
            left_table,
            left_column,
            right_table,
            right_column,
            combo_rows,
            sampled_rows,
            combo_source,
            created_at,
        ),
    )
    combo_id = int(cursor.lastrowid)
    conn.executemany(
        """
        INSERT INTO stat_combo_buckets
            (combo_id, left_value, right_value, observed_count)
        VALUES (?, ?, ?, ?)
        """,
        ((combo_id, left, right, count) for left, right, count in counts),
    )

    for left_table, left_column, right_table, right_column in JOIN_DOMAIN_SPECS:
        left_rows, left_values = _domain_rows(conn, left_table, left_column)
        right_where = (
            "active = 1"
            if right_table == "terminals" and right_column == "terminal_id"
            else None
        )
        right_rows, right_values = _domain_rows(
            conn, right_table, right_column, right_where
        )
        overlap = len(left_values & right_values)
        left_ndv = len(left_values)
        right_ndv = len(right_values)
        containment = overlap / left_ndv if left_ndv else 0.0
        multiplicity = right_rows / right_ndv if right_ndv else 0.0
        conn.execute(
            """
            INSERT INTO stat_join_domains
                (version, left_table, left_column, right_table, right_column,
                 left_ndv, right_ndv, overlap_ndv, left_rows, right_rows,
                 containment, right_multiplicity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version,
                left_table,
                left_column,
                right_table,
                right_column,
                left_ndv,
                right_ndv,
                overlap,
                left_rows,
                right_rows,
                containment,
                multiplicity,
            ),
        )

    conn.commit()


def load_histogram(
    conn: sqlite3.Connection,
    version: str,
    table: str,
    column: str,
) -> tuple[sqlite3.Row, list[dict[str, Any]]]:
    histogram = conn.execute(
        """
        SELECT * FROM stat_histograms
        WHERE version = ? AND table_name = ? AND column_name = ?
        """,
        (version, table, column),
    ).fetchone()
    if histogram is None:
        raise LookupError(f"missing histogram for {table}.{column} in {version}")
    buckets = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM stat_histogram_buckets
            WHERE hist_id = ? ORDER BY bucket_no
            """,
            (histogram["hist_id"],),
        )
    ]
    return histogram, buckets


def load_sample_values(
    conn: sqlite3.Connection, version: str, table: str, column: str
) -> list[Any]:
    row = conn.execute(
        """
        SELECT values_json FROM stat_samples
        WHERE version = ? AND table_name = ? AND column_name = ?
        """,
        (version, table, column),
    ).fetchone()
    return json_loads(row[0], []) if row else []


def _bucket_for_value(edges: list[float], value: float) -> int:
    index = bisect.bisect_left(edges, value) - 1
    return max(0, min(len(edges) - 2, index))


def _uniform_equality_fraction(bucket: dict[str, Any]) -> float:
    lower = float(bucket["lower_value"])
    upper = float(bucket["upper_value"])
    span = max(
        1.0,
        upper - lower + (1.0 if bucket["lower_inclusive"] else 0.0),
    )
    return min(1.0, 1.0 / span)


def _bucket_overlap_fraction(
    bucket: dict[str, Any], lower: float, upper: float
) -> float:
    bucket_lower = float(bucket["lower_value"])
    bucket_upper = float(bucket["upper_value"])
    overlap_lower = max(bucket_lower, lower)
    overlap_upper = min(bucket_upper, upper)
    if overlap_upper < overlap_lower:
        return 0.0
    width = bucket_upper - bucket_lower
    if width <= 0:
        return 1.0 if bucket_lower >= lower and bucket_lower <= upper else 0.0
    return max(0.0, min(1.0, (overlap_upper - overlap_lower) / width))


def _blend(
    histogram_selectivity: float,
    sample_selectivity: float,
    sample_size: int,
    total_rows: int,
    *,
    expected_sample_hits: float,
) -> tuple[float, float]:
    if sample_size <= 0 or total_rows <= 0 or expected_sample_hits < 8:
        return histogram_selectivity, 0.0
    weight = min(
        MAX_SAMPLE_BLEND,
        sample_size / (sample_size + max(total_rows / 8.0, 1.0)),
    )
    final = (1.0 - weight) * histogram_selectivity + weight * sample_selectivity
    return max(0.0, min(1.0, final)), weight


def estimate_predicate(
    conn: sqlite3.Connection,
    version: str,
    kind: str,
    *,
    value: int | None = None,
    start_ts: str | None = None,
    end_ts: str | None = None,
) -> PredicateEstimate:
    table = "patrol_logs"
    if kind == "region":
        column = "region_id"
        operator = "="
        literal: Any = int(value)
        predicate = f"patrol_logs.region_id = {value}"
    elif kind == "band":
        column = "band_id"
        operator = "="
        literal = int(value)
        predicate = f"patrol_logs.band_id = {value}"
    elif kind == "time":
        column = "logged_at"
        operator = "BETWEEN"
        literal = [start_ts, end_ts]
        predicate = f"patrol_logs.logged_at BETWEEN {start_ts} AND {end_ts}"
    else:
        raise ValueError(f"unsupported predicate kind: {kind}")

    histogram, buckets = load_histogram(conn, version, table, column)
    total_rows = int(histogram["total_rows"])
    sample_values = load_sample_values(conn, version, table, column)
    sample_size = len(sample_values)
    edges = [float(bucket["lower_value"]) for bucket in buckets]
    edges.append(float(buckets[-1]["upper_value"]))
    hits: list[dict[str, Any]] = []
    histogram_selectivity = 0.0
    sample_selectivity = 0.0
    sample_weight = 0.0
    expected_hits = 0.0

    if operator == "=":
        numeric = float(literal)
        bucket_no = _bucket_for_value(edges, numeric)
        bucket = buckets[bucket_no]
        uniform_fraction = _uniform_equality_fraction(bucket)
        bucket_share = (
            float(bucket["observed_count"]) / total_rows if total_rows else 0.0
        )
        histogram_selectivity = bucket_share * uniform_fraction
        sample_hits = sum(1 for sample in sample_values if float(sample) == numeric)
        sample_selectivity = sample_hits / sample_size if sample_size else 0.0

        lower = float(bucket["lower_value"])
        upper = float(bucket["upper_value"])
        bucket_samples = [
            float(sample)
            for sample in sample_values
            if (
                (sample >= lower if bucket["lower_inclusive"] else sample > lower)
                and (sample <= upper if bucket["upper_inclusive"] else sample < upper)
            )
        ]
        if bucket_samples:
            span = max(
                1.0,
                upper - lower + (1.0 if bucket["lower_inclusive"] else 0.0),
            )
            refined_fraction = (len([s for s in bucket_samples if s == numeric]) + 0.5) / (
                len(bucket_samples) + 0.5 * span
            )
            refined_fraction = max(uniform_fraction * 0.25, refined_fraction)
            sample_weight = len(bucket_samples) / (
                len(bucket_samples) + max(total_rows / 80.0, 1.0)
            )
            sample_weight = min(1.0, sample_weight)
            final_selectivity = bucket_share * (
                (1.0 - sample_weight) * uniform_fraction
                + sample_weight * refined_fraction
            )
        else:
            final_selectivity = histogram_selectivity
        formula = (
            "P(eq)=bucket_count/total_rows * "
            "[(1-w)*uniform_1/span + w*smoothed_sample_fraction]"
        )
        hits.append(
            {
                "bucket_no": bucket_no,
                "lower_value": display_value(column, bucket["lower_value"]),
                "upper_value": display_value(column, bucket["upper_value"]),
                "lower_inclusive": bool(bucket["lower_inclusive"]),
                "upper_inclusive": bool(bucket["upper_inclusive"]),
                "bucket_count": int(bucket["observed_count"]),
                "uniform_partial_fraction": uniform_fraction,
                "bucket_contribution": histogram_selectivity,
                "sample_fraction": sample_selectivity,
            }
        )
        expected_hits = histogram_selectivity * sample_size
    else:
        start_epoch = float(parse_ts(str(start_ts)).timestamp())
        end_epoch = float(parse_ts(str(end_ts)).timestamp())
        if end_epoch < start_epoch:
            raise ValueError("time window end precedes start")
        for bucket in buckets:
            fraction = _bucket_overlap_fraction(bucket, start_epoch, end_epoch)
            if fraction <= 0:
                continue
            bucket_share = (
                float(bucket["observed_count"]) / total_rows if total_rows else 0.0
            )
            contribution = bucket_share * fraction
            histogram_selectivity += contribution
            hits.append(
                {
                    "bucket_no": int(bucket["bucket_no"]),
                    "lower_value": display_value(column, bucket["lower_value"]),
                    "upper_value": display_value(column, bucket["upper_value"]),
                    "lower_inclusive": bool(bucket["lower_inclusive"]),
                    "upper_inclusive": bool(bucket["upper_inclusive"]),
                    "bucket_count": int(bucket["observed_count"]),
                    "uniform_partial_fraction": fraction,
                    "bucket_contribution": contribution,
                }
            )
        sample_hits = sum(
            1 for sample in sample_values if start_epoch <= float(sample) <= end_epoch
        )
        sample_selectivity = sample_hits / sample_size if sample_size else 0.0
        expected_hits = histogram_selectivity * sample_size
        final_selectivity, sample_weight = _blend(
            histogram_selectivity,
            sample_selectivity,
            sample_size,
            total_rows,
            expected_sample_hits=expected_hits,
        )
        formula = (
            "P(range)=sum(bucket_count/total_rows * overlap_width/bucket_width); "
            "final=(1-w)*hist+w*sample when sample hits are sufficient"
        )

    return PredicateEstimate(
        predicate=predicate,
        column=column,
        operator=operator,
        literal=literal,
        histogram_selectivity=max(0.0, min(1.0, histogram_selectivity)),
        sample_selectivity=max(0.0, min(1.0, sample_selectivity)),
        final_selectivity=max(0.0, min(1.0, final_selectivity)),
        sample_size=sample_size,
        total_rows=total_rows,
        sample_weight=sample_weight,
        hit_buckets=hits,
        formula=formula,
    )


def combo_histogram_info(
    conn: sqlite3.Connection, version: str
) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
    left_table, left_column, right_table, right_column = COMBO_SPEC
    combo = conn.execute(
        """
        SELECT * FROM stat_combo_histograms
        WHERE version = ? AND left_table = ? AND left_column = ?
          AND right_table = ? AND right_column = ?
        """,
        (version, left_table, left_column, right_table, right_column),
    ).fetchone()
    if combo is None:
        raise LookupError(f"missing region-band combo statistics in {version}")
    buckets = conn.execute(
        """
        SELECT * FROM stat_combo_buckets
        WHERE combo_id = ? ORDER BY left_value, right_value
        """,
        (combo["combo_id"],),
    ).fetchall()
    return combo, buckets


def estimate_combo_selectivity(
    conn: sqlite3.Connection,
    version: str,
    region_id: int,
    band_id: int,
    independence_selectivity: float,
) -> dict[str, Any]:
    combo, buckets = combo_histogram_info(conn, version)
    match = next(
        (
            row
            for row in buckets
            if int(row["left_value"]) == int(region_id)
            and int(row["right_value"]) == int(band_id)
        ),
        None,
    )
    source = str(combo["source"])
    sample_size = int(combo["sample_size"])
    total_rows = int(combo["total_rows"])
    fallback = False
    if match is None:
        observed_count = 0
        if source == "full":
            selectivity = 0.0
        else:
            selectivity = independence_selectivity
            fallback = True
    else:
        observed_count = int(match["observed_count"])
        denominator = sample_size if source == "sample" else total_rows
        selectivity = observed_count / denominator if denominator else 0.0
    return {
        "region_id": int(region_id),
        "band_id": int(band_id),
        "scope": "independence" if fallback else f"combo_histogram:{source}",
        "selectivity": max(0.0, min(1.0, selectivity)),
        "independence_selectivity": independence_selectivity,
        "observed_count": observed_count,
        "source": source,
        "sample_size": sample_size,
        "total_rows": total_rows,
        "fallback": fallback,
        "formula": (
            "P(region,band)=independence fallback (pair absent)"
            if fallback
            else f"P(region,band)={observed_count}/{sample_size if source == 'sample' else total_rows}"
        ),
    }


def validate_summary(
    conn: sqlite3.Connection, version: str
) -> list[SummaryIssue]:
    version_row = conn.execute(
        "SELECT * FROM stat_versions WHERE version = ?", (version,)
    ).fetchone()
    if version_row is None:
        return [
            SummaryIssue(
                table_name="<summary>",
                column_name=None,
                reason=f"统计摘要版本不存在: {version}",
                expected=version,
                actual=None,
            )
        ]
    issues: list[SummaryIssue] = []

    for table in BASE_TABLES:
        actual_rows = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        row_stat = conn.execute(
            """
            SELECT row_count FROM stat_row_counts
            WHERE version = ? AND table_name = ?
            """,
            (version, table),
        ).fetchone()
        if row_stat is None:
            issues.append(
                SummaryIssue(table, None, "缺少基表行数摘要", actual_rows, None)
            )
        elif int(row_stat["row_count"]) != actual_rows:
            issues.append(
                SummaryIssue(
                    table,
                    None,
                    "摘要基表行数与当前基表不一致",
                    int(row_stat["row_count"]),
                    actual_rows,
                )
            )

    expected_histograms = set(HISTOGRAM_COLUMNS)
    actual_histograms = {
        (row["table_name"], row["column_name"])
        for row in conn.execute(
            "SELECT table_name, column_name FROM stat_histograms WHERE version = ?",
            (version,),
        )
    }
    for table, column in sorted(expected_histograms - actual_histograms):
        issues.append(
            SummaryIssue(table, column, "缺少等深直方图摘要", "present", "missing")
        )

    for histogram in conn.execute(
        """
        SELECT * FROM stat_histograms
        WHERE version = ? ORDER BY table_name, column_name
        """,
        (version,),
    ):
        table = str(histogram["table_name"])
        column = str(histogram["column_name"])
        values = _fetch_column_values(conn, table, column)
        actual_rows = len(values)
        if int(histogram["total_rows"]) != actual_rows:
            issues.append(
                SummaryIssue(
                    table,
                    column,
                    "直方图记录的总行数与基表不一致",
                    int(histogram["total_rows"]),
                    actual_rows,
                )
            )
        bucket_rows = conn.execute(
            """
            SELECT * FROM stat_histogram_buckets
            WHERE hist_id = ? ORDER BY bucket_no
            """,
            (histogram["hist_id"],),
        ).fetchall()
        bucket_sum = sum(int(row["observed_count"]) for row in bucket_rows)
        if bucket_sum != int(histogram["total_rows"]):
            issues.append(
                SummaryIssue(
                    table,
                    column,
                    "直方图桶计数之和与摘要总行数不一致",
                    int(histogram["total_rows"]),
                    bucket_sum,
                )
            )
        non_null = [value for value in values if value is not None]
        if int(histogram["null_count"]) != actual_rows - len(non_null):
            issues.append(
                SummaryIssue(
                    table,
                    column,
                    "直方图 NULL 计数与基表不一致",
                    int(histogram["null_count"]),
                    actual_rows - len(non_null),
                )
            )
        if bucket_rows and non_null:
            edges = [float(bucket_rows[0]["lower_value"])]
            edges.extend(float(bucket["upper_value"]) for bucket in bucket_rows)
            recomputed = [0] * len(bucket_rows)
            for value in non_null:
                recomputed[_bucket_for_value(edges, float(value))] += 1
            for bucket, count in zip(bucket_rows, recomputed):
                if int(bucket["observed_count"]) != count:
                    issues.append(
                        SummaryIssue(
                            table,
                            column,
                            f"第 {int(bucket['bucket_no'])} 号桶计数与基表重算不一致",
                            int(bucket["observed_count"]),
                            count,
                        )
                    )

    combo, combo_buckets = combo_histogram_info(conn, version)
    actual_logs = int(conn.execute("SELECT COUNT(*) FROM patrol_logs").fetchone()[0])
    if int(combo["total_rows"]) != actual_logs:
        issues.append(
            SummaryIssue(
                "patrol_logs",
                "region_id+band_id",
                "区域频段组合摘要总行数与基表不一致",
                int(combo["total_rows"]),
                actual_logs,
            )
        )
    if combo["source"] == "full":
        combo_sum = sum(int(row["observed_count"]) for row in combo_buckets)
        if combo_sum != actual_logs:
            issues.append(
                SummaryIssue(
                    "patrol_logs",
                    "region_id+band_id",
                    "全量区域频段组合桶计数之和与基表不一致",
                    actual_logs,
                    combo_sum,
                )
            )
    elif int(combo["sample_size"]) > actual_logs:
        issues.append(
            SummaryIssue(
                "patrol_logs",
                "region_id+band_id",
                "组合摘要采样行数超过基表行数",
                int(combo["sample_size"]),
                actual_logs,
            )
        )

    return issues


def stat_fingerprint(conn: sqlite3.Connection, version: str) -> str:
    payload: dict[str, Any] = {"version": version}
    payload["row_counts"] = [
        list(row)
        for row in conn.execute(
            """
            SELECT table_name, row_count FROM stat_row_counts
            WHERE version = ? ORDER BY table_name
            """,
            (version,),
        )
    ]
    payload["histograms"] = []
    for histogram in conn.execute(
        """
        SELECT hist_id, table_name, column_name, bucket_count, total_rows,
               null_count, sample_size
        FROM stat_histograms WHERE version = ?
        ORDER BY table_name, column_name
        """,
        (version,),
    ):
        buckets = [
            list(row)
            for row in conn.execute(
                """
                SELECT bucket_no, lower_value, upper_value, lower_inclusive,
                       upper_inclusive, observed_count, ndv
                FROM stat_histogram_buckets WHERE hist_id = ?
                ORDER BY bucket_no
                """,
                (histogram["hist_id"],),
            )
        ]
        payload["histograms"].append([list(histogram), buckets])
    payload["combo"] = []
    for combo in conn.execute(
        """
        SELECT combo_id, source, total_rows, sample_size
        FROM stat_combo_histograms WHERE version = ?
        """,
        (version,),
    ):
        buckets = [
            list(row)
            for row in conn.execute(
                """
                SELECT left_value, right_value, observed_count
                FROM stat_combo_buckets WHERE combo_id = ?
                ORDER BY left_value, right_value
                """,
                (combo["combo_id"],),
            )
        ]
        payload["combo"].append([list(combo), buckets])
    payload["join_domains"] = [
        list(row)
        for row in conn.execute(
            """
            SELECT left_table, left_column, right_table, right_column,
                   left_ndv, right_ndv, overlap_ndv, containment,
                   right_multiplicity
            FROM stat_join_domains WHERE version = ?
            ORDER BY left_table, left_column, right_table, right_column
            """,
            (version,),
        )
    ]
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def statistics_summary(conn: sqlite3.Connection, version: str) -> dict[str, Any]:
    version_row = conn.execute(
        "SELECT * FROM stat_versions WHERE version = ?", (version,)
    ).fetchone()
    if version_row is None:
        raise LookupError(f"statistics version not found: {version}")
    return {
        "version": version,
        "created_at": version_row["created_at"],
        "seed": int(version_row["seed"]),
        "note": version_row["note"],
        "sample_size": int(version_row["sample_size"]),
        "combo_source": version_row["combo_source"],
        "fingerprint": stat_fingerprint(conn, version),
        "row_counts": {
            row["table_name"]: int(row["row_count"])
            for row in conn.execute(
                "SELECT table_name, row_count FROM stat_row_counts WHERE version = ?",
                (version,),
            )
        },
        "histograms": [
            {
                "table": row["table_name"],
                "column": row["column_name"],
                "buckets": int(row["bucket_count"]),
                "rows": int(row["total_rows"]),
            }
            for row in conn.execute(
                """
                SELECT table_name, column_name, bucket_count, total_rows
                FROM stat_histograms WHERE version = ?
                ORDER BY table_name, column_name
                """,
                (version,),
            )
        ],
    }
