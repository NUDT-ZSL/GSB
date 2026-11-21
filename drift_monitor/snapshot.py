"""Versioned, checksummed configuration snapshots and alert artifacts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .errors import DriftError, ErrorCode
from .hashing import content_hash


def _write_json(path: str, document: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, sort_keys=True, indent=2, ensure_ascii=False)
        handle.write("\n")


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


class SnapshotStore:
    """Stores configuration snapshots and enforces monotonic versioning."""

    def __init__(self, directory: str):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.index_path = os.path.join(directory, "snapshot_index.json")

    def _load_index(self) -> Dict[str, Any]:
        if not os.path.exists(self.index_path):
            return {"latest_version": 0, "snapshots": []}
        return _read_json(self.index_path)

    def replace(self, config_dict: Dict[str, Any]) -> Dict[str, Any]:
        index = self._load_index()
        checksum = content_hash(config_dict)

        # Whole-replacement semantics: identical content is a no-op; any new
        # content must arrive with a strictly greater version number.
        for existing in index["snapshots"]:
            if existing["checksum"] == checksum:
                return existing

        next_version = index["latest_version"] + 1
        snapshot = {
            "snapshot_version": next_version,
            "checksum": checksum,
            "config": config_dict,
        }
        filename = f"config_snapshot_v{next_version:06d}.json"
        _write_json(os.path.join(self.directory, filename), snapshot)
        index["latest_version"] = next_version
        index["snapshots"].append(
            {"snapshot_version": next_version, "checksum": checksum, "file": filename}
        )
        _write_json(self.index_path, index)
        return snapshot

    def attempt_rollback(self, target_version: int) -> None:
        index = self._load_index()
        if target_version <= index["latest_version"]:
            raise DriftError(
                ErrorCode.SNAPSHOT_ROLLBACK,
                "an older snapshot version cannot replace the current one",
                {
                    "field": "snapshot_version",
                    "target_version": target_version,
                    "current_version": index["latest_version"],
                },
            )

    def latest(self) -> Optional[Dict[str, Any]]:
        index = self._load_index()
        if not index["snapshots"]:
            return None
        entry = index["snapshots"][-1]
        return _read_json(os.path.join(self.directory, entry["file"]))


@dataclass
class AlertRecord:
    alert_id: str
    alert_version: int
    config_snapshot_version: int
    config_checksum: str
    cache_key: str
    window_start: int
    window_end: int
    batch_id: int
    feature: str
    severity: str
    drift_amount: float
    threshold: float
    method: str
    checksum: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "alert_version": self.alert_version,
            "config_snapshot_version": self.config_snapshot_version,
            "config_checksum": self.config_checksum,
            "cache_key": self.cache_key,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "batch_id": self.batch_id,
            "feature": self.feature,
            "severity": self.severity,
            "drift_amount": self.drift_amount,
            "threshold": self.threshold,
            "method": self.method,
            "checksum": self.checksum,
        }


class AlertStore:
    def __init__(self, directory: str, max_alerts: Optional[int] = None):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self.index_path = os.path.join(directory, "alert_index.json")
        self.max_alerts = max_alerts

    def _load_index(self) -> Dict[str, Any]:
        if not os.path.exists(self.index_path):
            return {"latest_version": 0, "alerts": []}
        return _read_json(self.index_path)

    def publish(
        self,
        *,
        feature_result: Dict[str, Any],
        batch_id: int,
        cache_key: str,
        snapshot: Dict[str, Any],
    ) -> AlertRecord:
        index = self._load_index()
        if self.max_alerts is not None and len(index["alerts"]) >= self.max_alerts:
            raise DriftError(
                ErrorCode.RESOURCE_LIMIT,
                "stored alert limit reached",
                {"field": "alert_entries", "limit": self.max_alerts},
            )
        next_version = index["latest_version"] + 1
        body = {
            "alert_version": next_version,
            "config_snapshot_version": snapshot["snapshot_version"],
            "config_checksum": snapshot["checksum"],
            "cache_key": cache_key,
            "window_start": feature_result["window_start"],
            "window_end": feature_result["window_end"],
            "batch_id": batch_id,
            "feature": feature_result["feature"],
            "severity": feature_result["severity"],
            "drift_amount": feature_result["drift_amount"],
            "threshold": feature_result["threshold"],
            "method": feature_result["method"],
        }
        checksum = content_hash(body)
        record = AlertRecord(alert_id=f"alert-{next_version:06d}", checksum=checksum, **body)
        filename = f"{record.alert_id}.json"
        _write_json(os.path.join(self.directory, filename), record.to_dict())
        index["latest_version"] = next_version
        index["alerts"].append({"alert_id": record.alert_id, "file": filename, "checksum": checksum})
        _write_json(self.index_path, index)
        return record

    def all_alerts(self) -> List[Dict[str, Any]]:
        index = self._load_index()
        return [_read_json(os.path.join(self.directory, entry["file"])) for entry in index["alerts"]]

    def get(self, alert_id: str) -> Optional[Dict[str, Any]]:
        for alert in self.all_alerts():
            if alert["alert_id"] == alert_id:
                return alert
        return None

    def assert_fresh(
        self,
        alert: Dict[str, Any],
        current_snapshot: Dict[str, Any],
        current_cache_key: str,
        current_window: List[int],
    ) -> None:
        """Reject alerts whose configuration, cache key or window no longer match."""
        stale_reasons: List[str] = []
        if alert["config_snapshot_version"] != current_snapshot["snapshot_version"]:
            stale_reasons.append("config_snapshot_version")
        if alert["config_checksum"] != current_snapshot["checksum"]:
            stale_reasons.append("config_checksum")
        if alert["cache_key"] != current_cache_key:
            stale_reasons.append("cache_key")
        window = [alert["window_start"], alert["window_end"]]
        if window != current_window[:2]:
            stale_reasons.append("window_range")
        if stale_reasons:
            raise DriftError(
                ErrorCode.ALERT_STALE,
                "alert references dependencies that have since changed",
                {
                    "alert_id": alert["alert_id"],
                    "field": ",".join(stale_reasons),
                },
            )
