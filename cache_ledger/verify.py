from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import VerificationError
from .models import SampleBundle
from .runner import execute_command
from .store import LedgerStore, canonical_json


def _diff_values(expected: Any, actual: Any, prefix: str = "") -> list[str]:
    fields: list[str] = []
    if type(expected) != type(actual):
        return [prefix or "value"]
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            path = f"{prefix}.{key}" if prefix else key
            if key not in expected or key not in actual:
                fields.append(path)
            else:
                fields.extend(_diff_values(expected[key], actual[key], path))
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            fields.append(prefix or "list_length")
        else:
            for index, (left, right) in enumerate(zip(expected, actual)):
                fields.extend(_diff_values(left, right, f"{prefix}[{index}]"))
    elif expected != actual:
        fields.append(prefix or "value")
    return fields


def _semantic_decision(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "idempotency_key": decision["idempotency_key"],
        "kind": decision["kind"],
        "tenant": decision["tenant"],
        "entry_key": decision["entry_key"],
        "clock_ms": decision["clock_ms"],
        "policy_fingerprint": decision["policy_fingerprint"],
        "payload": decision["payload"],
    }


def _semantic_status(status: dict[str, Any]) -> dict[str, Any]:
    entries = []
    for entry in status["entries"]:
        item = dict(entry)
        item.pop("updated_decision_seq", None)
        entries.append(item)
    pointer = dict(status["policy_pointer"] or {})
    pointer.pop("decision_seq", None)
    return {
        "tenants": status["tenants"],
        "layers": status["layers"],
        "entries": entries,
        "clock_watermark_ms": status["clock_watermark_ms"],
        "policy_pointer": pointer,
        "decision_count": status["decision_count"],
    }


