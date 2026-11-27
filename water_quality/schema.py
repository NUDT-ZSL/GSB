"""SQLite schema for the water-quality predicate-pushdown kernel."""

from __future__ import annotations

SCHEMA_VERSION = "1"


TABLES = (
    "metadata",
    "partition_rules",
    "pipes",
    "monitoring_points",
    "readings",
    "query_requests",
    "partition_assignments",
    "truth_violations",
    "truth_manual_reviews",
    "truth_query_results",
    "runs",
    "run_partition_assignments",
    "query_plans",
    "query_plan_partitions",
    "run_pipe_hits",
    "run_point_hits",
    "run_null_verdicts",
    "run_join_results",
    "run_query_results",
    "consistency_reports",
    "run_violations",
    "run_manual_reviews",
    "run_incremental_checks",
    "snapshots",
    "snapshot_query_plans",
    "snapshot_plan_partitions",
    "snapshot_partition_assignments",
    "snapshot_pipe_hits",
    "snapshot_point_hits",
    "snapshot_null_verdicts",
    "snapshot_join_results",
    "snapshot_query_results",
    "snapshot_consistency",
    "snapshot_violations",
    "snapshot_manual_reviews",
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS partition_rules (
    version TEXT PRIMARY KEY,
    rules_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipes (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pipe_id TEXT UNIQUE,
    upstream_node TEXT,
    downstream_node TEXT,
    region TEXT,
    diameter_mm INTEGER,
    material TEXT,
    partition_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pipes_partition ON pipes(partition_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pipes_pipe_id_unique ON pipes(pipe_id)
    WHERE pipe_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS monitoring_points (
    point_id TEXT PRIMARY KEY,
    pipe_id TEXT,
    location TEXT,
    metric_types TEXT NOT NULL,
    partition_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_points_partition ON monitoring_points(partition_id);
CREATE INDEX IF NOT EXISTS idx_points_pipe ON monitoring_points(pipe_id);

CREATE TABLE IF NOT EXISTS readings (
    reading_id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id TEXT NOT NULL,
    sampled_at TEXT NOT NULL,
    residual_chlorine REAL,
    turbidity REAL,
    ph REAL,
    water_temp REAL,
    UNIQUE(point_id, sampled_at)
);
CREATE INDEX IF NOT EXISTS idx_readings_point_time ON readings(point_id, sampled_at);

CREATE TABLE IF NOT EXISTS query_requests (
    query_id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    join_type TEXT NOT NULL,
    request_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS partition_assignments (
    entity_type TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    entity_id TEXT,
    partition_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_key)
);
CREATE INDEX IF NOT EXISTS idx_partition_assignments_partition
    ON partition_assignments(entity_type, partition_id);

CREATE TABLE IF NOT EXISTS truth_violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id TEXT NOT NULL,
    sampled_at TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    rule_name TEXT NOT NULL,
    threshold_low REAL,
    threshold_high REAL,
    point_partition_id TEXT NOT NULL,
    pipe_partition_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_truth_violations_order
    ON truth_violations(point_id, sampled_at, metric);

CREATE TABLE IF NOT EXISTS truth_manual_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    interval_start TEXT NOT NULL,
    interval_end TEXT NOT NULL,
    missing_count INTEGER NOT NULL,
    point_partition_id TEXT NOT NULL,
    pipe_partition_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_truth_reviews_order
    ON truth_manual_reviews(point_id, metric, interval_start);

CREATE TABLE IF NOT EXISTS truth_query_results (
    query_id TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    row_json TEXT NOT NULL,
    row_signature TEXT NOT NULL,
    PRIMARY KEY (query_id, row_index)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    seed INTEGER,
    partition_rules_version TEXT NOT NULL,
    partition_rules_json TEXT NOT NULL,
    source_run_id TEXT,
    snapshot_id TEXT,
    data_fingerprint TEXT,
    source_data_fingerprint TEXT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_partition_assignments (
    run_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    entity_id TEXT,
    partition_id TEXT NOT NULL,
    PRIMARY KEY (run_id, entity_type, entity_key)
);

CREATE TABLE IF NOT EXISTS query_plans (
    run_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    query_json TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    join_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, query_id)
);

CREATE TABLE IF NOT EXISTS query_plan_partitions (
    run_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    part_type TEXT NOT NULL,
    partition_id TEXT NOT NULL,
    predicate_sql TEXT NOT NULL,
    execution_sql TEXT NOT NULL,
    result_count INTEGER NOT NULL,
    PRIMARY KEY (run_id, query_id, part_type, partition_id)
);

CREATE TABLE IF NOT EXISTS run_pipe_hits (
    run_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    partition_id TEXT NOT NULL,
    pipe_row_id INTEGER NOT NULL,
    pipe_id TEXT,
    row_json TEXT NOT NULL,
    PRIMARY KEY (run_id, query_id, partition_id, pipe_row_id)
);

CREATE TABLE IF NOT EXISTS run_point_hits (
    run_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    partition_id TEXT NOT NULL,
    reading_id INTEGER NOT NULL,
    point_id TEXT NOT NULL,
    sampled_at TEXT NOT NULL,
    row_json TEXT NOT NULL,
    PRIMARY KEY (run_id, query_id, partition_id, reading_id)
);

CREATE TABLE IF NOT EXISTS run_null_verdicts (
    run_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    partition_id TEXT NOT NULL,
    row_key TEXT NOT NULL,
    point_id TEXT,
    sampled_at TEXT,
    predicate_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    operator TEXT NOT NULL,
    threshold_text TEXT,
    predicate_sql TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict = 'UNKNOWN')
);

CREATE TABLE IF NOT EXISTS run_join_results (
    run_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    join_task_key TEXT NOT NULL,
    pipe_partition_id TEXT NOT NULL,
    point_partition_id TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    row_json TEXT NOT NULL,
    row_signature TEXT NOT NULL,
    PRIMARY KEY (run_id, query_id, join_task_key, row_index)
);
CREATE INDEX IF NOT EXISTS idx_run_join_results_order
    ON run_join_results(run_id, query_id, row_index);

CREATE TABLE IF NOT EXISTS run_query_results (
    run_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    row_json TEXT NOT NULL,
    row_signature TEXT NOT NULL,
    PRIMARY KEY (run_id, query_id, row_index)
);

CREATE TABLE IF NOT EXISTS consistency_reports (
    run_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    is_consistent INTEGER NOT NULL,
    downpush_count INTEGER NOT NULL,
    central_count INTEGER NOT NULL,
    missing_in_pushdown_json TEXT NOT NULL,
    missing_in_central_json TEXT NOT NULL,
    marker_mismatches_json TEXT NOT NULL,
    order_mismatches_json TEXT NOT NULL,
    divergence_partition TEXT,
    divergence_predicate TEXT,
    details_json TEXT NOT NULL,
    PRIMARY KEY (run_id, query_id)
);

CREATE TABLE IF NOT EXISTS run_violations (
    run_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    point_id TEXT NOT NULL,
    sampled_at TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    rule_name TEXT NOT NULL,
    threshold_low REAL,
    threshold_high REAL,
    point_partition_id TEXT NOT NULL,
    pipe_partition_id TEXT,
    PRIMARY KEY (run_id, id)
);

CREATE TABLE IF NOT EXISTS run_manual_reviews (
    run_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    point_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    interval_start TEXT NOT NULL,
    interval_end TEXT NOT NULL,
    missing_count INTEGER NOT NULL,
    point_partition_id TEXT NOT NULL,
    pipe_partition_id TEXT,
    PRIMARY KEY (run_id, id)
);

CREATE TABLE IF NOT EXISTS run_incremental_checks (
    run_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    is_consistent INTEGER NOT NULL,
    reused_join_task_count INTEGER NOT NULL,
    recomputed_join_task_count INTEGER NOT NULL,
    recomputed_partition_ids_json TEXT NOT NULL,
    details_json TEXT NOT NULL,
    PRIMARY KEY (run_id, query_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    source_run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    seed INTEGER,
    partition_rules_version TEXT NOT NULL,
    partition_rules_json TEXT NOT NULL,
    data_fingerprint TEXT,
    source_data_fingerprint TEXT,
    payload_digest TEXT NOT NULL,
    query_count INTEGER NOT NULL,
    all_consistent INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_query_plans (
    snapshot_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    query_json TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    join_type TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, query_id)
);

CREATE TABLE IF NOT EXISTS snapshot_plan_partitions (
    snapshot_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    part_type TEXT NOT NULL,
    partition_id TEXT NOT NULL,
    predicate_sql TEXT NOT NULL,
    execution_sql TEXT NOT NULL,
    result_count INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, query_id, part_type, partition_id)
);

CREATE TABLE IF NOT EXISTS snapshot_partition_assignments (
    snapshot_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    entity_id TEXT,
    partition_id TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, entity_type, entity_key)
);

CREATE TABLE IF NOT EXISTS snapshot_pipe_hits (
    snapshot_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    partition_id TEXT NOT NULL,
    pipe_row_id INTEGER NOT NULL,
    pipe_id TEXT,
    row_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, query_id, partition_id, pipe_row_id)
);

CREATE TABLE IF NOT EXISTS snapshot_point_hits (
    snapshot_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    partition_id TEXT NOT NULL,
    reading_id INTEGER NOT NULL,
    point_id TEXT NOT NULL,
    sampled_at TEXT NOT NULL,
    row_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, query_id, partition_id, reading_id)
);

CREATE TABLE IF NOT EXISTS snapshot_null_verdicts (
    snapshot_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    partition_id TEXT NOT NULL,
    row_key TEXT NOT NULL,
    point_id TEXT,
    sampled_at TEXT,
    predicate_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    operator TEXT NOT NULL,
    threshold_text TEXT,
    predicate_sql TEXT NOT NULL,
    verdict TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_join_results (
    snapshot_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    join_task_key TEXT NOT NULL,
    pipe_partition_id TEXT NOT NULL,
    point_partition_id TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    row_json TEXT NOT NULL,
    row_signature TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, query_id, join_task_key, row_index)
);

CREATE TABLE IF NOT EXISTS snapshot_query_results (
    snapshot_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    row_json TEXT NOT NULL,
    row_signature TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, query_id, row_index)
);

