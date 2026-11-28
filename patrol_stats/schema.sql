PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS regions (
    region_id INTEGER PRIMARY KEY,
    region_code TEXT NOT NULL UNIQUE,
    region_name TEXT NOT NULL,
    terrain TEXT NOT NULL,
    remoteness INTEGER NOT NULL,
    risk_level TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bands (
    band_id INTEGER PRIMARY KEY,
    band_code TEXT NOT NULL UNIQUE,
    band_name TEXT NOT NULL,
    freq_low_mhz INTEGER NOT NULL,
    freq_high_mhz INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rangers (
    ranger_id INTEGER PRIMARY KEY,
    ranger_code TEXT NOT NULL UNIQUE,
    ranger_name TEXT NOT NULL,
    home_region_id INTEGER NOT NULL REFERENCES regions(region_id),
    active INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS terminals (
    terminal_id INTEGER PRIMARY KEY,
    serial_no TEXT NOT NULL UNIQUE,
    ranger_id INTEGER NOT NULL REFERENCES rangers(ranger_id),
    home_region_id INTEGER NOT NULL REFERENCES regions(region_id),
    band_id INTEGER NOT NULL REFERENCES bands(band_id),
    model TEXT NOT NULL,
    active INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_id INTEGER PRIMARY KEY,
    route_code TEXT NOT NULL UNIQUE,
    route_name TEXT NOT NULL,
    region_id INTEGER NOT NULL REFERENCES regions(region_id),
    patrol_km REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS base_stations (
    station_id INTEGER PRIMARY KEY,
    station_code TEXT NOT NULL UNIQUE,
    station_name TEXT NOT NULL,
    region_id INTEGER NOT NULL REFERENCES regions(region_id),
    band_id INTEGER NOT NULL REFERENCES bands(band_id),
    coverage_km REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS patrol_logs (
    log_id INTEGER PRIMARY KEY,
    ranger_id INTEGER NOT NULL REFERENCES rangers(ranger_id),
    terminal_id INTEGER NOT NULL REFERENCES terminals(terminal_id),
    route_id INTEGER NOT NULL REFERENCES routes(route_id),
    region_id INTEGER NOT NULL REFERENCES regions(region_id),
    band_id INTEGER NOT NULL REFERENCES bands(band_id),
    station_id INTEGER NOT NULL REFERENCES base_stations(station_id),
    logged_at TEXT NOT NULL,
    fire_risk REAL NOT NULL,
    signal_dbm REAL NOT NULL,
    duration_s INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS queries (
    query_id TEXT PRIMARY KEY,
    region_id INTEGER NOT NULL,
    band_id INTEGER NOT NULL,
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    deviation_threshold REAL NOT NULL,
    workload_class TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generation_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    seed INTEGER NOT NULL,
    log_count INTEGER NOT NULL,
    query_count INTEGER NOT NULL,
    data_sha256 TEXT NOT NULL,
    manifest_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stat_versions (
    version TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    seed INTEGER NOT NULL,
    note TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    combo_source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stat_row_counts (
    version TEXT NOT NULL REFERENCES stat_versions(version) ON DELETE CASCADE,
    table_name TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    PRIMARY KEY (version, table_name)
);

CREATE TABLE IF NOT EXISTS stat_column_stats (
    version TEXT NOT NULL REFERENCES stat_versions(version) ON DELETE CASCADE,
    table_name TEXT NOT NULL,
    column_name TEXT NOT NULL,
    ndv INTEGER NOT NULL,
    null_count INTEGER NOT NULL,
    min_value TEXT,
    max_value TEXT,
    sample_size INTEGER NOT NULL,
    population_rows INTEGER NOT NULL,
    PRIMARY KEY (version, table_name, column_name)
);

CREATE TABLE IF NOT EXISTS stat_samples (
    version TEXT NOT NULL REFERENCES stat_versions(version) ON DELETE CASCADE,
    table_name TEXT NOT NULL,
    column_name TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    population_rows INTEGER NOT NULL,
    values_json TEXT NOT NULL,
    PRIMARY KEY (version, table_name, column_name)
);

CREATE TABLE IF NOT EXISTS stat_histograms (
    hist_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL REFERENCES stat_versions(version) ON DELETE CASCADE,
    table_name TEXT NOT NULL,
    column_name TEXT NOT NULL,
    requested_bucket_count INTEGER NOT NULL,
    bucket_count INTEGER NOT NULL,
    total_rows INTEGER NOT NULL,
    null_count INTEGER NOT NULL,
    sample_size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (version, table_name, column_name)
);

CREATE TABLE IF NOT EXISTS stat_histogram_buckets (
    hist_id INTEGER NOT NULL REFERENCES stat_histograms(hist_id) ON DELETE CASCADE,
    bucket_no INTEGER NOT NULL,
    lower_value REAL NOT NULL,
    upper_value REAL NOT NULL,
    lower_inclusive INTEGER NOT NULL,
    upper_inclusive INTEGER NOT NULL,
    observed_count INTEGER NOT NULL,
    ndv INTEGER NOT NULL,
    PRIMARY KEY (hist_id, bucket_no)
);

CREATE TABLE IF NOT EXISTS stat_combo_histograms (
    combo_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL REFERENCES stat_versions(version) ON DELETE CASCADE,
    left_table TEXT NOT NULL,
    left_column TEXT NOT NULL,
    right_table TEXT NOT NULL,
    right_column TEXT NOT NULL,
    total_rows INTEGER NOT NULL,
    sample_size INTEGER NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (version, left_table, left_column, right_table, right_column)
);

CREATE TABLE IF NOT EXISTS stat_combo_buckets (
    combo_id INTEGER NOT NULL REFERENCES stat_combo_histograms(combo_id) ON DELETE CASCADE,
    left_value INTEGER NOT NULL,
    right_value INTEGER NOT NULL,
    observed_count INTEGER NOT NULL,
    PRIMARY KEY (combo_id, left_value, right_value)
);

CREATE TABLE IF NOT EXISTS stat_join_domains (
    version TEXT NOT NULL REFERENCES stat_versions(version) ON DELETE CASCADE,
    left_table TEXT NOT NULL,
    left_column TEXT NOT NULL,
    right_table TEXT NOT NULL,
    right_column TEXT NOT NULL,
    left_ndv INTEGER NOT NULL,
    right_ndv INTEGER NOT NULL,
    overlap_ndv INTEGER NOT NULL,
    left_rows INTEGER NOT NULL,
    right_rows INTEGER NOT NULL,
    containment REAL NOT NULL,
    right_multiplicity REAL NOT NULL,
    PRIMARY KEY (
        version, left_table, left_column, right_table, right_column
    )
);

CREATE TABLE IF NOT EXISTS evaluation_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL REFERENCES stat_versions(version),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    query_count INTEGER NOT NULL DEFAULT 0,
    mean_abs_rel_error REAL,
    max_abs_rel_error REAL,
    stat_fingerprint TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS query_evaluations (
    eval_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES evaluation_runs(run_id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    query_id TEXT NOT NULL,
    region_id INTEGER NOT NULL,
    band_id INTEGER NOT NULL,
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    deviation_threshold REAL NOT NULL,
    fact_rows INTEGER NOT NULL,
    independence_base REAL NOT NULL,
    combo_base REAL NOT NULL,
    selected_base REAL NOT NULL,
    actual_rows INTEGER NOT NULL,
    mean_node_error REAL NOT NULL,
    max_node_error REAL NOT NULL,
    first_breach_node TEXT,
    estimate_scope TEXT NOT NULL,
    predicate_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, query_id)
);

CREATE TABLE IF NOT EXISTS node_estimates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_id INTEGER NOT NULL REFERENCES query_evaluations(eval_id) ON DELETE CASCADE,
    node_order INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    node_name TEXT NOT NULL,
    join_table TEXT,
    join_key TEXT,
    estimate_independence REAL NOT NULL,
    estimate_combo REAL NOT NULL,
    selected_estimate REAL NOT NULL,
    actual_rows INTEGER NOT NULL,
    relative_error REAL NOT NULL,
    scope TEXT NOT NULL,
    overlap_ratio REAL NOT NULL,
    right_multiplicity REAL NOT NULL,
    correction_factor REAL NOT NULL,
    first_over_threshold INTEGER NOT NULL,
    reason TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    UNIQUE (eval_id, node_order)
);

CREATE TABLE IF NOT EXISTS join_order_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES evaluation_runs(run_id) ON DELETE CASCADE,
    query_id TEXT NOT NULL,
    recommended_order_json TEXT NOT NULL,
    baseline_order_json TEXT NOT NULL,
    costs_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    UNIQUE (run_id, query_id)
);

CREATE TABLE IF NOT EXISTS index_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES evaluation_runs(run_id) ON DELETE CASCADE,
    query_id TEXT NOT NULL,
    index_name TEXT NOT NULL,
    table_name TEXT NOT NULL,
    columns_json TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    estimated_benefit REAL NOT NULL,
    estimated_cost REAL NOT NULL,
    UNIQUE (run_id, query_id, index_name)
);

CREATE TABLE IF NOT EXISTS advisor_suggestions (
    suggestion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES evaluation_runs(run_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    table_name TEXT NOT NULL,
    column_name TEXT,
    reason TEXT NOT NULL,
    priority TEXT NOT NULL,
    action_json TEXT NOT NULL,
    applied_in_version TEXT
);

CREATE TABLE IF NOT EXISTS update_plans (
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_version TEXT NOT NULL,
    target_version TEXT,
    created_at TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    applied_at TEXT,
    source_run_id INTEGER NOT NULL,
    target_run_id INTEGER,
    mean_before REAL,
    mean_after REAL,
    converged INTEGER
);
