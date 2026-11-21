"""Replay records and lineage verification for exported interval manifests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .cache import Artifact, SnapshotStore
from .config import AppConfig
from .errors import fail
from .nodes.export import compute_export
from .util import (
    read_json,
    sha256_bytes,
    sha256_value,
    write_bytes_atomic,
    write_json_atomic,
)


NODE_ORDER = ("envelope", "vad", "refine", "speaker", "export")


@dataclass(frozen=True)
class ReplayResult:
    cache_key: str
    snapshot_version: int
    items: list[dict[str, Any]]
    jsonl_bytes: bytes
    record: dict[str, Any]


class ReplayStore:
    def __init__(self, root: str | Path, snapshots: SnapshotStore):
        self.root = Path(root)
        self.snapshots = snapshots
        self.records_dir = self.root / "records"
        self.exports_dir = self.root / "exports"
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)

    def _record_path(self, cache_key: str) -> Path:
        return self.records_dir / f"{cache_key}.json"

    def _export_path(self, cache_key: str) -> Path:
        return self.exports_dir / f"{cache_key}.jsonl"

    def register(
        self,
        snapshot_version: int,
        artifacts: dict[str, Artifact],
        config: AppConfig,
        annotation_digest: str,
        annotation_revision: int,
        clips: dict[str, Any],
    ) -> dict[str, Any]:
        for node in NODE_ORDER:
            if node not in artifacts:
                fail(
                    "E_LINEAGE_BROKEN",
                    "cannot register an export with incomplete DAG lineage",
                    entry=node,
                    version=snapshot_version,
                )
        export_artifact = artifacts["export"]
        cache_key = export_artifact.cache_key
        jsonl_bytes = export_artifact.payload["jsonl"].encode("utf-8")
        body = {
            "schema_version": 1,
            "export_cache_key": cache_key,
            "snapshot_version": snapshot_version,
            "jsonl_sha256": sha256_bytes(jsonl_bytes),
            "item_count": int(export_artifact.payload["item_count"]),
            "lineage": {
                node: {
                    "version": snapshot_version,
                    "cache_key": artifacts[node].cache_key,
                    "content_hash": artifacts[node].content_hash,
                    "node_version": artifacts[node].node_version,
                    "upstream_hash": artifacts[node].upstream_hash,
                }
                for node in NODE_ORDER
            },
            "config_versions": {
                "config_version": config.config_version,
                "frame_ms": config.frame_ms,
                "frame_hop_ms": config.frame_hop_ms,
                "threshold_version": config.vad.threshold_version,
                "annotation_revision": annotation_revision,
                "annotation_hash": annotation_digest,
                "export_schema": config.export.schema_version,
            },
            "export_config": {
                "schema_version": config.export.schema_version,
                "speaker_labels": list(config.speaker.labels),
            },
            "clip_durations_ms": {
                clip_id: float(clip.duration_ms) for clip_id, clip in clips.items()
            },
        }
        record = dict(body)
        record["record_checksum"] = sha256_value(body)
        record_path = self._record_path(cache_key)
        export_path = self._export_path(cache_key)
        if record_path.exists() or export_path.exists():
            existing = self.load_record(cache_key)
            existing_export = export_path.read_bytes() if export_path.exists() else b""
            if existing_export != jsonl_bytes:
                fail(
                    "E_CACHE_CORRUPT",
                    "replay record already exists with different content",
                    entry=cache_key,
                    version=snapshot_version,
                )
            return existing
        write_bytes_atomic(export_path, jsonl_bytes)
        write_json_atomic(record_path, record)
        return record

    def load_record(self, cache_key: str) -> dict[str, Any]:
        path = self._record_path(cache_key)
        try:
            record = read_json(path)
        except FileNotFoundError:
            fail(
                "E_LINEAGE_BROKEN",
                "replay record does not exist",
                entry=cache_key,
                version=None,
            )
        except ValueError as exc:
            fail(
                "E_CACHE_CORRUPT",
                "replay record is not valid JSON",
                entry=cache_key,
                version=None,
                error=str(exc),
            )
        checksum = record.get("record_checksum")
        body = {key: value for key, value in record.items() if key != "record_checksum"}
        if checksum != sha256_value(body):
            fail(
                "E_CACHE_CORRUPT",
                "replay record checksum mismatch",
                entry=cache_key,
                version=record.get("snapshot_version"),
            )
        return record

    def list_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self.records_dir.glob("*.json")):
            records.append(self.load_record(path.stem))
        records.sort(
            key=lambda item: (int(item.get("snapshot_version", 0)), item["export_cache_key"])
        )
        return records

    def latest_record(self) -> dict[str, Any] | None:
        records = self.list_records()
        return records[-1] if records else None

    def _lineage_artifact(
        self,
        record: dict[str, Any],
        node: str,
        previous: Artifact | None,
    ) -> Artifact:
        lineage = record.get("lineage", {})
        if node not in lineage:
            fail(
                "E_LINEAGE_BROKEN",
                "lineage is missing a DAG node",
                entry=node,
                version=record.get("snapshot_version"),
            )
        node_lineage = lineage[node]
        required = ("version", "cache_key", "content_hash", "node_version")
        if any(field not in node_lineage for field in required):
            fail(
                "E_LINEAGE_BROKEN",
                "lineage node has incomplete metadata",
                entry=node,
                version=record.get("snapshot_version"),
            )
        version = int(node_lineage["version"])
        cache_key = str(node_lineage["cache_key"])
        if not self.snapshots.entry_exists(version, cache_key):
            fail(
                "E_LINEAGE_BROKEN",
                "lineage snapshot entry is missing",
                entry=cache_key,
                version=version,
                node=node,
            )
        if not self.snapshots.entry_file_exists(version, cache_key):
            fail(
                "E_LINEAGE_BROKEN",
                "lineage snapshot entry file is missing",
                entry=cache_key,
                version=version,
                node=node,
            )
        artifact = self.snapshots.load_entry(cache_key, version=version)
        if artifact is None:
            fail(
                "E_LINEAGE_BROKEN",
                "lineage snapshot entry cannot be loaded",
                entry=cache_key,
                version=version,
                node=node,
            )
        if artifact.node != node:
            fail(
                "E_LINEAGE_BROKEN",
                "lineage node name does not match snapshot entry",
                entry=cache_key,
                version=version,
                node=node,
            )
        if artifact.content_hash != node_lineage["content_hash"]:
            fail(
                "E_LINEAGE_BROKEN",
                "lineage content hash does not match snapshot entry",
                entry=cache_key,
                version=version,
                node=node,
            )
        if artifact.node_version != node_lineage["node_version"]:
            fail(
                "E_LINEAGE_BROKEN",
                "lineage node version does not match snapshot entry",
                entry=cache_key,
                version=version,
                node=node,
            )
        expected_upstream = previous.content_hash if previous is not None else None
        if artifact.upstream_hash != expected_upstream:
            fail(
                "E_LINEAGE_BROKEN",
                "DAG upstream hash link is broken",
                entry=cache_key,
                version=version,
                node=node,
                expected_upstream_hash=expected_upstream,
                actual_upstream_hash=artifact.upstream_hash,
            )
        return artifact

    def replay(self, cache_key: str) -> ReplayResult:
        record = self.load_record(cache_key)
        if record.get("export_cache_key") != cache_key:
            fail(
                "E_LINEAGE_BROKEN",
                "replay record export key does not match its filename",
                entry=cache_key,
                version=record.get("snapshot_version"),
            )
        previous: Artifact | None = None
        loaded: dict[str, Artifact] = {}
        for node in NODE_ORDER:
            artifact = self._lineage_artifact(record, node, previous)
            loaded[node] = artifact
            previous = artifact
        export_artifact = loaded["export"]
        if export_artifact.cache_key != cache_key:
            fail(
                "E_LINEAGE_BROKEN",
                "export lineage cache key does not match the replay record",
                entry=cache_key,
                version=record.get("snapshot_version"),
            )
        export_path = self._export_path(cache_key)
        try:
            stored_jsonl = export_path.read_bytes()
        except FileNotFoundError:
            fail(
                "E_LINEAGE_BROKEN",
                "replay export payload is missing",
                entry=cache_key,
                version=record.get("snapshot_version"),
            )
        expected_sha = str(record["jsonl_sha256"])
        if sha256_bytes(stored_jsonl) != expected_sha:
            fail(
                "E_LINEAGE_BROKEN",
                "replay JSONL checksum does not match the replay record",
                entry=cache_key,
                version=record.get("snapshot_version"),
            )
        if export_artifact.payload.get("jsonl_sha256") != expected_sha:
            fail(
                "E_LINEAGE_BROKEN",
                "export snapshot payload hash does not match the replay record",
                entry=cache_key,
                version=record.get("snapshot_version"),
            )

        export_config = record.get("export_config", {})
        labels = tuple(export_config.get("speaker_labels", []))
        config_adapter = SimpleNamespace(
            speaker=SimpleNamespace(labels=labels),
            export=SimpleNamespace(schema_version=export_config.get("schema_version")),
        )
        clip_adapter = {
            clip_id: SimpleNamespace(duration_ms=float(duration_ms))
            for clip_id, duration_ms in record.get("clip_durations_ms", {}).items()
        }
        rebuilt = compute_export(
            loaded["speaker"].payload, clip_adapter, config_adapter
        )
        rebuilt_bytes = rebuilt["jsonl"].encode("utf-8")
        if rebuilt_bytes != stored_jsonl:
            fail(
                "E_LINEAGE_BROKEN",
                "replayed export bytes differ from the recorded export",
                entry=cache_key,
                version=record.get("snapshot_version"),
                first_difference=self._first_byte_difference(stored_jsonl, rebuilt_bytes),
            )
        stored_items = self._parse_jsonl(stored_jsonl)
        if stored_items != rebuilt["items"]:
            index = self._first_item_difference(stored_items, rebuilt["items"])
            fail(
                "E_LINEAGE_BROKEN",
                "replayed export items differ from the recorded export",
                entry=cache_key,
                version=record.get("snapshot_version"),
                first_difference_index=index,
                recorded_item=stored_items[index] if index < len(stored_items) else None,
                replayed_item=rebuilt["items"][index] if index < len(rebuilt["items"]) else None,
            )
        return ReplayResult(
            cache_key=cache_key,
            snapshot_version=int(record["snapshot_version"]),
            items=stored_items,
            jsonl_bytes=stored_jsonl,
            record=record,
        )

    @staticmethod
    def _parse_jsonl(data: bytes) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for line in data.decode("utf-8").splitlines():
            if line:
                items.append(json.loads(line))
        return items

    @staticmethod
    def _first_byte_difference(left: bytes, right: bytes) -> int:
        for index, (left_byte, right_byte) in enumerate(zip(left, right)):
            if left_byte != right_byte:
                return index
        return min(len(left), len(right))

    @staticmethod
    def _first_item_difference(
        left: list[dict[str, Any]], right: list[dict[str, Any]]
    ) -> int:
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            if left_item != right_item:
                return index
        return min(len(left), len(right))
