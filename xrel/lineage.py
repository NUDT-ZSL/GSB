"""Result cache keys, lineage records, stale detection and replay."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .canonical import content_checksum
from .config import AppConfig
from .errors import LineageBroken, StaleResult
from .snapshot import SnapshotRef, SnapshotStore, atomic_write


def build_cache_key(
    query_id: str,
    precision: str,
    bridge_ref: SnapshotRef,
    index_ref: SnapshotRef,
    config: AppConfig,
) -> str:
    payload = {
        "query_id": query_id,
        "precision": precision,
        "bridge_version": bridge_ref.version,
        "bridge_checksum": bridge_ref.checksum,
        "index_version": index_ref.version,
        "index_checksum": index_ref.checksum,
        "config": _config_fingerprint(config),
    }
    return content_checksum(payload)


def _config_fingerprint(config: AppConfig) -> dict:
    grades = [
        [grade, None if boundary == float("-inf") else boundary]
        for grade, boundary in config.threshold.grades
    ]
    return {
        "expand": config.expand.__dict__,
        "bm25": config.bm25.__dict__,
        "threshold": {
            "strategy": config.threshold.strategy,
            "tolerance": config.threshold.tolerance,
            "grades": grades,
        },
        "fixed": None
        if config.fixed is None
        else {"scale": config.fixed.scale, "rounding": config.fixed.rounding.value},
    }


@dataclass
class LineageRecord:
    query_id: str
    cache_key: str
    precision: str
    bridge_version: int
    bridge_checksum: str
    index_version: int
    index_checksum: str
    node_path: List[str]
    result_checksum: str
    result: Dict[str, Any]

    def to_dict(self) -> dict:
        return {
            "query_id": self.query_id,
            "cache_key": self.cache_key,
            "precision": self.precision,
            "bridge_version": self.bridge_version,
            "bridge_checksum": self.bridge_checksum,
            "index_version": self.index_version,
            "index_checksum": self.index_checksum,
            "node_path": list(self.node_path),
            "result_checksum": self.result_checksum,
            "result": self.result,
        }

    @staticmethod
    def from_dict(raw: dict) -> "LineageRecord":
        return LineageRecord(**raw)


class ResultStore:
    """Persists query result records keyed by cache key."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, cache_key: str) -> Path:
        return self.root / f"result_{cache_key}.json"

    def save(self, record: LineageRecord) -> None:
        atomic_write(self._path(record.cache_key), record.to_dict())

    def load(self, cache_key: str) -> Optional[LineageRecord]:
        path = self._path(cache_key)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return LineageRecord.from_dict(json.load(handle))

    def all_records(self) -> List[LineageRecord]:
        records = []
        for path in sorted(self.root.glob("result_*.json")):
            with open(path, "r", encoding="utf-8") as handle:
                records.append(LineageRecord.from_dict(json.load(handle)))
        return records


def check_freshness(
    record: LineageRecord, store: SnapshotStore
) -> None:
    """Raise E_STALE_RESULT if any dependency version moved on."""

    current_bridge = store.current(SnapshotStore.BRIDGE)
    current_index = store.current(SnapshotStore.INDEX)
    stale: Dict[str, Any] = {}
    if current_bridge is None:
        raise LineageBroken(record.query_id, "bridge snapshot ledger is missing")
    if current_index is None:
        raise LineageBroken(record.query_id, "index snapshot ledger is missing")
    if (
        current_bridge.version != record.bridge_version
        or current_bridge.checksum != record.bridge_checksum
    ):
        stale["bridge"] = {
            "recorded_version": record.bridge_version,
            "current_version": current_bridge.version,
            "recorded_checksum": record.bridge_checksum,
            "current_checksum": current_bridge.checksum,
        }
    if (
        current_index.version != record.index_version
        or current_index.checksum != record.index_checksum
    ):
        stale["index"] = {
            "recorded_version": record.index_version,
            "current_version": current_index.version,
            "recorded_checksum": record.index_checksum,
            "current_checksum": current_index.checksum,
        }
    if stale:
        raise StaleResult(record.query_id, stale)


def verify_lineage(record: LineageRecord) -> None:
    """Re-checksum the stored result and compare with the lineage checksum."""

    expected = content_checksum(record.result)
    if expected != record.result_checksum:
        raise LineageBroken(
            record.query_id,
            "stored result checksum does not match lineage record",
            recorded_checksum=record.result_checksum,
            recomputed_checksum=expected,
        )
    if not record.node_path:
        raise LineageBroken(record.query_id, "lineage node path is empty")
    if not record.cache_key:
        raise LineageBroken(record.query_id, "cache key missing from lineage")


def result_fingerprint(result: Dict[str, Any]) -> str:
    return content_checksum(result)
