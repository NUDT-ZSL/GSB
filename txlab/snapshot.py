"""Named, self-contained execution snapshots and full-state restoration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import E_REPLAY, ReplayError
from .storage import SQLiteStore
from .util import (
    atomic_write_json,
    read_json,
    safe_snapshot_name,
    state_digest,
    utc_now_iso,
)


SNAPSHOT_FORMAT_VERSION = 1


def build_snapshot(
    name: str,
    result: dict[str, Any],
    *,
    initial_sql: str | None = None,
) -> dict[str, Any]:
    initial_rows = result["initial_rows"]
    final_rows = result["final_rows"]
    final_version = int(result["final_data_version"])
    snapshot = {
        "snapshot_format_version": SNAPSHOT_FORMAT_VERSION,
        "application": "sqlite-txn-lock-lab",
        "name": name,
        "saved_at": utc_now_iso(),
        "script": result["script"],
        "isolation_levels": {
            session["session_id"]: session["isolation"]
            for session in result["sessions"]
        },
        "config": result["config"],
        "execution": result,
        "initial_sql": initial_sql,
        "initial_state": {
            "rows": initial_rows,
            "data_version": int(result["initial_data_version"]),
            "digest": state_digest(initial_rows),
        },
        "result_state": {
            "rows": final_rows,
            "data_version": final_version,
            "digest": result["actual_digest"],
        },
        # This is the state/session version used to decide whether a restore
        # is a wholesale replacement of a different workspace generation.
        "state_version": final_version,
    }
    return snapshot


def save_snapshot(
    snapshot_dir: str | Path,
    name: str,
    result: dict[str, Any],
    *,
    initial_sql: str | None = None,
) -> Path:
    safe_name = safe_snapshot_name(name)
    payload = build_snapshot(safe_name, result, initial_sql=initial_sql)
    path = Path(snapshot_dir) / f"{safe_name}.json"
    atomic_write_json(path, payload)
    return path


def load_snapshot(path: str | Path) -> dict[str, Any]:
    raw = read_json(path, code=E_REPLAY, label="snapshot")
    validate_snapshot_payload(raw)
    return raw


def validate_snapshot_payload(snapshot: Any) -> None:
    if not isinstance(snapshot, dict):
        raise ReplayError("snapshot root must be a JSON object")
    format_version = snapshot.get("snapshot_format_version")
    if format_version != SNAPSHOT_FORMAT_VERSION:
        raise ReplayError(
            f"unsupported snapshot format version {format_version!r}",
            details={"supported": SNAPSHOT_FORMAT_VERSION},
        )
    required = (
        "name",
        "script",
        "config",
        "execution",
        "initial_state",
        "result_state",
        "state_version",
    )
    missing = [key for key in required if key not in snapshot]
    if missing:
        raise ReplayError(
            "snapshot is missing required fields",
            details={"missing": missing},
        )
    if not isinstance(snapshot["initial_state"], dict) or not isinstance(
        snapshot["result_state"], dict
    ):
        raise ReplayError("snapshot state sections must be objects")
    for section in ("initial_state", "result_state"):
        state = snapshot[section]
        if "rows" not in state or "data_version" not in state:
            raise ReplayError(
                f"snapshot {section} must contain rows and data_version"
            )
        if not isinstance(state["rows"], list):
            raise ReplayError(f"snapshot {section}.rows must be an array")
        if not isinstance(state["data_version"], int):
            raise ReplayError(f"snapshot {section}.data_version must be an integer")
        stored_digest = state.get("digest")
        if stored_digest is not None and stored_digest != state_digest(state["rows"]):
            raise ReplayError(
                f"snapshot {section} digest does not match its rows",
                details={
                    "stored_digest": stored_digest,
                    "recomputed_digest": state_digest(state["rows"]),
                },
            )
    if int(snapshot["state_version"]) != int(
        snapshot["result_state"]["data_version"]
    ):
        raise ReplayError(
            "snapshot state_version does not match result_state.data_version",
            details={
                "state_version": snapshot["state_version"],
                "result_data_version": snapshot["result_state"]["data_version"],
            },
        )
    result_rows = snapshot["result_state"]["rows"]
    stored_digest = snapshot["result_state"].get("digest")
    if stored_digest is not None and stored_digest != state_digest(result_rows):
        raise ReplayError(
            "snapshot result digest does not match its rows",
            details={
                "stored_digest": stored_digest,
                "recomputed_digest": state_digest(result_rows),
            },
        )


def restore_snapshot(path: str | Path, store: SQLiteStore) -> dict[str, Any]:
    snapshot = load_snapshot(path)
    result_state = snapshot["result_state"]
    target_version = int(snapshot["state_version"])
    snapshot_rows = result_state["rows"]

    current_version = store.data_version()
    current_rows = store.load_rows()
    current_digest = state_digest(current_rows)
    snapshot_digest = state_digest(snapshot_rows)
    version_mismatch = current_version != target_version
    content_mismatch = current_digest != snapshot_digest

    # Restoration is intentionally all-or-nothing.  No row-level merge is
    # attempted, even when the versions happen to match.
    store.replace_state(snapshot_rows, target_version)
    restored_version = store.data_version()
    restored_digest = state_digest(store.load_rows())
    if restored_version != target_version or restored_digest != snapshot_digest:
        raise ReplayError(
            "full-state restore verification failed",
            details={
                "expected_version": target_version,
                "actual_version": restored_version,
                "expected_digest": snapshot_digest,
                "actual_digest": restored_digest,
            },
        )

    return {
        "code": "OK",
        "snapshot_name": snapshot["name"],
        "snapshot_format_version": snapshot["snapshot_format_version"],
        "restore_mode": "full_replace",
        "current_session_version": current_version,
        "snapshot_session_version": target_version,
        "version_mismatch": version_mismatch,
        "content_mismatch": content_mismatch,
        "previous_digest": current_digest,
        "restored_digest": restored_digest,
        "restored_data_version": restored_version,
        "replaced_row_count": len(snapshot_rows),
    }


def list_snapshots(snapshot_dir: str | Path) -> list[dict[str, Any]]:
    directory = Path(snapshot_dir)
    if not directory.exists():
        return []
    snapshots = []
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
        try:
            payload = load_snapshot(path)
            snapshots.append(
                {
                    "name": payload["name"],
                    "path": str(path),
                    "saved_at": payload.get("saved_at"),
                    "state_version": payload["state_version"],
                    "script_name": payload["execution"].get("script_name"),
                }
            )
        except Exception as exc:
            snapshots.append(
                {
                    "name": path.stem,
                    "path": str(path),
                    "error": str(exc),
                }
            )
    return snapshots
