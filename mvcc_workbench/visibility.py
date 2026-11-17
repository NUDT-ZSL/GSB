"""Pure MVCC visibility evaluator and divergence diagnostics.

Both the fast and authoritative paths ultimately call ``evaluate_visibility``.
The paths differ in how they obtain the facts: the fast path uses an in-memory
incremental index and cache, while the authoritative path re-reads txn_events
and follows the SQLite version-chain pointers.
"""

from __future__ import annotations

import json
from typing import Any


VISIBILITY_FORMAT = "mvcc-visibility/v1"


def _bool(value: Any) -> bool:
    return bool(value)


def _candidate_facts(
    versions: list[dict[str, Any]],
    *,
    reader_txid: int,
    snapshot: dict[str, Any],
    tx_states: dict[int, str],
) -> list[dict[str, Any]]:
    active = {int(value) for value in snapshot["active"]}
    snapshot_xmax = int(snapshot["xmax"])
    factors: list[dict[str, Any]] = []
    for index, version in enumerate(versions, start=1):
        xmin = int(version["xmin"])
        xmax_raw = version.get("xmax")
        xmax = int(xmax_raw) if xmax_raw is not None else None
        xmin_state = tx_states.get(xmin, "missing")
        xmax_state = tx_states.get(xmax, "missing") if xmax is not None else "none"
        own_creator = bool(reader_txid) and xmin == reader_txid
        own_deleter = bool(reader_txid) and xmax == reader_txid

        xmin_committed = xmin_state == "committed"
        xmin_in_snapshot = xmin < snapshot_xmax and xmin not in active
        xmin_visible = own_creator or (xmin_committed and xmin_in_snapshot)

        xmax_committed = xmax_state == "committed"
        xmax_in_snapshot = xmax is not None and xmax < snapshot_xmax and xmax not in active
        xmax_visible = own_deleter or (xmax is not None and xmax_committed and xmax_in_snapshot)

        tombstone = _bool(version["is_tombstone"])
        if not xmin_visible:
            if xmin_state == "active":
                reason = "creator_not_visible_active"
            elif xmin_state == "rolled_back":
                reason = "creator_rolled_back"
            elif xmin_state == "missing":
                reason = "creator_missing"
            elif not xmin_committed:
                reason = "creator_not_committed"
            elif not xmin_in_snapshot:
                reason = "creator_outside_snapshot"
            else:
                reason = "creator_not_visible"
            decision = "not_visible"
        elif xmax_visible:
            reason = "superseded_by_visible_xmax"
            decision = "not_visible"
        elif tombstone:
            reason = "visible_tombstone"
            decision = "visible_tombstone"
        else:
            reason = "visible_version"
            decision = "visible"

        value: Any = None
        if not tombstone and version.get("value_json") is not None:
            value = json.loads(version["value_json"])
        factors.append(
            {
                "candidate": {
                    "version_id": int(version["version_id"]),
                    "chain_position": index,
                    "xmin": xmin,
                    "xmax": xmax,
                    "xmax_set_version": version.get("xmax_set_version"),
                    "created_version": int(version["created_version"]),
                    "prev_version_id": version.get("prev_version_id"),
                    "tombstone": tombstone,
                    "value": value,
                    "xmin_state": xmin_state,
                    "xmax_state": xmax_state,
                    "visible": decision in {"visible", "visible_tombstone"},
                    "decision": decision,
                    "reason": reason,
                },
                "basis": {
                    "step": index,
                    "version_id": int(version["version_id"]),
                    "chain_position": index,
                    "xmin": xmin,
                    "xmin_state": xmin_state,
                    "xmin_committed": xmin_committed,
                    "xmin_in_snapshot": xmin_in_snapshot,
                    "xmin_is_reader": own_creator,
                    "xmin_visible": xmin_visible,
                    "xmax": xmax,
                    "xmax_state": xmax_state,
                    "xmax_committed": xmax_committed,
                    "xmax_in_snapshot": xmax_in_snapshot,
                    "xmax_is_reader": own_deleter,
                    "xmax_visible": xmax_visible,
                    "tombstone": tombstone,
                    "decision": decision,
                    "reason": reason,
                },
            }
        )
    return factors


def evaluate_visibility(
    *,
    table: str,
    key: str,
    reader_txid: int,
    view_version: int,
    snapshot: dict[str, Any],
    versions: list[dict[str, Any]],
    tx_states: dict[int, str],
) -> dict[str, Any]:
    """Evaluate a snapshot against a version chain in stable chain order.

    Versions whose creator transaction number is at or beyond the snapshot
    boundary are omitted from the snapshot-qualified candidate chain.  Such a
    version cannot be visible, and omitting it keeps repeated transaction-view
    results byte-stable when unrelated newer transactions commit later.
    """

    snapshot_xmax = int(snapshot["xmax"])
    qualified = [
        version
        for version in versions
        if int(version["xmin"]) < snapshot_xmax
        or (reader_txid and int(version["xmin"]) == int(reader_txid))
    ]
    factors = _candidate_facts(
        qualified,
        reader_txid=reader_txid,
        snapshot=snapshot,
        tx_states=tx_states,
    )
    candidates = [item["candidate"] for item in factors]
    basis = [item["basis"] for item in factors]

    selected: dict[str, Any] | None = None
    for candidate in candidates:
        if candidate["visible"]:
            selected = candidate

    if selected is None:
        visible = False
        value = None
        selected_version_id = None
        deleted = False
        reason = "no_visible_version"
    elif selected["tombstone"]:
        visible = False
        value = None
        selected_version_id = selected["version_id"]
        deleted = True
        reason = "visible_tombstone"
    else:
        visible = True
        value = selected["value"]
        selected_version_id = selected["version_id"]
        deleted = False
        reason = "visible_version"

    return {
        "format": VISIBILITY_FORMAT,
        "table": table,
        "key": key,
        "reader_txid": int(reader_txid),
        "view_version": int(view_version),
        "snapshot": {
            "xmin": int(snapshot["xmin"]),
            "xmax": int(snapshot["xmax"]),
            "active": sorted(int(value) for value in snapshot["active"]),
        },
        "visible": visible,
        "deleted": deleted,
        "value": value,
        "selected_version_id": selected_version_id,
        "reason": reason,
        "basis": basis,
        "candidates": candidates,
    }


def first_divergence(left: Any, right: Any, path: str = "$") -> dict[str, Any]:
    """Return the first recursively encountered byte-relevant divergence."""

    if type(left) is not type(right):
        return {"path": path, "left_type": type(left).__name__, "right_type": type(right).__name__}
    if isinstance(left, dict):
        left_keys = sorted(left)
        right_keys = sorted(right)
        if left_keys != right_keys:
            missing_right = sorted(set(left_keys) - set(right_keys))
            missing_left = sorted(set(right_keys) - set(left_keys))
            return {"path": path, "missing_on_right": missing_right, "missing_on_left": missing_left}
        for key in left_keys:
            if left[key] != right[key]:
                return first_divergence(left[key], right[key], f"{path}.{key}")
        return {"path": path, "left": left, "right": right}
    if isinstance(left, list):
        if len(left) != len(right):
            return {"path": path, "left_length": len(left), "right_length": len(right)}
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            if left_item != right_item:
                return first_divergence(left_item, right_item, f"{path}[{index}]")
        return {"path": path, "left": left, "right": right}
    if left != right:
        return {"path": path, "left": left, "right": right}
    return {"path": path, "left": left, "right": right}