CREATE TABLE IF NOT EXISTS snapshot_consistency (
    snapshot_id TEXT NOT NULL,
    query_id TEXT NOT NULL,
    is_consistent INTEGER NOT NULL,
    downpush_count INTEGER NOT NULL,
    central_count INTEGER NOT NULL,
    missing_in_pushdown_json TEXT NOT NULL,
    missing_in_central_json TEXT NOT NULL,
    marker_mismatches_json TEXT NOT NULL,
    order_mismatches_json TEXT NOT NULL,
    divergence_partition TEXT,
    divergence_predicate TEXT,
    details_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, query_id)
);

CREATE TABLE IF NOT EXISTS snapshot_violations (
    snapshot_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    point_id TEXT NOT NULL,
    sampled_at TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    rule_name TEXT NOT NULL,
    threshold_low REAL,
    threshold_high REAL,
    point_partition_id TEXT NOT NULL,
    pipe_partition_id TEXT,
    PRIMARY KEY (snapshot_id, id)
);

CREATE TABLE IF NOT EXISTS snapshot_manual_reviews (
    snapshot_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    point_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    interval_start TEXT NOT NULL,
    interval_end TEXT NOT NULL,
    missing_count INTEGER NOT NULL,
    point_partition_id TEXT NOT NULL,
    pipe_partition_id TEXT,
    PRIMARY KEY (snapshot_id, id)
);
"""


def drop_all(conn) -> None:
    """Drop all application tables in FK-safe order."""

    for table in reversed(TABLES):
        conn.execute(f'DROP TABLE IF EXISTS "{table}"')


def create_all(conn) -> None:
    conn.executescript(SCHEMA_SQL)

