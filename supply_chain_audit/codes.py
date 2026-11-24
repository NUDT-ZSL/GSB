"""Stable business codes and format/version identifiers."""

SCHEMA_V1 = "supply-chain-audit/v1"
SCHEMA_V2_MANIFEST = "supply-chain-audit/v2-manifest"
SCHEMA_V2_SHARD = "supply-chain-audit/v2-shard"
SCHEMA_CLOCK = "supply-chain-audit/observation-clock/v1"
SCHEMA_DEPENDENCIES = "supply-chain-audit/dependencies/v1"
SCHEMA_REVOCATIONS = "supply-chain-audit/revocations/v1"

CHECKPOINT_SCHEMA_VERSION = 1
ALGORITHM_WHITELIST_VERSION = "algo-whitelist-1"
MERGE_POLICY_VERSION = "merge-policy-1"

E_PARSE_JSON = "E_PARSE_JSON"
E_FILE_NOT_FOUND = "E_FILE_NOT_FOUND"
E_CANONICAL_KEY = "E_CANONICAL_KEY"
E_DUP_KEY = "E_DUP_KEY"
E_VERSION_SYNTAX = "E_VERSION_SYNTAX"
E_CONSTRAINT_SYNTAX = "E_CONSTRAINT_SYNTAX"
E_HASH_HEX = "E_HASH_HEX"
E_HASH_ALGO = "E_HASH_ALGO"
E_HASH_MISMATCH = "E_HASH_MISMATCH"
E_SIGNATURE_STATUS = "E_SIGNATURE_STATUS"
E_NON_NEGATIVE_INT = "E_NON_NEGATIVE_INT"
E_TOMBSTONE_TYPE = "E_TOMBSTONE_TYPE"
E_METADATA_MISMATCH = "E_METADATA_MISMATCH"
E_DEPENDENCY_KEY = "E_DEPENDENCY_KEY"
E_DEP_UNRESOLVED = "E_DEP_UNRESOLVED"
E_DEP_CYCLE = "E_DEP_CYCLE"
E_TIME_CONFLICT = "E_TIME_CONFLICT"
E_GENERATION_CONFLICT = "E_GENERATION_CONFLICT"
E_ALGO_CONFLICT = "E_ALGO_CONFLICT"
E_RECOMPUTE_DIVERGE = "E_RECOMPUTE_DIVERGE"
E_TIMEOUT = "E_TIMEOUT"
E_QUEUE_FULL = "E_QUEUE_FULL"
E_HASH_BUDGET = "E_HASH_BUDGET"
E_CHECKPOINT_CORRUPT = "E_CHECKPOINT_CORRUPT"
E_CHECKPOINT_VERSION = "E_CHECKPOINT_VERSION"
E_CHECKPOINT_MISSING = "E_CHECKPOINT_MISSING"
E_REVOKED = "E_REVOKED"
E_AFFECTED = "E_AFFECTED"
E_UNKNOWN_ARTIFACT = "E_UNKNOWN_ARTIFACT"
E_CLOCK_SCHEMA = "E_CLOCK_SCHEMA"
E_CLOCK_REQUIRED = "E_CLOCK_REQUIRED"

SIGNATURE_STATUSES = frozenset({"signed", "unsigned", "invalid_signature"})


def rejection(snapshot_id, artifact_key, code, message, **extra):
    """Create a stable rejection record."""
    record = {
        "record_type": "rejection",
        "snapshot_id": snapshot_id,
        "artifact_key": artifact_key,
        "code": code,
        "message": message,
    }
    record.update(extra)
    return record
