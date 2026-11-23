"""Replay the event journal and compare every committed snapshot."""

from __future__ import annotations

from typing import Any

from .canonical import canonical_json, sha256_obj, sha256_text
from .engine import Engine, build_genesis
from .errors import GatewayError
from .store import Store, apply_event_to_engine


def _first_difference(left: Any, right: Any, path: str = "$") -> dict[str, Any] | None:
    if type(left) is not type(right):
        return {
            "path": path,
            "expected_type": type(left).__name__,
            "actual_type": type(right).__name__,
            "expected": left,
            "actual": right,
        }
    if isinstance(left, dict):
        left_keys = set(left)
        right_keys = set(right)
        if left_keys != right_keys:
            return {
                "path": path,
                "expected_keys": sorted(left_keys),
                "actual_keys": sorted(right_keys),
            }
        for key in sorted(left):
            difference = _first_difference(left[key], right[key], f"{path}.{key}")
            if difference is not None:
                return difference
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return {
                "path": path,
                "expected_length": len(left),
                "actual_length": len(right),
            }
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            difference = _first_difference(
                left_item, right_item, f"{path}[{index}]"
            )
            if difference is not None:
                return difference
        return None
    if left != right:
        return {"path": path, "expected": left, "actual": right}
    return None


def verify_store(store: Store) -> dict[str, Any]:
    bundle = store.read_bundle()
    inputs = bundle["inputs"]
    snapshots = bundle["snapshots"]
    events = bundle["events"]
    pointer_state = bundle["state"]
    pointer_version = bundle["version"]
    pointer_hash = bundle["state_hash"]
    for required in ("config", "services", "requests"):
        if required not in inputs:
            raise GatewayError(
                "E_DB_CORRUPT",
                f"required input blob is missing: {required}",
                kind=required,
            )
    fingerprint = sha256_obj(
        {
            "config": inputs["config"],
            "services": inputs["services"],
            "requests": inputs["requests"],
        }
    )
    genesis_state = build_genesis(
        {
            "config": inputs["config"],
            "services": inputs["services"],
            "requests": inputs["requests"],
            "fingerprint": fingerprint,
        }
    )
    if not snapshots:
        raise GatewayError("E_DB_CORRUPT", "ledger contains no snapshots")
    expected_versions = list(range(1, len(snapshots) + 1))
    actual_versions = [item["version"] for item in snapshots]
    if actual_versions != expected_versions:
        raise GatewayError(
            "E_DB_CORRUPT",
            "snapshot versions are not contiguous",
            expected_versions=expected_versions,
            actual_versions=actual_versions,
        )
    for snapshot in snapshots:
        if snapshot["version"] == 1:
            expected_parent = None
        else:
            expected_parent = snapshot["version"] - 1
        if snapshot["parent_version"] != expected_parent:
            raise GatewayError(
                "E_DB_CORRUPT",
                "snapshot parent pointer is not the previous version",
                version=snapshot["version"],
                expected_parent=expected_parent,
                actual_parent=snapshot["parent_version"],
            )

    genesis_hash = sha256_text(canonical_json(genesis_state))
    if snapshots[0]["state_hash"] != genesis_hash:
        raise GatewayError(
            "E_VERIFY_MISMATCH",
            "genesis snapshot differs from replayed input",
            version=1,
            expected_hash=genesis_hash,
            actual_hash=snapshots[0]["state_hash"],
        )
    if not events or events[0]["event"].get("type") != "genesis":
        raise GatewayError("E_DB_CORRUPT", "genesis event is missing")
    expected_event_versions = [0] + expected_versions[1:]
    actual_event_versions = [item["version"] for item in events]
    if actual_event_versions != expected_event_versions:
        raise GatewayError(
            "E_DB_CORRUPT",
            "event versions do not match the snapshot chain",
            expected_versions=expected_event_versions,
            actual_versions=actual_event_versions,
        )
    genesis_event = events[0]["event"]
    if genesis_event.get("db_id") != fingerprint:
        raise GatewayError(
            "E_VERIFY_MISMATCH",
            "genesis db_id differs from input fingerprint",
            expected=fingerprint,
            actual=genesis_event.get("db_id"),
        )

    engine = Engine(genesis_state)
    snapshots_by_version = {item["version"]: item for item in snapshots}
    for event_row in events[1:]:
        event = event_row["event"]
        apply_event_to_engine(engine, event)
        version = event_row["version"]
        if version not in snapshots_by_version:
            raise GatewayError(
                "E_DB_CORRUPT", "event references a missing snapshot", version=version
            )
        expected_json = canonical_json(engine.state)
        expected_hash = sha256_text(expected_json)
        actual_hash = snapshots_by_version[version]["state_hash"]
        if expected_hash != actual_hash:
            raise GatewayError(
                "E_VERIFY_MISMATCH",
                "replayed state does not match committed snapshot",
                version=version,
                expected_hash=expected_hash,
                actual_hash=actual_hash,
            )

    if pointer_version != snapshots[-1]["version"]:
        raise GatewayError(
            "E_VERIFY_MISMATCH",
            "pointer does not reference the latest snapshot",
            pointer_version=pointer_version,
            latest_snapshot=snapshots[-1]["version"],
        )
    difference = _first_difference(pointer_state, engine.state)
    if difference is not None:
        raise GatewayError(
            "E_VERIFY_MISMATCH",
            "recomputed ledger differs field by field from persisted ledger",
            difference=difference,
        )
    if sha256_text(canonical_json(pointer_state)) != pointer_hash:
        raise GatewayError("E_DB_CORRUPT", "pointer state hash mismatch")

    return {
        "ok": True,
        "command": "verify",
        "verified": True,
        "db_id": fingerprint,
        "versions": len(snapshots),
        "events": len(events),
        "final_version": pointer_version,
        "state_hash": pointer_hash,
        "config_version": int(pointer_state["config_version"]),
        "requests": len(pointer_state["requests"]),
        "tombstones": len(pointer_state["tombstones"]),
    }
