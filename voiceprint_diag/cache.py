"""Content-addressed, atomically written stage artifact cache."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .canonical import canonical_json_bytes, read_json, sha256_obj, write_json_atomic


CACHE_SCHEMA = "cache/v1"


@dataclass
class CacheRecord:
    key: str
    kind: str
    label: str
    payload: dict[str, Any]
    payload_sha256: str
    path: Path


class CacheStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.records: dict[str, CacheRecord] = {}
        self.started_keys: set[str] = set()
        self.memo: dict[str, dict[str, Any]] = {}
        self.access_order: list[dict[str, Any]] = []
        self.required_labels: dict[str, str] = {}
        self._scan()
        self.started_keys = set(self.records)

    def _scan(self) -> None:
        for path in sorted(self.root.glob("*/*.json")):
            try:
                wrapper = read_json(path)
                if not isinstance(wrapper, dict) or wrapper.get("cache_schema") != CACHE_SCHEMA:
                    continue
                key = wrapper.get("key")
                kind = wrapper.get("kind")
                label = wrapper.get("label")
                payload = wrapper.get("payload")
                payload_sha = wrapper.get("payload_sha256")
                if not all(isinstance(item, str) for item in (key, kind, label, payload_sha)):
                    continue
                if not isinstance(payload, dict):
                    continue
                if sha256_obj(payload) != payload_sha:
                    continue
                if path.stem != key:
                    continue
                self.records[key] = CacheRecord(key, kind, label, payload, payload_sha, path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue

    def _label_for(self, key: str, fallback: str) -> str:
        record = self.records.get(key)
        return record.label if record is not None else fallback

    def has(self, key: str) -> bool:
        return key in self.records

    def get_or_compute(
        self,
        *,
        kind: str,
        key: str,
        label: str,
        producer: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        self.required_labels[key] = label
        if key in self.memo:
            return self.memo[key]
        record = self.records.get(key)
        if record is not None:
            reused = key in self.started_keys
            self.memo[key] = record.payload
            self.access_order.append(
                {
                    "key": key,
                    "kind": kind,
                    "label": record.label if reused else label,
                    "action": "reuse" if reused else "recompute",
                    "payload_sha256": record.payload_sha256,
                }
            )
            return record.payload
        payload = producer()
        if not isinstance(payload, dict):
            raise TypeError("cache producer must return a JSON object")
        payload_sha = sha256_obj(payload)
        path = self.root / kind / f"{key}.json"
        wrapper = {
            "cache_schema": CACHE_SCHEMA,
            "key": key,
            "kind": kind,
            "label": label,
            "payload_sha256": payload_sha,
            "payload": payload,
        }
        write_json_atomic(path, wrapper, pretty=False)
        record = CacheRecord(key, kind, label, payload, payload_sha, path)
        self.records[key] = record
        self.memo[key] = payload
        self.access_order.append(
            {
                "key": key,
                "kind": kind,
                "label": label,
                "action": "recompute",
                "payload_sha256": payload_sha,
            }
        )
        return payload

    def store(
        self,
        *,
        kind: str,
        key: str,
        label: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return self.get_or_compute(kind=kind, key=key, label=label, producer=lambda: payload)

    def invalidated(self, keep_kinds: set[str] | None = None) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for key in sorted(self.started_keys - set(self.required_labels)):
            record = self.records.get(key)
            if record is None:
                continue
            if keep_kinds is not None and record.kind not in keep_kinds:
                continue
            if key in self.started_keys:
                output.append(
                    {
                        "key": key,
                        "kind": record.kind,
                        "label": record.label,
                        "payload_sha256": record.payload_sha256,
                        "reason": "cache_key_not_required_by_current_manifest_or_config",
                    }
                )
        return output

    def reuse_records(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.access_order if item["action"] == "reuse"]

    def recompute_records(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.access_order if item["action"] == "recompute"]

    def stage_products(self, keys: list[str]) -> dict[str, dict[str, str]]:
        products: dict[str, dict[str, str]] = {}
        for key in keys:
            record = self.records.get(key)
            if record is None:
                continue
            products[key] = {
                "label": record.label,
                "kind": record.kind,
                "payload_sha256": record.payload_sha256,
            }
        return products

    def product_versions(self) -> dict[str, str]:
        return {
            key: record.payload_sha256
            for key, record in sorted(self.records.items())
            if key in self.required_labels
        }


STAGE_ORDER = {
    "audio": 0,
    "feature": 1,
    "features": 1,
    "feature_failure": 1,
    "aggregation": 2,
    "aggregation_failure": 2,
    "embedding": 3,
    "similarity": 4,
    "similarity_failure": 4,
    "report": 5,
}


def action_sort_key(item: dict[str, Any]) -> tuple[int, str, str, str]:
    return (
        STAGE_ORDER.get(str(item.get("kind", "")), 99),
        str(item.get("label", "")),
        str(item.get("key", "")),
        str(item.get("action", "")),
    )
