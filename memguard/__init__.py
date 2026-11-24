"""memguard: offline memory-boundary guard for deterministic access traces."""

__version__ = "0.1.0"

# Layout / structure versions. Bumping any of these forces a full rebuild
# instead of an incremental continuation.
LEDGER_LAYOUT_VERSION = 1
CHECKPOINT_STRUCT_VERSION = 1
SNAPSHOT_VERSION = 1
REPORT_SCHEMA_VERSION = 1
