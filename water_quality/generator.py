"""Deterministic water-network data generator and truth builder."""

from __future__ import annotations

import json
import random
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .audit import (
    AUDIT_METRICS,
    THRESHOLDS,
    audit_signature,
    compute_manual_reviews,
    compute_violations,
)
from .db import (
    canonical_json,
    connect,
    data_fingerprint,
    initialize_database,
    metadata_get,
    metadata_set,
    sha256_text,
)
from .predicates import QuerySpec
from .reference import execute_central_reference
from .rules import (
    DEFAULT_RULES,
    PartitionRules,
    activate_partition_rules,
    get_active_rules,
)


REGIONS = ("north", "south", "east", "west")
MATERIALS = ("ductile_iron", "steel", "PE", "PVC")
METRIC_COMBOS = (
    ("residual_chlorine", "turbidity", "ph"),
    ("residual_chlorine", "turbidity", "ph"),
    ("residual_chlorine", "turbidity"),
    ("turbidity", "ph"),
    ("residual_chlorine", "ph"),
)


@dataclass(frozen=True)
class GeneratorConfig:
    seed: int = 20261128
    pipe_count: int = 20
    point_count: int = 32
    time_steps: int = 18
    sample_interval_minutes: int = 120
    null_rate: float = 0.12
    null_burst_rate: float = 0.025
    null_burst_max: int = 4
    start_time: str = "2026-11-01T00:00:00Z"

    def __post_init__(self) -> None:
        if self.pipe_count < 4:
            raise ValueError("pipe_count must be at least 4")
        if self.point_count < 8:
            raise ValueError("point_count must be at least 8")
        if self.time_steps < 4:
            raise ValueError("time_steps must be at least 4")
        if self.sample_interval_minutes < 1:
            raise ValueError("sample_interval_minutes must be positive")
        if not 0.0 <= self.null_rate <= 1.0:
            raise ValueError("null_rate must be between 0 and 1")
        if not 0.0 <= self.null_burst_rate <= 1.0:
            raise ValueError("null_burst_rate must be between 0 and 1")
        if self.null_burst_max < 2:
            raise ValueError("null_burst_max must be at least 2")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GeneratorConfig":
        return cls(**data)


@dataclass
class GenerationResult:
    db_path: str
    seed: int
    pipe_rows: int
    point_rows: int
    reading_rows: int
    query_rows: int
    violation_rows: int
    review_rows: int
    data_fingerprint: str
    truth_signature: str


def _sampled_times(config: GeneratorConfig) -> list[str]:
    start = datetime.fromisoformat(config.start_time.replace("Z", "+00:00"))
    interval = timedelta(minutes=config.sample_interval_minutes)
    return [
        (start + interval * index).isoformat().replace("+00:00", "Z")
        for index in range(config.time_steps)
    ]


def _insert_pipes(conn: sqlite3.Connection, config: GeneratorConfig) -> None:
    rng = random.Random(config.seed ^ 0xC0FFEE)
    for index in range(config.pipe_count):
        number = index + 1
        region = REGIONS[index % len(REGIONS)]
        upstream = f"N{number:03d}"
        downstream = f"N{number + 1:03d}"
        diameter = rng.choice((100, 150, 200, 250, 300, 400, 500))
        material = rng.choice(MATERIALS)
        conn.execute(
            """
            INSERT INTO pipes(
                pipe_id, upstream_node, downstream_node, region,
                diameter_mm, material, partition_id
            ) VALUES (?, ?, ?, ?, ?, ?, '')
            """,
            (
                f"P{number:03d}",
                upstream,
                downstream,
                region,
                diameter,
                material,
            ),
        )
    # Deliberate null-key topology row for outer-join semantics. It has no
    # monitoring point and is retained by RIGHT/FULL joins as pipe_key_null.
    conn.execute(
        """
        INSERT INTO pipes(
            pipe_id, upstream_node, downstream_node, region,
            diameter_mm, material, partition_id
        ) VALUES (NULL, 'NNULL-IN', 'NNULL-OUT', 'unassigned', 80, 'unknown', '')
        """
    )


