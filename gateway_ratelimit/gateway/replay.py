"""Deterministic replay of ledger batches and field-level comparison."""

from __future__ import annotations

import json

from .engine import GatewayEngine
from .errors import E_REPLAY, BusinessError
from .ledger import Ledger
from .models import (
    LIMIT_PATHS,
    Request,
    Route,
    SignatureRecord,
    TokenRecord,
    TokenTable,
)


def rebuild_engine(routes_payload: list[dict], token_payload: dict) -> GatewayEngine:
    routes = [
        Route(
            name=item["name"],
            prefix=item["prefix"],
            auth_policy=item["auth_policy"],
            rate_per_sec=item["rate_per_sec"],
            burst=item["burst"],
            window_ms=item["window_ms"],
            max_in_window=item["max_in_window"],
            source_line=item.get("source_line", 0),
        )
        for item in routes_payload
    ]
    table = TokenTable(version=token_payload["version"])
    for item in token_payload["tokens"]:
        table.tokens[item["token"]] = TokenRecord(
            token=item["token"],
            client_id=item["client_id"],
            expires_at_ms=item["expires_at_ms"],
            source_line=item.get("source_line", 0),
        )
    for item in token_payload["signatures"]:
        table.signatures[item["key_id"]] = SignatureRecord(
            key_id=item["key_id"],
            client_id=item["client_id"],
            secret=item["secret"],
            expires_at_ms=item["expires_at_ms"],
            source_line=item.get("source_line", 0),
        )
    return routes, table


def _to_request(payload: dict) -> Request:
    return Request(
        seq=payload["seq"],
        route=payload["route"],
        client_id=payload["client_id"],
        token=payload["token"],
        ts_ms=payload["ts_ms"],
        signed_at_ms=payload.get("signed_at_ms"),
        signature=payload.get("signature"),
        source_line=payload.get("source_line", 0),
    )


def compare_decision(recorded: dict, replayed: dict) -> dict | None:
    """Return the first mismatching (seq, path/field) description or None."""
    if recorded.get("decision") != replayed.get("decision"):
        return {
            "seq": recorded["seq"],
            "scope": "top",
            "field": "decision",
            "recorded": recorded.get("decision"),
            "replayed": replayed.get("decision"),
        }
    if recorded.get("auth", {}).get("code") != replayed.get("auth", {}).get("code"):
        return {
            "seq": recorded["seq"],
            "scope": "auth",
            "field": "code",
            "recorded": recorded.get("auth", {}).get("code"),
            "replayed": replayed.get("auth", {}).get("code"),
        }
    for path in LIMIT_PATHS:
        left = recorded.get("paths", {}).get(path, {})
        right = replayed.get("paths", {}).get(path, {})
        for field in ("allow", "remaining", "reason"):
            if left.get(field) != right.get(field):
                return {
                    "seq": recorded["seq"],
                    "scope": path,
                    "field": field,
                    "recorded": left.get(field),
                    "replayed": right.get(field),
                }
    return None


def replay_ledger(ledger: Ledger) -> dict:
    """Replay every complete batch from empty state; raise at first mismatch.

    Batches are grouped by policy version. Each version group rebuilds a fresh
    engine from the persisted policy snapshot and replays its batches in order,
    which mirrors the wholesale state replacement performed at runtime.
    """
    results = {"batches_checked": 0, "status": "match", "versions": []}
    snapshots = ledger.policy_snapshots()
    grouped: dict[str, list] = {}
    for batch in ledger.complete_batches():
        grouped.setdefault(batch["policy_version"], []).append(batch)

    for version_key in sorted(grouped, key=lambda key: json.loads(key)):
        if version_key not in snapshots:
            raise BusinessError(
                E_REPLAY,
                f"policy snapshot {version_key} missing from ledger",
            )
        snapshot = snapshots[version_key]
        routes, table = rebuild_engine(snapshot["routes"], snapshot["token_table"])
        routes_version, token_version = json.loads(version_key)
        engine = GatewayEngine(routes, routes_version, table)
        for batch in grouped[version_key]:
            batch_id = batch["batch_id"]
            rows = ledger.batch_requests(batch_id)
            mismatch = None
            for request_payload, recorded in rows:
                request = _to_request(request_payload)
                replayed = engine.process(request)
                mismatch = compare_decision(recorded, replayed)
                if mismatch is not None:
                    mismatch["batch_id"] = batch_id
                    ledger.record_replay(batch_id, "mismatch", mismatch)
                    raise BusinessError(
                        E_REPLAY,
                        "first replay mismatch at "
                        f"seq={mismatch['seq']} scope={mismatch['scope']} "
                        f"field={mismatch['field']} recorded={mismatch['recorded']!r} "
                        f"replayed={mismatch['replayed']!r}",
                    )
            ledger.record_replay(batch_id, "match")
            results["batches_checked"] += 1
        results["versions"].append(json.loads(version_key))
    return results
