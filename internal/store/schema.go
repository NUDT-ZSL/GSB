package store

const schemaSQL = `
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS producer_sessions (
  producer_session TEXT PRIMARY KEY,
  last_seq INTEGER NOT NULL CHECK(last_seq >= 1)
);

CREATE TABLE IF NOT EXISTS partitions (
  partition_key TEXT PRIMARY KEY,
  next_offset INTEGER NOT NULL CHECK(next_offset >= 1),
  committed_offset INTEGER NOT NULL DEFAULT 0 CHECK(committed_offset >= 0),
  confirmed_offset INTEGER NOT NULL DEFAULT 0 CHECK(confirmed_offset >= 0),
  window_holder TEXT NOT NULL DEFAULT '',
  window_start INTEGER NOT NULL DEFAULT 0 CHECK(window_start >= 0),
  window_end INTEGER NOT NULL DEFAULT 0 CHECK(window_end >= 0),
  window_deadline_unix_ms INTEGER NOT NULL DEFAULT 0,
  window_ttl_ms INTEGER NOT NULL DEFAULT 0,
  window_recovered INTEGER NOT NULL DEFAULT 0 CHECK(window_recovered IN (0,1)),
  pending_redelivery INTEGER NOT NULL DEFAULT 0 CHECK(pending_redelivery IN (0,1)),
  redelivery_from INTEGER NOT NULL DEFAULT 0,
  blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked IN (0,1)),
  blocked_offset INTEGER NOT NULL DEFAULT 0,
  blocked_retry_count INTEGER NOT NULL DEFAULT 0,
  ledger_version INTEGER NOT NULL,
  state_revision INTEGER NOT NULL DEFAULT 0,
  CHECK(committed_offset <= confirmed_offset)
);

CREATE TABLE IF NOT EXISTS messages (
  partition_key TEXT NOT NULL,
  message_offset INTEGER NOT NULL CHECK(message_offset >= 1),
  producer_session TEXT NOT NULL,
  producer_seq INTEGER NOT NULL CHECK(producer_seq >= 1),
  body TEXT NOT NULL,
  appended_unix_ms INTEGER NOT NULL,
  PRIMARY KEY(partition_key, message_offset),
  UNIQUE(partition_key, producer_session, producer_seq)
);

CREATE INDEX IF NOT EXISTS idx_messages_partition_seq
  ON messages(partition_key, producer_seq);

CREATE TABLE IF NOT EXISTS retry_counts (
  partition_key TEXT NOT NULL,
  message_offset INTEGER NOT NULL,
  attempts INTEGER NOT NULL CHECK(attempts >= 0),
  PRIMARY KEY(partition_key, message_offset)
);

CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  partition_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  session TEXT NOT NULL DEFAULT '',
  message_offset INTEGER NOT NULL DEFAULT 0,
  start_offset INTEGER NOT NULL DEFAULT 0,
  end_offset INTEGER NOT NULL DEFAULT 0,
  committed_before INTEGER NOT NULL DEFAULT 0,
  committed_after INTEGER NOT NULL DEFAULT 0,
  retry_count INTEGER NOT NULL DEFAULT 0,
  code TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  created_unix_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_partition_id
  ON events(partition_key, event_id);
`
