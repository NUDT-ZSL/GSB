from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .errors import EdgeError, ProtocolError, StateError, VersionError, error_payload, exit_code_for
from .ledger import ActiveState, Ledger
from .tcp import JsonLineServer, RequestResult, rpc
from .util import JsonLogger, canonical_json, emit_json_line, monotonic_ms, utc_now_text


@dataclass
class SourceClient:
    host: str
    port: int

    def fetch_content(self, path: str, ledger_version: int) -> dict[str, Any]:
        return rpc(
            self.host,
            self.port,
            {"op": "content", "path": path, "ledger_version": ledger_version},
        )


class NodeCache:
    """A persistent cache facade; no cache entry exists only in process memory."""

    def __init__(
        self,
        ledger: Ledger,
        node_id: str,
        source: SourceClient,
        now_ms: Callable[[], int] = monotonic_ms,
        logger: JsonLogger | None = None,
    ) -> None:
        self.ledger = ledger
        self.node_id = node_id
        self.source = source
        self.now_ms = now_ms
        self.logger = logger

    def _remaining(self, row: Mapping[str, Any], now: int) -> int:
        elapsed = max(0, now - int(row["ttl_anchor_mono_ms"]))
        return max(0, int(row["remaining_ttl_ms"]) - elapsed)

    def _view(self, row: Mapping[str, Any], now: int) -> dict[str, Any]:
        return {
            "node_id": row["node_id"],
            "cache_key": row["cache_key"],
            "path": row["path"],
            "key_fields": json.loads(row["key_fields_json"]),
            "etag": row["etag"],
            "entry_version": int(row["entry_version"]),
            "ttl_ms": int(row["ttl_ms"]),
            "remaining_ttl_ms": self._remaining(row, now),
            "ledger_version": int(row["ledger_version"]),
            "written_mono_ms": int(row["written_mono_ms"]),
            "last_access_mono_ms": int(row["last_access_mono_ms"]),
        }

    def _cache_key(self, active: ActiveState, path: str, key_values: Mapping[str, Any] | None) -> tuple[str, list[str]]:
        cache_key = active.policy.key_for(path, key_values)
        return cache_key, list(active.policy.key_fields)

    def _values_from_cache_key(self, active: ActiveState, cache_key: str) -> dict[str, str]:
        components = json.loads(cache_key)
        values = {str(field): str(value) for field, value in components}
        values.pop("path", None)
        return values

    def _evict_victim(self, rows: Sequence[Mapping[str, Any]], rule: str) -> Mapping[str, Any]:
        if rule == "lru":
            return sorted(
                rows,
                key=lambda row: (
                    int(row["last_access_mono_ms"]),
                    int(row["written_mono_ms"]),
                    str(row["cache_key"]),
                ),
            )[0]
        if rule == "fifo":
            return sorted(
                rows,
                key=lambda row: (
                    int(row["written_mono_ms"]),
                    int(row["last_access_mono_ms"]),
                    str(row["cache_key"]),
                ),
            )[0]
        if rule == "ttl":
            return sorted(
                rows,
                key=lambda row: (
                    int(row["ttl_anchor_mono_ms"]) + int(row["remaining_ttl_ms"]),
                    int(row["written_mono_ms"]),
                    str(row["cache_key"]),
                ),
            )[0]
        raise StateError(f"unknown eviction rule {rule!r}", location="policy.eviction_rule")

    def cleanup_expired(self, expected_version: int | None = None) -> list[dict[str, Any]]:
        now = self.now_ms()
        removed: list[dict[str, Any]] = []
        with self.ledger.transaction() as connection:
            active = (
                self.ledger.assert_version_conn(connection, expected_version)
                if expected_version is not None
                else self.ledger.active_state_conn(connection)
            )
            rows = connection.execute(
                "SELECT * FROM cache_entries WHERE node_id = ? ORDER BY cache_key",
                (self.node_id,),
            ).fetchall()
            for row_raw in rows:
                row = dict(row_raw)
                if int(row["ledger_version"]) != active.ledger_version:
                    continue
                if self._remaining(row, now) > 0:
                    continue
                connection.execute(
                    "DELETE FROM cache_entries WHERE node_id = ? AND cache_key = ?",
                    (self.node_id, row["cache_key"]),
                )
                self.ledger.record_eviction_conn(
                    connection,
                    node_id=self.node_id,
                    cache_key=row["cache_key"],
                    path=row["path"],
                    etag=row["etag"],
                    reason="ttl_expired",
                    rule=None,
                    ledger_version=active.ledger_version,
                )
                self.ledger.record_cache_event_conn(
                    connection,
                    node_id=self.node_id,
                    cache_key=row["cache_key"],
                    path=row["path"],
                    event_type="evict",
                    ledger_version=active.ledger_version,
                    content_set_hash=active.content_set_hash,
                    entry_version=int(row["entry_version"]),
                    reason="ttl_expired",
                    before=row,
                    after=None,
                )
                removed.append(self._view(row, now))
        return removed

    def state(self, expected_version: int | None = None) -> dict[str, Any]:
        self.cleanup_expired(expected_version)
        active = (
            self.ledger.assert_version(expected_version)
            if expected_version is not None
            else self.ledger.active_state()
        )
        now = self.now_ms()
        rows = self.ledger.list_entries(self.node_id)
        entries = [self._view(row, now) for row in rows]
        intents = [
            item
            for item in self.ledger.list_write_intents(self.node_id)
            if item["status"] in ("pending", "unknown")
        ]
        return {
            "node_id": self.node_id,
            "ledger_version": active.ledger_version,
            "policy_version": active.policy_version,
            "content_set_hash": active.content_set_hash,
            "policy_hash": active.policy_hash,
            "content_versions": active.content.versions,
            "entries": entries,
            "entry_count": len(entries),
            "unknown_intents": intents,
            "evictions": self.ledger.list_evictions(self.node_id, limit=100),
        }

    def query(
        self,
        *,
        path: str,
        key_values: Mapping[str, Any] | None,
        expected_version: int,
    ) -> dict[str, Any]:
        now = self.now_ms()
        with self.ledger.transaction() as connection:
            active = self.ledger.assert_version_conn(connection, expected_version)
            if path not in active.content.effective:
                raise EdgeError(
                    "E_NOT_FOUND",
                    f"content path {path!r} does not exist",
                    location=f"request.path[{path!r}]",
                )
            cache_key, _ = self._cache_key(active, path, key_values)
            row_raw = connection.execute(
                "SELECT * FROM cache_entries WHERE node_id = ? AND cache_key = ?",
                (self.node_id, cache_key),
            ).fetchone()
            if row_raw is None:
                return {"hit": False, "reason": "miss", "cache_key": cache_key, "path": path}
            row = dict(row_raw)
            if (
                int(row["ledger_version"]) != active.ledger_version
                or row["content_set_hash"] != active.content_set_hash
                or row["policy_hash"] != active.policy_hash
            ):
                raise VersionError(
                    "cache entry belongs to a different policy/content ledger state",
                    location=f"cache_entries[{self.node_id},{cache_key}]",
                    entry_ledger_version=int(row["ledger_version"]),
                    active_ledger_version=active.ledger_version,
                    entry_content_set_hash=row["content_set_hash"],
                    active_content_set_hash=active.content_set_hash,
                    entry_policy_hash=row["policy_hash"],
                    active_policy_hash=active.policy_hash,
                )
            remaining = self._remaining(row, now)
            if remaining <= 0:
                connection.execute(
                    "DELETE FROM cache_entries WHERE node_id = ? AND cache_key = ?",
                    (self.node_id, cache_key),
                )
                self.ledger.record_eviction_conn(
                    connection,
                    node_id=self.node_id,
                    cache_key=cache_key,
                    path=path,
                    etag=row["etag"],
                    reason="ttl_expired",
                    rule=None,
                    ledger_version=active.ledger_version,
                )
                self.ledger.record_cache_event_conn(
                    connection,
                    node_id=self.node_id,
                    cache_key=cache_key,
                    path=path,
                    event_type="evict",
                    ledger_version=active.ledger_version,
                    content_set_hash=active.content_set_hash,
                    entry_version=int(row["entry_version"]),
                    reason="ttl_expired",
                    before=row,
                    after=None,
                )
                return {
                    "hit": False,
                    "reason": "expired",
                    "cache_key": cache_key,
                    "path": path,
                }
            before = self._view(row, now)
            connection.execute(
                "UPDATE cache_entries SET remaining_ttl_ms = ?, ttl_anchor_mono_ms = ?, "
                "last_access_mono_ms = ?, updated_at = ? WHERE node_id = ? AND cache_key = ?",
                (remaining, now, now, utc_now_text(), self.node_id, cache_key),
            )
            after = dict(before)
            after["remaining_ttl_ms"] = remaining
            after["last_access_mono_ms"] = now
            self.ledger.record_cache_event_conn(
                connection,
                node_id=self.node_id,
                cache_key=cache_key,
                path=path,
                event_type="hit",
                ledger_version=active.ledger_version,
                content_set_hash=active.content_set_hash,
                entry_version=int(row["entry_version"]),
                reason="fresh",
                before=before,
                after=after,
            )
            return {
                "hit": True,
                "reason": "fresh",
                "cache_key": cache_key,
                "entry": after,
            }

    def fill(
        self,
        *,
        path: str,
        key_values: Mapping[str, Any] | None,
        expected_version: int,
    ) -> dict[str, Any]:
        active = self.ledger.assert_version(expected_version)
        if path not in active.content.effective:
            raise EdgeError(
                "E_NOT_FOUND",
                f"content path {path!r} does not exist",
                location=f"request.path[{path!r}]",
            )
        cache_key, key_fields = self._cache_key(active, path, key_values)
        ttl_ms = active.policy.ttl_ms_for(path)
        source_result = self.source.fetch_content(path, expected_version)
        if source_result.get("etag") is None:
            raise ProtocolError("source response omitted ETag", location=f"content[{path!r}]")

        intent_id = self.ledger.begin_write_intent(
            node_id=self.node_id,
            cache_key=cache_key,
            path=path,
            ledger_version=expected_version,
            detail={"source_etag": source_result["etag"], "ttl_ms": ttl_ms},
        )
        committed = False
        try:
            now = self.now_ms()
            evicted: list[dict[str, Any]] = []
            with self.ledger.transaction() as connection:
                active_now = self.ledger.assert_version_conn(connection, expected_version)
                if source_result["etag"] != active_now.content.effective[path].etag:
                    raise VersionError(
                        "source content changed while filling cache",
                        location=f"content[{path!r}].etag",
                        fetched_etag=source_result["etag"],
                        active_etag=active_now.content.effective[path].etag,
                    )
                if source_result.get("content_set_hash") != active_now.content_set_hash:
                    raise VersionError(
                        "source content set changed while filling cache",
                        location="content_set_hash",
                        fetched_content_set_hash=source_result.get("content_set_hash"),
                        active_content_set_hash=active_now.content_set_hash,
                    )
                existing_raw = connection.execute(
                    "SELECT * FROM cache_entries WHERE node_id = ? AND cache_key = ?",
                    (self.node_id, cache_key),
                ).fetchone()
                before = dict(existing_raw) if existing_raw else None
                if existing_raw is not None:
                    connection.execute(
                        "DELETE FROM cache_entries WHERE node_id = ? AND cache_key = ?",
                        (self.node_id, cache_key),
                    )
                    self.ledger.record_cache_event_conn(
                        connection,
                        node_id=self.node_id,
                        cache_key=cache_key,
                        path=path,
                        event_type="replace",
                        ledger_version=active_now.ledger_version,
                        content_set_hash=active_now.content_set_hash,
                        entry_version=int(existing_raw["entry_version"]),
                        reason="fill_replace",
                        before=before,
                        after=None,
                    )
                entry_version = self.ledger.get_path_entry_version_conn(connection, path)
                connection.execute(
                    "INSERT INTO cache_entries(node_id, cache_key, path, key_fields_json, etag, "
                    "entry_version, written_mono_ms, ttl_ms, remaining_ttl_ms, ttl_anchor_mono_ms, "
                    "last_access_mono_ms, ledger_version, policy_hash, content_set_hash, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.node_id,
                        cache_key,
                        path,
                        canonical_json(key_fields),
                        source_result["etag"],
                        entry_version,
                        now,
                        ttl_ms,
                        ttl_ms,
                        now,
                        now,
                        active_now.ledger_version,
                        active_now.policy_hash,
                        active_now.content_set_hash,
                        utc_now_text(),
                    ),
                )
                rows = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM cache_entries WHERE node_id = ? ORDER BY cache_key",
                        (self.node_id,),
                    ).fetchall()
                ]
                while len(rows) > active_now.policy.node_capacity:
                    victim = self._evict_victim(rows, active_now.policy.eviction_rule)
                    connection.execute(
                        "DELETE FROM cache_entries WHERE node_id = ? AND cache_key = ?",
                        (self.node_id, victim["cache_key"]),
                    )
                    self.ledger.record_eviction_conn(
                        connection,
                        node_id=self.node_id,
                        cache_key=victim["cache_key"],
                        path=victim["path"],
                        etag=victim["etag"],
                        reason="capacity",
                        rule=active_now.policy.eviction_rule,
                        ledger_version=active_now.ledger_version,
                    )
                    self.ledger.record_cache_event_conn(
                        connection,
                        node_id=self.node_id,
                        cache_key=victim["cache_key"],
                        path=victim["path"],
                        event_type="evict",
                        ledger_version=active_now.ledger_version,
                        content_set_hash=active_now.content_set_hash,
                        entry_version=int(victim["entry_version"]),
                        reason=f"capacity:{active_now.policy.eviction_rule}",
                        before=victim,
                        after=None,
                    )
                    evicted.append(dict(victim))
                    rows = [row for row in rows if row["cache_key"] != victim["cache_key"]]
                inserted_raw = connection.execute(
                    "SELECT * FROM cache_entries WHERE node_id = ? AND cache_key = ?",
                    (self.node_id, cache_key),
                ).fetchone()
                inserted = dict(inserted_raw) if inserted_raw else None
                self.ledger.mark_write_intent_conn(
                    connection,
                    intent_id,
                    "committed",
                    {"cache_key": cache_key, "etag": source_result["etag"]},
                )
                self.ledger.record_cache_event_conn(
                    connection,
                    node_id=self.node_id,
                    cache_key=cache_key,
                    path=path,
                    event_type="write",
                    ledger_version=active_now.ledger_version,
                    content_set_hash=active_now.content_set_hash,
                    entry_version=entry_version,
                    reason="fill",
                    before=before,
                    after=inserted,
                )
                committed = True
            return {
                "node_id": self.node_id,
                "path": path,
                "cache_key": cache_key,
                "etag": source_result["etag"],
                "entry_version": entry_version,
                "intent_id": intent_id,
                "retained": inserted is not None,
                "entry": self._view(inserted, now) if inserted is not None else None,
                "evicted": [self._view(row, now) for row in evicted],
            }
        finally:
            if not committed:
                try:
                    self.ledger.fail_write_intent(
                        intent_id,
                        {"reason": "fill_failed_before_commit"},
                    )
                except Exception:
                    pass

    def fill_many(
        self,
        requests: Sequence[Mapping[str, Any]],
        expected_version: int,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for request in requests:
            path = request.get("path")
            if not isinstance(path, str):
                raise ProtocolError("fill request requires path", location="requests[].path")
            key_values = request.get("key_values", {})
            if not isinstance(key_values, Mapping):
                raise ProtocolError("key_values must be an object", location="requests[].key_values")
            results.append(
                self.fill(path=path, key_values=key_values, expected_version=expected_version)
            )
        return {
            "node_id": self.node_id,
            "ledger_version": expected_version,
            "filled": len(results),
            "results": results,
            "eviction_count": sum(len(item["evicted"]) for item in results),
        }

    def refresh_paths(
        self,
        *,
        trigger_path: str,
        paths: Sequence[str],
        batch_id: str,
        expected_version: int,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for path in paths:
            now = self.now_ms()
            specs: list[dict[str, str]] = []
            before_etag: str | None = None
            replaced = 0
            with self.ledger.transaction() as connection:
                active = self.ledger.assert_version_conn(connection, expected_version)
                if path not in active.content.effective:
                    raise StateError("propagation references unknown content path", path=path)
                rows = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM cache_entries WHERE node_id = ? AND path = ? ORDER BY cache_key",
                        (self.node_id, path),
                    ).fetchall()
                ]
                before_etag = rows[0]["etag"] if rows else None
                for row in rows:
                    values = self._values_from_cache_key(active, row["cache_key"])
                    specs.append({"path": path, "key_values": values})
                    connection.execute(
                        "DELETE FROM cache_entries WHERE node_id = ? AND cache_key = ?",
                        (self.node_id, row["cache_key"]),
                    )
                    self.ledger.record_eviction_conn(
                        connection,
                        node_id=self.node_id,
                        cache_key=row["cache_key"],
                        path=path,
                        etag=row["etag"],
                        reason="derived_invalidation",
                        rule=None,
                        ledger_version=active.ledger_version,
                    )
                    self.ledger.record_cache_event_conn(
                        connection,
                        node_id=self.node_id,
                        cache_key=row["cache_key"],
                        path=path,
                        event_type="invalidate",
                        ledger_version=active.ledger_version,
                        content_set_hash=active.content_set_hash,
                        entry_version=int(row["entry_version"]),
                        reason=f"propagation:{batch_id}",
                        before=row,
                        after=None,
                    )
                    replaced += 1
            filled: list[dict[str, Any]] = []
            for spec in specs:
                filled.append(
                    self.fill(
                        path=spec["path"],
                        key_values=spec["key_values"],
                        expected_version=expected_version,
                    )
                )
            after_etag = (
                filled[0]["etag"]
                if filled
                else self.ledger.assert_version(expected_version).content.effective[path].etag
            )
            entry_version = filled[0]["entry_version"] if filled else None
            results.append(
                {
                    "path": path,
                    "affected_path": path,
                    "trigger_path": trigger_path,
                    "before_etag": before_etag,
                    "after_etag": after_etag,
                    "entry_version": entry_version,
                    "replaced_entries": replaced,
                    "refilled_entries": len(filled),
                    "status": "replaced" if replaced else "no_entry",
                }
            )
            if self.logger:
                self.logger.info(
                    "path_invalidated",
                    batch_id=batch_id,
                    trigger_path=trigger_path,
                    affected_path=path,
                    before_etag=before_etag,
                    after_etag=after_etag,
                    replaced_entries=replaced,
                )
        return {
            "node_id": self.node_id,
            "batch_id": batch_id,
            "trigger_path": trigger_path,
            "ledger_version": expected_version,
            "ordered_paths": list(paths),
            "results": results,
            "replaced_entries": sum(item["replaced_entries"] for item in results),
        }

    def reset_for_policy(self, expected_version: int) -> dict[str, Any]:
        now = self.now_ms()
        removed: list[dict[str, Any]] = []
        with self.ledger.transaction() as connection:
            active = self.ledger.assert_version_conn(connection, expected_version)
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM cache_entries WHERE node_id = ? ORDER BY cache_key",
                    (self.node_id,),
                ).fetchall()
            ]
            for row in rows:
                connection.execute(
                    "DELETE FROM cache_entries WHERE node_id = ? AND cache_key = ?",
                    (self.node_id, row["cache_key"]),
                )
                self.ledger.record_eviction_conn(
                    connection,
                    node_id=self.node_id,
                    cache_key=row["cache_key"],
                    path=row["path"],
                    etag=row["etag"],
                    reason="policy_replace",
                    rule=active.policy.eviction_rule,
                    ledger_version=active.ledger_version,
                )
                self.ledger.record_cache_event_conn(
                    connection,
                    node_id=self.node_id,
                    cache_key=row["cache_key"],
                    path=row["path"],
                    event_type="evict",
                    ledger_version=active.ledger_version,
                    content_set_hash=active.content_set_hash,
                    entry_version=int(row["entry_version"]),
                    reason="policy_replace",
                    before=row,
                    after=None,
                )
                removed.append(self._view(row, now))
        return {
            "node_id": self.node_id,
            "ledger_version": expected_version,
            "removed_entries": len(removed),
            "removed": removed,
        }

    def simulate_orphan_write(
        self,
        *,
        path: str,
        key_values: Mapping[str, Any] | None,
        expected_version: int,
    ) -> dict[str, Any]:
        active = self.ledger.assert_version(expected_version)
        if path not in active.content.effective:
            raise EdgeError("E_NOT_FOUND", "unknown path", location=f"request.path[{path!r}]")
        cache_key, _ = self._cache_key(active, path, key_values)
        intent_id = self.ledger.begin_write_intent(
            node_id=self.node_id,
            cache_key=cache_key,
            path=path,
            ledger_version=expected_version,
            detail={"simulated": True, "purpose": "forced-termination recovery teaching"},
        )
        return {
            "node_id": self.node_id,
            "intent_id": intent_id,
            "status": "pending",
            "path": path,
            "cache_key": cache_key,
            "note": "the process must be force-killed before recovery can mark this unknown",
        }