def verify_replay(store: LedgerStore, bundle: SampleBundle) -> dict[str, Any]:
    prepared = store.pending_operations("prepared")
    running = store.running_commands()
    if prepared or running:
        raise VerificationError(
            "unfinished transaction state; run resume before verify",
            {"prepared_operations": len(prepared), "running_commands": len(running)},
        )

    store.assert_invariants_locked()
    rows = store.conn.execute(
        "SELECT request_id,command,request_json,status FROM command_log WHERE status='completed' ORDER BY request_id"
    ).fetchall()
    replay_path = store.db_path.with_name(store.db_path.name + ".verify-replay.sqlite3")
    LedgerStore.reset_database(replay_path)
    fresh = LedgerStore(replay_path)
    try:
        fresh.load_bundle(bundle)
        for row in rows:
            request = json.loads(row["request_json"])
            execute_command(fresh, bundle, request)

        actual_decisions = store.decisions()
        expected_decisions = fresh.decisions()
        first_divergence: dict[str, Any] | None = None
        for index in range(max(len(actual_decisions), len(expected_decisions))):
            if index >= len(actual_decisions) or index >= len(expected_decisions):
                seq = index + 1
                first_divergence = {
                    "first_divergent_decision_seq": seq,
                    "entry": None,
                    "fields": ["missing_decision"],
                    "actual_present": index < len(actual_decisions),
                    "expected_present": index < len(expected_decisions),
                }
                break
            actual = actual_decisions[index]
            expected = expected_decisions[index]
            comparable_keys = ("seq", "idempotency_key", "kind", "tenant", "entry_key", "clock_ms", "policy_fingerprint", "payload")
            fields = _diff_values(
                {key: expected[key] for key in comparable_keys},
                {key: actual[key] for key in comparable_keys},
            )
            if fields:
                first_divergence = {
                    "first_divergent_decision_seq": actual["seq"],
                    "idempotency_key": actual["idempotency_key"],
                    "entry": actual.get("entry_key"),
                    "fields": fields,
                    "expected": {key: expected.get(key) for key in comparable_keys},
                    "actual": {key: actual.get(key) for key in comparable_keys},
                }
                break

        actual_status = store.status()
        expected_status = fresh.status()
        state_fields = _diff_values(
            {
                "tenants": expected_status["tenants"],
                "layers": expected_status["layers"],
                "entries": expected_status["entries"],
                "clock_watermark_ms": expected_status["clock_watermark_ms"],
                "policy_pointer": expected_status["policy_pointer"],
                "decision_count": expected_status["decision_count"],
            },
            {
                "tenants": actual_status["tenants"],
                "layers": actual_status["layers"],
                "entries": actual_status["entries"],
                "clock_watermark_ms": actual_status["clock_watermark_ms"],
                "policy_pointer": actual_status["policy_pointer"],
                "decision_count": actual_status["decision_count"],
            },
        )
        if first_divergence is None and state_fields:
            first_divergence = {
                "first_divergent_decision_seq": actual_status["decision_count"],
                "entry": None,
                "fields": state_fields,
            }
        exact_match = first_divergence is None
        semantic_divergence: dict[str, Any] | None = None
        if not exact_match:
            expected_by_key = {item["idempotency_key"]: _semantic_decision(item) for item in expected_decisions}
            actual_by_key = {item["idempotency_key"]: _semantic_decision(item) for item in actual_decisions}
            missing = sorted(set(expected_by_key) - set(actual_by_key))
            extra = sorted(set(actual_by_key) - set(expected_by_key))
            if missing or extra:
                semantic_divergence = {
                    "first_divergent_decision_seq": actual_decisions[-1]["seq"] if actual_decisions else 0,
                    "entry": None,
                    "fields": ["idempotency_key_set"],
                    "missing": missing,
                    "extra": extra,
                }
            else:
                for key in sorted(expected_by_key):
                    fields = _diff_values(expected_by_key[key], actual_by_key[key])
                    if fields:
                        actual_item = store.find_decision(key)
                        semantic_divergence = {
                            "first_divergent_decision_seq": actual_item["seq"] if actual_item else None,
                            "idempotency_key": key,
                            "entry": actual_by_key[key].get("entry_key"),
                            "fields": fields,
                        }
                        break
            if semantic_divergence is None:
                state_fields = _diff_values(
                    _semantic_status(expected_status),
                    _semantic_status(actual_status),
                )
                if state_fields:
                    semantic_divergence = {
                        "first_divergent_decision_seq": actual_status["decision_count"],
                        "entry": None,
                        "fields": state_fields,
                    }

        if semantic_divergence is not None:
            details = dict(semantic_divergence)
            details["semantic_divergence"] = dict(semantic_divergence)
            details["exact_replay_divergence_seq"] = (
                first_divergence.get("first_divergent_decision_seq") if first_divergence else None
            )
            raise VerificationError(
                "incremental ledger differs from global one-shot replay",
                details,
            )

        invariants = fresh.assert_invariants_locked()
        import hashlib

        result = {
            "ok": True,
            "verified_decisions": len(actual_decisions),
            "replayed_commands": len(rows),
            "invariants": invariants,
            "consistency_mode": "exact_decision_sequence" if exact_match else "semantic_idempotency_key_concurrent",
            "canonical_state_sha256": hashlib.sha256(
                canonical_json({
                    "status": actual_status,
                    "decisions": sorted(
                        (_semantic_decision(item) for item in actual_decisions),
                        key=lambda item: item["idempotency_key"],
                    ),
                }).encode("utf-8")
            ).hexdigest(),
        }
        if not exact_match:
            result["exact_sequence_note"] = (
                "global seq interleaving differs, but every idempotent decision payload and ledger field matches"
            )
            result["first_exact_sequence_divergence"] = {
                key: first_divergence[key]
                for key in ("first_divergent_decision_seq", "entry", "fields", "idempotency_key")
                if key in first_divergence
            }
        return result
    finally:
        fresh.close()
        for suffix in ("", "-wal", "-shm", ".lock"):
            candidate = Path(str(replay_path) + suffix) if suffix else replay_path
            if candidate.exists():
                candidate.unlink()