def _insert_points(conn: sqlite3.Connection, config: GeneratorConfig) -> list[str]:
    rng = random.Random(config.seed ^ 0xBEEF)
    real_pipe_ids = [
        row["pipe_id"]
        for row in conn.execute(
            "SELECT pipe_id FROM pipes WHERE pipe_id IS NOT NULL ORDER BY pipe_id"
        )
    ]
    locations: list[str] = []
    for index in range(config.point_count):
        number = index + 1
        point_id = f"M{number:03d}"
        pipe_id = real_pipe_ids[index % len(real_pipe_ids)]
        if index % 11 == 10:
            pipe_id = None
        if index % 17 == 16:
            location = None
        else:
            zone = REGIONS[index % len(REGIONS)]
            location = f"{zone}-{index // len(REGIONS) + 1:02d}"
            locations.append(location)
        metric_types = rng.choice(METRIC_COMBOS)
        conn.execute(
            """
            INSERT INTO monitoring_points(
                point_id, pipe_id, location, metric_types, partition_id
            ) VALUES (?, ?, ?, ?, '')
            """,
            (point_id, pipe_id, location, ",".join(metric_types)),
        )
    return locations


def _generate_value(
    rng: random.Random, metric: str
) -> float:
    if metric == "residual_chlorine":
        return round(rng.uniform(0.03, 1.2), 3)
    if metric == "turbidity":
        return round(rng.uniform(0.05, 2.2), 3)
    if metric == "ph":
        return round(rng.uniform(5.9, 9.2), 2)
    if metric == "water_temp":
        return round(rng.uniform(8.0, 35.0), 1)
    raise ValueError(metric)


def _boundary_value(metric: str, point_index: int, time_index: int) -> float | None:
    marker = point_index * 13 + time_index
    cases = {
        "residual_chlorine": (
            (29, 0.2),
            (31, 0.199),
            (61, 0.201),
        ),
        "turbidity": (
            (37, 1.0),
            (41, 1.001),
            (67, 0.999),
        ),
        "ph": (
            (43, 6.5),
            (47, 6.499),
            (53, 8.5),
            (59, 8.501),
        ),
    }
    for divisor, value in cases.get(metric, ()):
        if marker % divisor == 0:
            return value
    return None


def _insert_readings(conn: sqlite3.Connection, config: GeneratorConfig) -> None:
    rng = random.Random(config.seed ^ 0xA11CE)
    points = conn.execute(
        """
        SELECT point_id, metric_types
        FROM monitoring_points
        ORDER BY point_id
        """
    ).fetchall()
    times = _sampled_times(config)
    rows: list[tuple[Any, ...]] = []

    for point_index, point in enumerate(points):
        supported = set(point["metric_types"].split(","))
        burst_remaining = {metric: 0 for metric in AUDIT_METRICS + ("water_temp",)}
        for time_index, sampled_at in enumerate(times):
            values: dict[str, float | None] = {}
            for metric in AUDIT_METRICS:
                if metric not in supported:
                    values[metric] = None
                    continue
                if burst_remaining[metric] > 0:
                    values[metric] = None
                    burst_remaining[metric] -= 1
                    continue
                remaining = len(times) - time_index
                if (
                    remaining >= 2
                    and rng.random() < config.null_burst_rate
                ):
                    burst_length = rng.randint(2, min(config.null_burst_max, remaining))
                    values[metric] = None
                    burst_remaining[metric] = burst_length - 1
                    continue
                if rng.random() < config.null_rate:
                    values[metric] = None
                    continue
                values[metric] = _boundary_value(metric, point_index, time_index)
                if values[metric] is None:
                    values[metric] = _generate_value(rng, metric)

            if burst_remaining["water_temp"] > 0:
                water_temp = None
                burst_remaining["water_temp"] -= 1
            elif rng.random() < config.null_rate / 2:
                water_temp = None
            else:
                water_temp = _generate_value(rng, "water_temp")

            rows.append(
                (
                    point["point_id"],
                    sampled_at,
                    values["residual_chlorine"],
                    values["turbidity"],
                    values["ph"],
                    water_temp,
                )
            )

    conn.executemany(
        """
        INSERT INTO readings(
            point_id, sampled_at, residual_chlorine, turbidity, ph, water_temp
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _build_queries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    north_locations = [
        row["location"]
        for row in conn.execute(
            """
            SELECT DISTINCT location
            FROM monitoring_points
            WHERE location LIKE 'north-%'
            ORDER BY location
            """
        )
    ]
    east_region = [{"field": "region", "operator": "IN", "value": ["east"]}]
    queries: list[dict[str, Any]] = [
        {
            "query_id": "Q001",
            "description": "北区余氯低于预警下界的异常监测记录",
            "join_type": "inner",
            "pipe_predicates": [
                {"field": "region", "operator": "IN", "value": ["north"]}
            ],
            "reading_predicate_groups": [
                [
                    {
                        "field": "residual_chlorine",
                        "operator": "<",
                        "value": 0.35,
                    }
                ]
            ],
        },
        {
            "query_id": "Q002",
            "description": "南区浊度超过上限的事件（保留无匹配管段的监测记录）",
            "join_type": "left",
            "pipe_predicates": [
                {"field": "region", "operator": "IN", "value": ["south"]}
            ],
            "reading_predicate_groups": [
                [{"field": "turbidity", "operator": ">", "value": 1.0}]
            ],
        },
        {
            "query_id": "Q003",
            "description": "pH 超出 6.5-8.5 区间（全连接）",
            "join_type": "full",
            "pipe_predicates": [],
            "reading_predicate_groups": [
                [{"field": "ph", "operator": "<", "value": 6.5}],
                [{"field": "ph", "operator": ">", "value": 8.5}],
            ],
        },
        {
            "query_id": "Q004",
            "description": "东区余氯边界查询（右连接保留未匹配管段）",
            "join_type": "right",
            "pipe_predicates": east_region,
            "reading_predicate_groups": [
                [{"field": "residual_chlorine", "operator": "<=", "value": 0.2}]
            ],
        },
        {
            "query_id": "Q005",
            "description": "北区位置监测点浊度边界样本",
            "join_type": "inner",
            "pipe_predicates": [],
            "point_predicates": [
                {
                    "field": "location",
                    "operator": "IN",
                    "value": north_locations,
                }
            ],
            "reading_predicate_groups": [
                [{"field": "turbidity", "operator": ">=", "value": 1.0}]
            ],
        },
        {
            "query_id": "Q006",
            "description": "水温异常升高且含空值比较的监测记录",
            "join_type": "left",
            "pipe_predicates": [],
            "reading_predicate_groups": [
                [{"field": "water_temp", "operator": ">=", "value": 30.0}]
            ],
        },
        {
            "query_id": "Q007",
            "description": "全量记录全连接，覆盖空连接键与记录顺序",
            "join_type": "full",
            "pipe_predicates": [],
            "reading_predicate_groups": [],
        },
    ]
    return queries


def _persist_queries(conn: sqlite3.Connection, queries: list[dict[str, Any]]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for query in queries:
        QuerySpec.from_dict(query)
        conn.execute(
            """
            INSERT INTO query_requests(
                query_id, description, join_type, request_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                query["query_id"],
                query.get("description", ""),
                query.get("join_type", "inner"),
                canonical_json(query),
                now,
            ),
        )