class NodeRuntime:
    def __init__(
        self,
        ledger: Ledger,
        node_id: str,
        source_host: str,
        source_port: int,
        logger: JsonLogger,
    ) -> None:
        self.ledger = ledger
        self.node_id = node_id
        self.logger = logger
        self.started_at = utc_now_text()
        self.started_mono = time.monotonic()
        self.request_count = 0
        self.cache = NodeCache(
            ledger,
            node_id,
            SourceClient(source_host, source_port),
            logger=logger,
        )
        self.recovery_report = ledger.recover_node(node_id)

    def status(self) -> dict[str, Any]:
        active = self.ledger.active_state()
        return {
            "service": "edge-node",
            "node_id": self.node_id,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "uptime_seconds": round(time.monotonic() - self.started_mono, 3),
            "request_count": self.request_count,
            "ledger_version": active.ledger_version,
            "policy_version": active.policy_version,
            "content_set_hash": active.content_set_hash,
            "cache_entry_count": len(self.ledger.list_entries(self.node_id)),
            "recovery_report": self.recovery_report,
        }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local edge cache node (JSON-line/TCP)")
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--source-host", default="127.0.0.1")
    parser.add_argument("--source-port", type=int, required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--log", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logger = JsonLogger(args.log, f"node:{args.node_id}")
    server: JsonLineServer | None = None
    runtime: NodeRuntime | None = None
    try:
        ledger = Ledger(args.db)
        if not ledger.has_state():
            raise StateError("node cannot start before source ledger is initialized")

        def handle(request: dict[str, Any]) -> dict[str, Any] | RequestResult:
            if runtime is None:
                raise StateError("node runtime is still recovering")
            runtime.request_count += 1
            op = request.get("op")
            if op == "ping":
                return {"service": "edge-node", "node_id": args.node_id, "pid": os.getpid()}
            if op == "status":
                return runtime.status()
            if op == "state":
                version = request.get("ledger_version")
                return runtime.cache.state(version if isinstance(version, int) else None)
            if op == "query":
                path = request.get("path")
                version = request.get("ledger_version")
                key_values = request.get("key_values", {})
                if not isinstance(path, str):
                    raise ProtocolError("query requires string path", location="request.path")
                if not isinstance(version, int):
                    raise ProtocolError("query requires integer ledger_version", location="request.ledger_version")
                if not isinstance(key_values, Mapping):
                    raise ProtocolError("key_values must be an object", location="request.key_values")
                return runtime.cache.query(
                    path=path,
                    key_values=key_values,
                    expected_version=version,
                )
            if op == "fill":
                path = request.get("path")
                version = request.get("ledger_version")
                key_values = request.get("key_values", {})
                if not isinstance(path, str):
                    raise ProtocolError("fill requires string path", location="request.path")
                if not isinstance(version, int):
                    raise ProtocolError("fill requires integer ledger_version", location="request.ledger_version")
                if not isinstance(key_values, Mapping):
                    raise ProtocolError("key_values must be an object", location="request.key_values")
                result = runtime.cache.fill(
                    path=path,
                    key_values=key_values,
                    expected_version=version,
                )
                logger.info(
                    "cache_fill",
                    node_id=args.node_id,
                    ledger_version=version,
                    path=path,
                    etag=result["etag"],
                    entry_version=result["entry_version"],
                    evicted=[item["cache_key"] for item in result["evicted"]],
                )
                return result
            if op == "fill_many":
                version = request.get("ledger_version")
                requests = request.get("requests")
                if not isinstance(version, int):
                    raise ProtocolError("fill_many requires integer ledger_version", location="request.ledger_version")
                if not isinstance(requests, list):
                    raise ProtocolError("fill_many requires requests array", location="request.requests")
                result = runtime.cache.fill_many(requests, version)
                logger.info(
                    "cache_fill_many",
                    node_id=args.node_id,
                    ledger_version=version,
                    filled=result["filled"],
                    eviction_count=result["eviction_count"],
                )
                return result
            if op == "refresh":
                version = request.get("ledger_version")
                paths = request.get("paths")
                trigger_path = request.get("trigger_path")
                batch_id = request.get("batch_id")
                if not isinstance(version, int):
                    raise ProtocolError("refresh requires integer ledger_version", location="request.ledger_version")
                if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
                    raise ProtocolError("refresh requires string paths array", location="request.paths")
                if not isinstance(trigger_path, str) or not isinstance(batch_id, str):
                    raise ProtocolError(
                        "refresh requires trigger_path and batch_id",
                        location="request.trigger_path",
                    )
                result = runtime.cache.refresh_paths(
                    trigger_path=trigger_path,
                    paths=paths,
                    batch_id=batch_id,
                    expected_version=version,
                )
                logger.info(
                    "cache_refresh",
                    node_id=args.node_id,
                    ledger_version=version,
                    batch_id=batch_id,
                    trigger_path=trigger_path,
                    ordered_paths=paths,
                    replaced_entries=result["replaced_entries"],
                )
                return result
            if op == "reset_policy":
                version = request.get("ledger_version")
                if not isinstance(version, int):
                    raise ProtocolError(
                        "reset_policy requires integer ledger_version",
                        location="request.ledger_version",
                    )
                result = runtime.cache.reset_for_policy(version)
                logger.info(
                    "policy_cache_reset",
                    node_id=args.node_id,
                    ledger_version=version,
                    removed_entries=result["removed_entries"],
                )
                return result
            if op == "simulate_orphan_write":
                version = request.get("ledger_version")
                path = request.get("path")
                key_values = request.get("key_values", {})
                if not isinstance(version, int):
                    raise ProtocolError(
                        "simulate_orphan_write requires integer ledger_version",
                        location="request.ledger_version",
                    )
                if not isinstance(path, str):
                    raise ProtocolError("simulate_orphan_write requires path", location="request.path")
                if not isinstance(key_values, Mapping):
                    raise ProtocolError("key_values must be an object", location="request.key_values")
                result = runtime.cache.simulate_orphan_write(
                    path=path,
                    key_values=key_values,
                    expected_version=version,
                )
                logger.warning(
                    "simulated_orphan_write",
                    node_id=args.node_id,
                    intent_id=result["intent_id"],
                    path=path,
                )
                return result
            if op == "shutdown":
                logger.info(
                    "exit_report",
                    graceful=True,
                    service="edge-node",
                    node_id=args.node_id,
                    request_count=runtime.request_count,
                    cache_entry_count=len(ledger.list_entries(args.node_id)),
                    ledger_version=ledger.current_version(),
                )
                return RequestResult(
                    {"message": "node shutting down", "node_id": args.node_id, "graceful": True},
                    stop_after_response=True,
                )
            raise ProtocolError(
                f"unsupported node op {op!r}",
                location="request.op",
                supported=[
                    "ping",
                    "status",
                    "state",
                    "query",
                    "fill",
                    "fill_many",
                    "refresh",
                    "reset_policy",
                    "simulate_orphan_write",
                    "shutdown",
                ],
            )

        server = JsonLineServer(args.host, args.port, handle, logger, f"node:{args.node_id}")
        server.start()
        runtime = NodeRuntime(
            ledger,
            args.node_id,
            args.source_host,
            args.source_port,
            logger,
        )
        logger.info(
            "node_ready",
            node_id=args.node_id,
            ledger_version=ledger.current_version(),
            recovery_report=runtime.recovery_report,
        )
        server.serve_forever()
        return 0
    except EdgeError as exc:
        logger.error(
            "business_error",
            code=exc.code,
            message=exc.message,
            location=exc.location,
            details=exc.details,
        )
        emit_json_line({"ok": False, "component": "node", "node_id": args.node_id, "error": exc.to_dict()}, sys.stderr)
        if server is not None:
            server.request_stop()
        return exit_code_for(exc)
    except KeyboardInterrupt:
        logger.warning("interrupted", graceful=False)
        if server is not None:
            server.request_stop()
        return 130
    except Exception as exc:  # pragma: no cover - defensive process boundary
        logger.error(
            "internal_error",
            code="E_INTERNAL",
            message=str(exc),
            exception=exc.__class__.__name__,
        )
        emit_json_line(
            {"ok": False, "component": "node", "node_id": args.node_id, "error": error_payload(exc)},
            sys.stderr,
        )
        if server is not None:
            server.request_stop()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
