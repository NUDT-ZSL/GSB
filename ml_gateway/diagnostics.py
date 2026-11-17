from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import canonical_json
from .engine import GatewayEngine
from .fingerprint import FINGERPRINT_QUANTIZATION
from .stats import stats_summary


def build_diagnostic_report(engine: GatewayEngine) -> dict[str, Any]:
    """Build a stable, JSON-safe current-view diagnostic report."""

    state = engine.state
    stats = state.get("stats", {})
    backend_rows: list[dict[str, Any]] = []
    current_states = engine.states
    for backend_id in sorted(current_states):
        backend = current_states[backend_id]
        backend_rows.append(
            {
                "backend_id": backend.backend_id,
                "canonical_vector": backend.canonical_vector,
                "config_digest": backend.config_digest,
                "content_signature": backend.content_signature,
                "fingerprint": backend.fingerprint,
                "host": backend.host,
                "load_sequence": backend.load_sequence,
                "max_concurrency": backend.max_concurrency,
                "model_id": backend.model_id,
                "parameter_length": backend.parameter_length,
                "port": backend.port,
            }
        )

    effective_hits: dict[str, int] = {}
    invalid_hits: dict[str, int] = {}
    for entry in state.get("ledger", []):
        target = effective_hits if entry.get("effective") else invalid_hits
        target[entry["rule_id"]] = target.get(entry["rule_id"], 0) + 1
    rule_rows: list[dict[str, Any]] = []
    for rule in engine.bundle.ordered_rules():
        rule_rows.append(
            {
                "batch_size": rule.batch_size,
                "effective_hits": effective_hits.get(rule.rule_id, 0),
                "invalid_hits": invalid_hits.get(rule.rule_id, 0),
                "match": rule.match_dict(),
                "priority": rule.priority,
                "rule_id": rule.rule_id,
                "target_backend": rule.target_backend,
            }
        )

    ledger_rows = engine.binding_rows()
    invalid_attributions = sorted(
        [
            row
            for row in ledger_rows
            if not row["effective"] or not row["binding_valid"]
        ],
        key=lambda row: (row["request_index"], row["request_id"], row["entry_id"]),
    )

    batch_rows: list[dict[str, Any]] = []
    for batch in sorted(
        state.get("batches", []), key=lambda item: (item["batch_seq"], item["batch_id"])
    ):
        batch_rows.append(
            {
                "backend_id": batch["backend_id"],
                "batch_id": batch["batch_id"],
                "batch_seq": int(batch["batch_seq"]),
                "bound_fingerprint": batch["bound_fingerprint"],
                "created_epoch": int(batch.get("created_epoch", 0)),
                "duration_ms": batch.get("duration_ms"),
                "error_code": batch.get("error_code"),
                "error_message": batch.get("error_message"),
                "output_summary": batch.get("output_summary"),
                "redispatch_of": batch.get("redispatch_of"),
                "request_ids": list(batch.get("request_ids", [])),
                "rule_id": batch["rule_id"],
                "status": batch["status"],
                "superseded_by": batch.get("superseded_by"),
            }
        )

    snapshot_rows = []
    for snapshot in engine.store.list_snapshots():
        snapshot_rows.append(
            {
                "backend_fingerprints": dict(
                    sorted(snapshot["backend_fingerprints"].items())
                ),
                "created_logical_epoch": int(snapshot["created_logical_epoch"]),
                "label": snapshot["label"],
                "ordinal": int(snapshot["ordinal"]),
                "rule_version": snapshot["rule_version"],
                "snapshot_id": snapshot["snapshot_id"],
                "state_digest": snapshot["state_digest"],
                "stats_version": int(snapshot["stats_version"]),
                "view_version": int(snapshot["view_version"]),
            }
        )

    stats_version_rows = []
    for version in engine.store.list_stats_versions():
        metadata = version["metadata"]
        stats_version_rows.append(
            {
                "affected_backends": list(metadata.get("affected_backends", [])),
                "affected_rules": list(metadata.get("affected_rules", [])),
                "changed_stat_keys": list(metadata.get("changed_stat_keys", [])),
                "invalidated_entry_count": int(
                    metadata.get("invalidated_entry_count", 0)
                ),
                "previous_version": metadata.get("previous_version"),
                "reason": version["reason"],
                "recalculated_stat_keys": list(
                    metadata.get("recalculated_stat_keys", [])
                ),
                "redispatched_batch_ids": list(
                    metadata.get("redispatched_batch_ids", [])
                ),
                "readonly": bool(version["readonly"]),
                "stats_summary": stats_summary(version["stats"]),
                "stats_version": int(version["stats_version"]),
                "unchanged_stat_keys": list(metadata.get("unchanged_stat_keys", [])),
                "view_version": int(version["view_version"]),
                "void_batch_ids": list(metadata.get("void_batch_ids", [])),
            }
        )

    return {
        "backends": backend_rows,
        "batches": batch_rows,
        "counts": {
            "batches": len(batch_rows),
            "effective_ledger_entries": sum(
                1 for row in ledger_rows if row["effective"]
            ),
            "invalid_ledger_entries": len(invalid_attributions),
            "ledger_entries": len(ledger_rows),
            "redispatched_batches": sum(
                1 for batch in batch_rows if batch["redispatch_of"]
            ),
            "snapshots": len(snapshot_rows),
            "void_batches": sum(1 for batch in batch_rows if batch["status"] == "VOID"),
        },
        "current_view_version": int(state["view_version"]),
        "current_stats_version": int(state["stats_version"]),
        "fingerprint_quantization": dict(FINGERPRINT_QUANTIZATION),
        "invalid_attributions": invalid_attributions,
        "ledger": ledger_rows,
        "redispatched_batches": [
            batch for batch in batch_rows if batch["redispatch_of"] is not None
        ],
        "rule_version": engine.bundle.rule_version,
        "rules": rule_rows,
        "snapshots": snapshot_rows,
        "stats": stats_summary(stats),
        "stats_versions": stats_version_rows,
        "void_batches": [batch for batch in batch_rows if batch["status"] == "VOID"],
    }


def write_json_report(path: str | Path, payload: dict[str, Any]) -> str:
    report_path = Path(path).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    report_path.write_text(text + "\n", encoding="utf-8", newline="\n")
    return str(report_path)


def stable_event(event: str, **payload: Any) -> str:
    return canonical_json({"event": event, **payload})