def _persist_truth(conn: sqlite3.Connection) -> tuple[int, int, str, str]:
    violations = compute_violations(conn)
    reviews = compute_manual_reviews(conn)
    conn.executemany(
        """
        INSERT INTO truth_violations(
            point_id, sampled_at, metric, value, rule_name,
            threshold_low, threshold_high, point_partition_id, pipe_partition_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item["point_id"],
                item["sampled_at"],
                item["metric"],
                item["value"],
                item["rule_name"],
                item["threshold_low"],
                item["threshold_high"],
                item["point_partition_id"],
                item["pipe_partition_id"],
            )
            for item in violations
        ],
    )
    conn.executemany(
        """
        INSERT INTO truth_manual_reviews(
            point_id, metric, interval_start, interval_end, missing_count,
            point_partition_id, pipe_partition_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                item["point_id"],
                item["metric"],
                item["interval_start"],
                item["interval_end"],
                item["missing_count"],
                item["point_partition_id"],
                item["pipe_partition_id"],
            )
            for item in reviews
        ],
    )

    truth_query_payload: list[dict[str, Any]] = []
    for request in conn.execute(
        "SELECT request_json FROM query_requests ORDER BY query_id"
    ).fetchall():
        query = QuerySpec.from_json(request["request_json"])
        rows = execute_central_reference(conn, query)
        truth_query_payload.append(
            {
                "query_id": query.query_id,
                "rows": rows,
            }
        )
        conn.executemany(
            """
            INSERT INTO truth_query_results(
                query_id, row_index, row_json, row_signature
            ) VALUES (?, ?, ?, ?)
            """,
            [
                (
                    query.query_id,
                    index,
                    canonical_json(row),
                    sha256_text(canonical_json(row)),
                )
                for index, row in enumerate(rows)
            ],
        )
    truth_signature = audit_signature(violations, reviews)
    truth_query_digest = sha256_text(canonical_json(truth_query_payload))
    metadata_set(conn, "truth_violation_count", str(len(violations)))
    metadata_set(conn, "truth_review_count", str(len(reviews)))
    metadata_set(conn, "truth_signature", truth_signature)
    metadata_set(conn, "truth_query_digest", truth_query_digest)
    return len(violations), len(reviews), truth_signature, truth_query_digest


