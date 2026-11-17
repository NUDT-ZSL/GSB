"""In-memory incremental visibility index and wholesale-invalidated cache."""

from __future__ import annotations

import copy
import threading
from collections import defaultdict
from typing import Any

from .storage import Storage
from .util import canonical_json, sha256_text


class VisibilityIndex:
    """Append-only mirror of versions/events used by the interactive path."""

    def __init__(self) -> None:
        self._versions: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        self._events: list[dict[str, Any]] = []
        self._tables: set[str] = set()
        self._order = 0
        self._lock = threading.RLock()

    def rebuild(self, storage: Storage) -> None:
        with storage.read_transaction() as conn:
            tables = {row["table_name"] for row in conn.execute("SELECT table_name FROM tables_meta")}
            versions = [dict(row) for row in conn.execute("SELECT * FROM versions ORDER BY version_id")]
            events = [
                dict(row)
                for row in conn.execute(
                    "SELECT event_id,txid,event,state_version,at_ns FROM txn_events "
                    "ORDER BY state_version,event_id"
                )
            ]
        with self._lock:
            self._tables = tables
            self._versions = defaultdict(list)
            for row in versions:
                self._versions[(row["table_name"], row["row_key"])].append(row)
            self._events = events
            self._order = max((int(row["event_id"]) for row in events), default=0)

    def apply_table(self, table_name: str) -> None:
        with self._lock:
            self._tables.add(table_name)

    def apply_event(self, txid: int, event: str, state_version: int, at_ns: int) -> None:
        with self._lock:
            self._order += 1
            self._events.append(
                {
                    "event_id": self._order,
                    "txid": int(txid),
                    "event": event,
                    "state_version": int(state_version),
                    "at_ns": int(at_ns),
                }
            )

    def apply_version(self, version: dict[str, Any]) -> None:
        with self._lock:
            table_name = version.get("table_name", version.get("table"))
            row_key = version.get("row_key", version.get("key"))
            row = {
                "version_id": int(version["version_id"]),
                "table_name": table_name,
                "row_key": row_key,
                "xmin": int(version["xmin"]),
                "xmax": version.get("xmax"),
                "value_json": (
                    version.get("value_json")
                    if "value_json" in version
                    else (canonical_json(version["value"]) if not version.get("is_tombstone") else None)
                ),
                "is_tombstone": 1 if version.get("is_tombstone") else 0,
                "prev_version_id": version.get("prev_version_id"),
                "next_version_id": version.get("next_version_id"),
                "created_version": int(version["created_version"]),
                "xmax_set_version": version.get("xmax_set_version"),
            }
            self._versions[(table_name, row_key)].append(row)
            # Appending a version moves the previous head's xmax state.
            if row["prev_version_id"] is not None:
                for previous in self._versions[(table_name, row_key)]:
                    if int(previous["version_id"]) == int(row["prev_version_id"]):
                        previous["xmax"] = row["xmin"]
                        previous["next_version_id"] = row["version_id"]
                        previous["xmax_set_version"] = row["created_version"]
                        break

    def snapshot_at(self, view_version: int) -> dict[str, Any]:
        with self._lock:
            states = self.tx_states_at(view_version)
            next_txid = self.next_txid_at(view_version)
        active = sorted(txid for txid, state in states.items() if state == "active")
        return {
            "xmin": min(active) if active else next_txid,
            "xmax": next_txid,
            "active": active,
            "states": states,
        }

    def tx_states_at(self, view_version: int) -> dict[int, str]:
        with self._lock:
            states: dict[int, str] = {}
            for row in self._events:
                if int(row["state_version"]) > view_version:
                    continue
                states[int(row["txid"])] = {
                    "begin": "active",
                    "commit": "committed",
                    "rollback": "rolled_back",
                }[row["event"]]
            return states

    def next_txid_at(self, view_version: int) -> int:
        with self._lock:
            maximum = max(
                (int(row["txid"]) for row in self._events if int(row["state_version"]) <= view_version),
                default=0,
            )
            return maximum + 1

    def chain_at(self, table_name: str, row_key: str, view_version: int) -> list[dict[str, Any]]:
        with self._lock:
            source = self._versions.get((table_name, row_key), [])
            chain = []
            for row in source:
                if int(row["created_version"]) > view_version:
                    continue
                item = dict(row)
                if item["xmax_set_version"] is not None and int(item["xmax_set_version"]) > view_version:
                    item["xmax"] = None
                    item["xmax_set_version"] = None
                chain.append(item)
            chain.sort(key=lambda item: int(item["version_id"]))
            return chain

    def has_table(self, table_name: str) -> bool:
        with self._lock:
            return table_name in self._tables


class SnapshotCache:
    """Result cache whose entries all belong to one persisted state version."""

    def __init__(self) -> None:
        self._entries: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._bound_version = 0
        self._generation = 0
        self._invalidations = 0
        self._reason: str | None = "cold_start"
        self._hits = 0
        self._misses = 0
        self._lock = threading.RLock()

    @staticmethod
    def key_for(
        *,
        view_version: int,
        snapshot: dict[str, Any],
        reader_txid: int,
        table: str,
        key: str,
    ) -> tuple[Any, ...]:
        fingerprint_payload = {
            "view_version": int(view_version),
            "snapshot": {
                "xmin": int(snapshot["xmin"]),
                "xmax": int(snapshot["xmax"]),
                "active": sorted(int(value) for value in snapshot["active"]),
            },
            "reader_txid": int(reader_txid),
            "table": table,
            "key": key,
        }
        fingerprint = sha256_text(canonical_json(fingerprint_payload))
        return (int(view_version), int(reader_txid), table, key, fingerprint)

    def ensure_version(self, state_version: int, reason: str = "state_version_changed") -> None:
        with self._lock:
            if state_version != self._bound_version:
                self._entries.clear()
                self._bound_version = int(state_version)
                self._generation += 1
                self._invalidations += 1
                self._reason = reason

    def invalidate(self, state_version: int, reason: str) -> None:
        with self._lock:
            self._entries.clear()
            self._bound_version = int(state_version)
            self._generation += 1
            self._invalidations += 1
            self._reason = reason

    def get(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                self._misses += 1
                return None
            self._hits += 1
            return copy.deepcopy(value)

    def put(self, key: tuple[Any, ...], value: dict[str, Any]) -> None:
        with self._lock:
            self._entries[key] = copy.deepcopy(value)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "bound_state_version": self._bound_version,
                "generation": self._generation,
                "entry_count": len(self._entries),
                "invalidations": self._invalidations,
                "last_invalidation_reason": self._reason,
                "hits": self._hits,
                "misses": self._misses,
            }
