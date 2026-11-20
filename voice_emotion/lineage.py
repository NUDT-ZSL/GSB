"""Immutable score lineage records and key-only historical replay."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .cache import CacheStore, ConfigStore
from .canonical import hash_value, read_json, write_json_atomic
from .config import AggregationConfig
from .errors import (
    E_CACHE_BUSY,
    E_INCONSISTENT,
    E_LINEAGE_BROKEN,
    BusinessError,
    raise_error,
)
from .regression import aggregate_segments


LINEAGE_SCHEMA = "vem.lineage-record.v1"


def make_lineage_id(
    dialog_id: str,
    kind: str,
    terminal_cache_key: str,
    segment_id: str | None = None,
    context_hash: str | None = None,
) -> str:
    return "lin-" + hash_value(
        {
            "dialog_id": dialog_id,
            "kind": kind,
            "segment_id": segment_id,
            "terminal_cache_key": terminal_cache_key,
            "context_hash": context_hash,
        }
    )[:32]


class LineageStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "lineage"
        self.records = self.root / "records"
        self.records.mkdir(parents=True, exist_ok=True)

    def record_path(self, record_id: str) -> Path:
        return self.records / record_id[:2] / f"{record_id}.json"

    def put(self, record: dict[str, Any]) -> dict[str, Any]:
        body = {key: value for key, value in record.items() if key != "checksum"}
        record_id = str(body.get("record_id", ""))
        if not record_id:
            raise_error(E_INCONSISTENT, "lineage record lacks record_id")
        stored = {**body, "checksum": hash_value(body)}
        path = self.record_path(record_id)
        existing = self.get_optional(record_id)
        if existing is not None:
            if existing["checksum"] != stored["checksum"]:
                raise_error(
                    E_INCONSISTENT,
                    "lineage record id was reused with different content",
                    record_id=record_id,
                )
            return existing
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            write_json_atomic(path, stored)
        except FileExistsError:
            raise_error(E_CACHE_BUSY, "lineage record was written concurrently", record_id=record_id)
        loaded = self.get(record_id)
        if loaded["checksum"] != stored["checksum"]:
            raise_error(E_INCONSISTENT, "lineage write verification failed", record_id=record_id)
        return loaded

    def get_optional(self, record_id: str) -> dict[str, Any] | None:
        path = self.record_path(record_id)
        if not path.exists():
            return None
        try:
            body = read_json(path)
        except Exception as exc:
            raise_error(
                E_INCONSISTENT,
                "lineage record cannot be parsed",
                record_id=record_id,
                path=str(path),
                reason=str(exc),
            )
        if not isinstance(body, dict):
            raise_error(E_INCONSISTENT, "lineage record is not an object", record_id=record_id)
        checksum = body.get("checksum")
        unsigned = {key: value for key, value in body.items() if key != "checksum"}
        if checksum != hash_value(unsigned):
            raise_error(
                E_INCONSISTENT,
                "lineage record checksum mismatch",
                record_id=record_id,
            )
        if body.get("schema") != LINEAGE_SCHEMA:
            raise_error(
                E_INCONSISTENT,
                "unexpected lineage schema",
                record_id=record_id,
                schema=body.get("schema"),
            )
        return body

    def get(self, record_id: str) -> dict[str, Any]:
        body = self.get_optional(record_id)
        if body is None:
            raise BusinessError(
                "E_LINEAGE_MISSING",
                "lineage record does not exist",
                record_id=record_id,
            )
        return body

    def list_for_dialog(self, dialog_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self.records.glob("*/*.json")):
            body = read_json(path)
            if not isinstance(body, dict):
                continue
            if body.get("dialog_id") == dialog_id:
                checksum = body.get("checksum")
                unsigned = {key: value for key, value in body.items() if key != "checksum"}
                if checksum == hash_value(unsigned):
                    records.append(body)
        return sorted(records, key=lambda item: str(item["record_id"]))


def _payload_output_hash(payload: dict[str, Any]) -> str | None:
    value = payload.get("output_hash")
    return str(value) if value is not None else None


def _broken(
    record_id: str,
    item: dict[str, Any],
    reason: str,
    *,
    cause: dict[str, Any] | None = None,
) -> None:
    raise_error(
        E_LINEAGE_BROKEN,
        "historical lineage cannot be replayed",
        record_id=record_id,
        broken_node=item.get("node", "unknown"),
        segment_id=item.get("segment_id"),
        version=item.get("version"),
        cache_key=item.get("cache_key"),
        reason=reason,
        cause=cause,
    )


def replay_lineage(
    record: dict[str, Any],
    cache: CacheStore,
    config_store: ConfigStore,
) -> dict[str, Any]:
    """Reconstruct a historical score exclusively from recorded cache keys."""

    record_id = str(record["record_id"])
    try:
        config_snapshot = config_store.load(int(record["config_version"]))
    except BusinessError as exc:
        raise_error(
            E_LINEAGE_BROKEN,
            "historical config snapshot cannot be loaded",
            record_id=record_id,
            broken_node="config",
            version=record.get("config_version"),
            reason=str(exc),
            cause=exc.to_dict(),
        )
    if config_snapshot["checksum"] != record.get("config_checksum"):
        raise_error(
            E_LINEAGE_BROKEN,
            "historical config checksum differs from lineage",
            record_id=record_id,
            broken_node="config",
            version=record.get("config_version"),
        )
    path_items = list(record.get("node_path", []))
    if not path_items:
        raise_error(
            E_LINEAGE_BROKEN,
            "lineage record has an empty node path",
            record_id=record_id,
            broken_node="config",
            version=record.get("config_version"),
        )
    loaded: dict[str, Any] = {}
    for item in path_items:
        cache_key = str(item.get("cache_key"))
        try:
            entry = cache.get(cache_key)
        except BusinessError as exc:
            _broken(record_id, item, "cache key is missing or unreadable", cause=exc.to_dict())
        if entry is None:
            _broken(record_id, item, "cache key is missing")
        if entry.node != item.get("node"):
            _broken(record_id, item, "cache entry node differs from lineage")
        if int(entry.version) != int(item.get("version", -1)):
            _broken(record_id, item, "cache entry version differs from lineage")
        actual_hash = _payload_output_hash(entry.payload)
        if actual_hash is None or actual_hash != item.get("output_hash"):
            _broken(record_id, item, "cache payload output hash differs from lineage")
        loaded[cache_key] = entry
        for parent_key in item.get("parents", []):
            if parent_key not in loaded:
                _broken(record_id, item, "lineage parent is missing or out of order")
            parent = loaded[parent_key]
            parent_hash = _payload_output_hash(parent.payload)
            if parent_hash is None:
                _broken(record_id, parent, "lineage parent has no output hash")
            if item["node"] == "assignment":
                if entry.key_components.get("upstream_cache_key") != parent_key:
                    _broken(record_id, item, "assignment upstream key mismatch")
                if entry.key_components.get("upstream_hash") != parent_hash:
                    _broken(record_id, item, "assignment upstream hash mismatch")
            elif item["node"] == "segment":
                if entry.key_components.get("upstream_cache_key") != parent_key:
                    _broken(record_id, item, "segment upstream key mismatch")
                if entry.key_components.get("upstream_hash") != parent_hash:
                    _broken(record_id, item, "segment upstream hash mismatch")
            elif item["node"] == "dialog":
                expected_keys = entry.key_components.get("segment_cache_keys", [])
                expected_hashes = entry.key_components.get("segment_hashes", [])
                if parent_key not in expected_keys:
                    _broken(record_id, item, "dialog segment key missing from key components")
                index = expected_keys.index(parent_key)
                if index >= len(expected_hashes) or expected_hashes[index] != parent_hash:
                    _broken(record_id, item, "dialog segment hash mismatch")
    terminal = path_items[-1]
    terminal_entry = loaded[str(terminal["cache_key"])]
    kind = str(record.get("kind"))
    if kind == "segment":
        if terminal_entry.node != "segment":
            _broken(record_id, terminal, "terminal node is not a segment")
        replayed = terminal_entry.payload.get("segment")
        if replayed != record.get("score"):
            _broken(record_id, terminal, "replayed segment score differs from lineage")
    elif kind == "dialog":
        if terminal_entry.node != "dialog":
            _broken(record_id, terminal, "terminal node is not a dialog aggregate")
        segment_entries = [
            loaded[str(item["cache_key"])]
            for item in path_items
            if item.get("node") == "segment"
        ]
        segment_details = [entry.payload["segment"] for entry in segment_entries]
        try:
            aggregation_config = AggregationConfig(**terminal_entry.payload["aggregation_config"])
            recomputed = aggregate_segments(segment_details, aggregation_config)
        except BusinessError as exc:
            _broken(record_id, terminal, "dialog aggregate replay failed", cause=exc.to_dict())
        except Exception as exc:  # pragma: no cover - defensive audit boundary
            _broken(record_id, terminal, "dialog aggregate replay raised", cause={"error": str(exc)})
        if recomputed.get("output_hash") != terminal_entry.payload.get("output_hash"):
            _broken(record_id, terminal, "recomputed dialog aggregate hash differs")
        if recomputed.get("scores") != terminal_entry.payload.get("scores"):
            _broken(record_id, terminal, "recomputed dialog scores differ")
        replayed = recomputed["scores"]
        if replayed != record.get("score"):
            _broken(record_id, terminal, "replayed dialog score differs from lineage")
    else:
        raise_error(
            E_LINEAGE_BROKEN,
            "unsupported lineage replay kind",
            record_id=record_id,
            broken_node=terminal.get("node"),
            version=terminal.get("version"),
            kind=kind,
        )
    return {
        "record_id": record_id,
        "dialog_id": record.get("dialog_id"),
        "kind": kind,
        "replayed": replayed,
        "matches_recorded": True,
        "config_version": record.get("config_version"),
        "config_checksum": record.get("config_checksum"),
        "input_versions": record.get("input_versions", {}),
        "node_path": path_items,
    }
