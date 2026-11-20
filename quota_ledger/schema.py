"""SQLite schema for the quota ledger."""

SCHEMA_VERSION = 1

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quota_layers (
        path TEXT PRIMARY KEY,
        level TEXT NOT NULL,
        tenant TEXT NOT NULL,
        bucket TEXT NOT NULL,
        prefix TEXT NOT NULL,
        max_bytes INTEGER NOT NULL CHECK (max_bytes > 0),
        max_objects INTEGER NOT NULL CHECK (max_objects > 0),
        soft_pct INTEGER NOT NULL CHECK (soft_pct >= 0 AND soft_pct <= 100),
        hard_pct INTEGER NOT NULL CHECK (hard_pct >= 0 AND hard_pct <= 100),
        soft_bytes INTEGER NOT NULL,
        soft_objects INTEGER NOT NULL,
        hard_bytes INTEGER NOT NULL,
        hard_objects INTEGER NOT NULL,
        UNIQUE (level, tenant, bucket, prefix),
        CHECK (soft_pct <= hard_pct)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage (
        path TEXT PRIMARY KEY REFERENCES quota_layers(path),
        used_bytes INTEGER NOT NULL DEFAULT 0 CHECK (used_bytes >= 0),
        used_objects INTEGER NOT NULL DEFAULT 0 CHECK (used_objects >= 0),
        soft_active INTEGER NOT NULL DEFAULT 0 CHECK (soft_active IN (0, 1)),
        soft_entry_seq INTEGER,
        soft_entry_clock_ms INTEGER,
        soft_entry_used_bytes INTEGER,
        soft_entry_used_objects INTEGER,
        soft_entry_soft_bytes INTEGER,
        soft_entry_soft_objects INTEGER,
        soft_exit_seq INTEGER,
        soft_exit_clock_ms INTEGER,
        soft_exit_used_bytes INTEGER,
        soft_exit_used_objects INTEGER,
        soft_exit_soft_bytes INTEGER,
        soft_exit_soft_objects INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS objects (
        tenant TEXT NOT NULL,
        bucket TEXT NOT NULL,
        prefix TEXT NOT NULL,
        object_key TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
        created_seq INTEGER NOT NULL,
        updated_seq INTEGER NOT NULL,
        PRIMARY KEY (tenant, bucket, prefix, object_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tokens (
        tenant TEXT PRIMARY KEY,
        tokens_micro INTEGER NOT NULL CHECK (tokens_micro >= 0),
        last_refill_ms INTEGER NOT NULL CHECK (last_refill_ms >= 0),
        last_seen_ms INTEGER NOT NULL CHECK (last_seen_ms >= 0),
        refill_remainder_num INTEGER NOT NULL CHECK (refill_remainder_num >= 0),
        capacity_micro INTEGER NOT NULL CHECK (capacity_micro > 0),
        CHECK (tokens_micro <= capacity_micro)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        fingerprint TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        result_json TEXT NOT NULL,
        decision TEXT NOT NULL,
        code TEXT NOT NULL,
        accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
        clock_ms INTEGER NOT NULL CHECK (clock_ms >= 0)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotency (
        idempotency_key TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        seq INTEGER NOT NULL UNIQUE REFERENCES events(seq),
        result_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS soft_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        transition TEXT NOT NULL CHECK (transition IN ('ENTER', 'EXIT')),
        level TEXT NOT NULL,
        path TEXT NOT NULL,
        event_seq INTEGER NOT NULL,
        clock_ms INTEGER NOT NULL,
        used_bytes INTEGER NOT NULL,
        used_objects INTEGER NOT NULL,
        soft_bytes_limit INTEGER NOT NULL,
        soft_objects_limit INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pending_operations (
        op_key TEXT PRIMARY KEY,
        seq INTEGER NOT NULL,
        idempotency_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        stage TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_quota_layers_order ON quota_layers(level, tenant, bucket, prefix)",
    "CREATE INDEX IF NOT EXISTS idx_soft_events_order ON soft_events(event_seq, path, transition)",
    "CREATE INDEX IF NOT EXISTS idx_events_clock ON events(clock_ms)",
)

