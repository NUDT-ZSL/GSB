"""Business errors with stable E_xxx codes."""

from __future__ import annotations

from typing import Any, Dict, Optional


ERROR_CODES = {
    "E_IO": "A required local file could not be read or written.",
    "E_JSON": "Input is not valid JSON or has an unsupported top-level shape.",
    "E_CONFIG": "The diagnostic configuration is invalid.",
    "E_MANIFEST": "A v2/package manifest is missing or inconsistent.",
    "E_TX_ID": "A transaction identifier is invalid or cannot be canonicalized.",
    "E_V1_ORDER": "v1 transaction identifiers are not strictly increasing.",
    "E_V1_DELAY": "v1 relative_delay_ms is not a non-negative integer.",
    "E_V1_BYTES": "v1 payload_bytes is not a positive integer.",
    "E_V1_COMMIT": "v1 committed flags are not a committed-prefix followed by an uncommitted suffix.",
    "E_V1_NODE": "A v1 node identifier is not a non-empty string.",
    "E_V2_HASH": "A v2 shard or metadata SQLite file hash does not match its manifest.",
    "E_V2_SCHEMA": "The v2 SQLite metadata database has an invalid schema or row type.",
    "E_V2_GENERATION": "v2 replay_generation is negative or decreases in transaction order.",
    "E_V2_TIMESTAMP": "A v2 timestamp is not a non-negative integer number of milliseconds.",
    "E_V2_REPLAY_BEFORE_COMMIT": "v2 replay_time_ms is earlier than commit_time_ms.",
    "E_V2_BYTES": "v2 payload_bytes is not a positive integer.",
    "E_V2_TOMBSTONE": "v2 tombstone_applied does not correspond to a replay generation.",
    "E_V2_METADATA_DIGEST": "A v2 shard record digest does not match its SQLite metadata row.",
    "E_DUPLICATE_TX": "A transaction occurs more than once inside one source generation.",
    "E_AMBIGUOUS_TX": "Cross-version records tie on all arbitration fields but have different content.",
    "E_LOG_ORDER": "The replay transaction stream is not strictly ordered.",
    "E_LOG_DUPLICATE": "The replay transaction stream contains a duplicate transaction.",
    "E_LOG_GAP": "The replay transaction stream contains a position discontinuity.",
    "E_LOG_COMMIT": "The replay stream has an applied record after an unapplied record.",
    "E_LOG_BYTES": "Replay-stream bytes disagree with the merged position record.",
    "E_LOG_ORPHAN": "A replay-stream transaction has no merged position record.",
    "E_POSITION_GAP": "Merged positions contain a transaction-number discontinuity.",
    "E_CLOCK_ROLLBACK": "The injected logical clock moves backwards in transaction order.",
    "E_MISSING_TIME": "A delay conclusion would require missing time information.",
    "E_POSITION_ROLLBACK": "A package transaction upper bound is below the committed watermark.",
    "E_VERSION_ROLLBACK": "A replacement package version is older than the active version.",
    "E_PACKAGE_INCOMPLETE": "A package does not contain the range needed for its declared mode.",
    "E_PACKAGE_OVERLAP": "An incremental package overlaps records that it may not replace.",
    "E_STATE": "The persistent state is absent, incomplete, or inconsistent.",
    "E_RECOVERY": "A staged operation cannot be resumed without all of its staged data.",
    "E_LOCK": "The state lock could not be acquired.",
    "E_VERIFY": "Persisted state does not match the reproducible lag output.",
    "E_INJECTION": "An injection manifest entry is malformed or unsupported.",
}


class DiagnosticsError(Exception):
    """A user-facing validation/business failure.

    ``details`` is deliberately JSON-serializable so the CLI can return stable
    machine-readable failures on stdout.
    """

    def __init__(
        self,
        code: str,
        message: Optional[str] = None,
        *,
        location: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown business error code: {code}")
        self.code = code
        self.message = message or ERROR_CODES[code]
        self.location = location
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }
        if self.location:
            result["error"]["location"] = self.location
        if self.details:
            result["error"]["details"] = self.details
        return result