def generate_database(
    db_path: str | Path,
    config: GeneratorConfig | None = None,
    *,
    rules: PartitionRules = DEFAULT_RULES,
    reset: bool = True,
) -> GenerationResult:
    config = config or GeneratorConfig()
    conn = initialize_database(db_path, reset=reset)
    try:
        metadata_set(conn, "generator_seed", str(config.seed))
        metadata_set(conn, "generator_config", canonical_json(config.to_dict()))
        metadata_set(
            conn,
            "safety_thresholds",
            canonical_json(THRESHOLDS),
        )
        metadata_set(
            conn,
            "boundary_injection_spec",
            canonical_json(
                {
                    "residual_chlorine": [0.199, 0.2, 0.201],
                    "turbidity": [0.999, 1.0, 1.001],
                    "ph": [6.499, 6.5, 8.5, 8.501],
                    "null_key_pipe_rows": 1,
                    "null_key_point_frequency": 11,
                }
            ),
        )
        _insert_pipes(conn, config)
        _insert_points(conn, config)
        activate_partition_rules(conn, rules)
        metadata_set(
            conn,
            "generation_partition_rules_version",
            rules.version,
        )
        metadata_set(
            conn,
            "generation_partition_rules_json",
            canonical_json(rules.to_dict()),
        )
        _insert_readings(conn, config)
        queries = _build_queries(conn)
        _persist_queries(conn, queries)
        conn.commit()
        violation_count, review_count, truth_signature, _ = _persist_truth(conn)
        fingerprint = data_fingerprint(conn)
        metadata_set(conn, "data_fingerprint", fingerprint)
        metadata_set(conn, "generated_at", datetime.now(timezone.utc).isoformat())
        conn.commit()

        counts = {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in (
                "pipes",
                "monitoring_points",
                "readings",
                "query_requests",
            )
        }
        return GenerationResult(
            db_path=str(db_path),
            seed=config.seed,
            pipe_rows=counts["pipes"],
            point_rows=counts["monitoring_points"],
            reading_rows=counts["readings"],
            query_rows=counts["query_requests"],
            violation_rows=violation_count,
            review_rows=review_count,
            data_fingerprint=fingerprint,
            truth_signature=truth_signature,
        )
    finally:
        conn.close()


def verify_reproducible(db_path: str | Path) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        config_text = metadata_get(conn, "generator_config")
        if config_text is None:
            raise RuntimeError("generator_config metadata is missing")
        config = GeneratorConfig.from_dict(json.loads(config_text))
        generation_rules_text = metadata_get(
            conn, "generation_partition_rules_json"
        )
        if generation_rules_text is not None:
            rules = PartitionRules.from_json(generation_rules_text)
        else:
            rules = get_active_rules(conn)
        original_fingerprint = metadata_get(conn, "data_fingerprint")
        original_truth = metadata_get(conn, "truth_signature")
        original_truth_queries = metadata_get(conn, "truth_query_digest")
        with tempfile.TemporaryDirectory(prefix="wq-repro-") as tmp:
            replay_path = Path(tmp) / "replay.db"
            generate_database(replay_path, config, rules=rules)
            replay = connect(replay_path)
            try:
                replay_fingerprint = metadata_get(replay, "data_fingerprint")
                replay_truth = metadata_get(replay, "truth_signature")
                replay_truth_queries = metadata_get(replay, "truth_query_digest")
            finally:
                replay.close()
    finally:
        conn.close()

    checks = {
        "data_fingerprint": original_fingerprint == replay_fingerprint,
        "truth_signature": original_truth == replay_truth,
        "truth_query_digest": original_truth_queries == replay_truth_queries,
    }
    return {
        "reproducible": all(checks.values()),
        "checks": checks,
        "config": config.to_dict(),
        "original": {
            "data_fingerprint": original_fingerprint,
            "truth_signature": original_truth,
            "truth_query_digest": original_truth_queries,
        },
        "replay": {
            "data_fingerprint": replay_fingerprint,
            "truth_signature": replay_truth,
            "truth_query_digest": replay_truth_queries,
        },
    }

