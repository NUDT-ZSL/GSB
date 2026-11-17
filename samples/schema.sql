-- Logical schemas used by samples/config.json.  Physical shards use the same
-- columns and are named db_NNNN.<logical_table>_NNNN by the router.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS orders (
    order_id   INTEGER PRIMARY KEY,
    tenant_id  INTEGER NOT NULL,
    region     TEXT    NOT NULL,
    amount     INTEGER NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_tenant_region
    ON orders (tenant_id, region);

CREATE TABLE IF NOT EXISTS ledger (
    entry_id   INTEGER PRIMARY KEY,
    amount     INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_amount ON ledger (amount);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    tenant_id  INTEGER NOT NULL,
    payload    TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON sessions (tenant_id);
