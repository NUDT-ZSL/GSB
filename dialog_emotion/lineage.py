"""Persistent score lineage and cache-key replay."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .cache import ContentAddressedCache
from .canonical import canonical_bytes, content_hash
from .errors import fail
from .scoring import aggregate_dialog, score_segment

LINEAGE_SCHEMA = "dialog-emotion-lineage-v1"


class LineageStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.records_dir = self.root / "records"
        self.records_dir.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        kind: str,
        dialog_id: str,
        node_path: list[str],
        cache_keys: dict[str, str],
        input_versions: dict[str, Any],
        score: dict[str, Any],
        segment_index: int | None = None,
        segment_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        record_id = content_hash(
            {
                "schema": LINEAGE_SCHEMA,
                "kind": kind,
                "dialog_id": dialog_id,
                "segment_index": segment_index,
                "node_path": node_path,
                "cache_keys": cache_keys,
                "input_versions": input_versions,
            }
        )
        body = {
            "schema": LINEAGE_SCHEMA,
            "record_id": record_id,
            "kind": kind,
            "dialog_id": dialog_id,
            "segment_index": segment_index,
            "node_path": node_path,
            "cache_keys": cache_keys,
            "segment_keys": segment_keys or [],
            "input_versions": input_versions,
            "score": score,
        }
        record = {**body, "checksum": content_hash(body)}
        path = self.records_dir / f"{record_id}.json"
        if path.exists():
            existing = self.get(record_id)
            if existing["checksum"] != record["checksum"]:
                fail(
                    "E_LINEAGE_BROKEN",
                    "lineage record id collision",
                    missing_key_level="lineage_record",
                    record_id=record_id,
                )
            return existing
        tmp = self.records_dir / f".{record_id}.tmp"
        tmp.write_bytes(canonical_bytes(record) + b"\n")
        tmp.replace(path)
        return record

    def get(self, record_id: str) -> dict[str, Any]:
        path = self.records_dir / f"{record_id}.json"
        if not path.exists():
            fail(
                "E_LINEAGE_BROKEN",
                "lineage record is missing",
                missing_key_level="lineage_record",
                record_id=record_id,
            )
        try:
            record = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            fail(
                "E_LINEAGE_BROKEN",
                "lineage record cannot be decoded",
                missing_key_level="lineage_record",
                record_id=record_id,
                error=str(exc),
            )
        checksum = record.get("checksum")
        body = {key: value for key, value in record.items() if key != "checksum"}
        if checksum != content_hash(body) or record.get("schema") != LINEAGE_SCHEMA:
            fail(
                "E_LINEAGE_BROKEN",
                "lineage record checksum mismatch",
                missing_key_level="lineage_record",
                record_id=record_id,
            )
        return record

    def replay(
        self,
        record_id: str,
        cache: ContentAddressedCache,
    ) -> dict[str, Any]:
        record = self.get(record_id)
        loaded: dict[str, dict[str, Any]] = {}
        for level, key in record["cache_keys"].items():
            entry = cache.get(key)
            if entry is None:
                fail(
                    "E_LINEAGE_BROKEN",
                    "cache key required by lineage is missing",
                    missing_key_level=level,
                    key=key,
                    record_id=record_id,
                )
            loaded[level] = entry

        if record["kind"] == "segment_score":
            replayed = self._replay_segment(record, loaded, cache)
        elif record["kind"] == "dialog_score":
            replayed = self._replay_dialog(record, loaded, cache)
        else:
            replayed = {"score": record["score"], "recomputed": False}

        expected_score = record["score"]
        if record["kind"] in {"segment_score", "dialog_score"}:
            self._compare_score(record_id, expected_score, replayed["score"])
        return {
            "record_id": record_id,
            "kind": record["kind"],
            "dialog_id": record["dialog_id"],
            "node_path": record["node_path"],
            "cache_keys": record["cache_keys"],
            "input_versions": record["input_versions"],
            "key_components": {
                level: loaded[level]["key_components"]
                for level in record["cache_keys"]
            },
            "score": replayed["score"],
            "reproduced": True,
            "replay_method": replayed["method"],
        }

    def _replay_segment(
        self,
        record: dict[str, Any],
        loaded: dict[str, dict[str, Any]],
        cache: ContentAddressedCache,
    ) -> dict[str, Any]:
        segment_key = record["cache_keys"].get("segment_regression")
        features_key = record["cache_keys"].get("frame_features")
        if not segment_key:
            fail(
                "E_LINEAGE_BROKEN",
                "segment lineage has no segment regression key",
                missing_key_level="segment_regression",
                record_id=record["record_id"],
            )
        if not features_key:
            fail(
                "E_LINEAGE_BROKEN",
                "segment lineage has no feature key",
                missing_key_level="frame_features",
                record_id=record["record_id"],
            )
        segment_entry = loaded["segment_regression"]
        feature_entry = loaded["frame_features"]
        data = segment_entry["data"]
        segment = {
            "segment_index": data["segment_index"],
            "start_frame": data["start_frame"],
            "end_frame": data["end_frame"],
            "start_ms": data["start_ms"],
            "end_ms": data["end_ms"],
            "duration_ms": data["duration_ms"],
            "speaker_id": data["speaker_id"],
        }
        recomputed = score_segment(
            dialog_id=data["dialog_id"],
            segment=segment,
            feature_data=feature_entry["data"],
            truth_rows=data["truth_rows"],
            ridge_lambda=float(data["ridge_lambda"]),
            confidence_level=float(data["valence_ci"]["level"]),
        )
        return {
            "score": {
                "valence": recomputed["valence"],
                "arousal": recomputed["arousal"],
                "valence_ci": recomputed["valence_ci"],
                "arousal_ci": recomputed["arousal_ci"],
            },
            "method": "recomputed from cached feature matrix and recorded truth snapshot",
        }

    def _replay_dialog(
        self,
        record: dict[str, Any],
        loaded: dict[str, dict[str, Any]],
        cache: ContentAddressedCache,
    ) -> dict[str, Any]:
        segment_keys = record.get("segment_keys", [])
        if not segment_keys:
            fail(
                "E_LINEAGE_BROKEN",
                "dialog lineage has no segment key list",
                missing_key_level="segment_regression",
                record_id=record["record_id"],
            )
        scores: list[dict[str, Any]] = []
        for index, key in enumerate(segment_keys):
            entry = cache.get(key)
            if entry is None:
                fail(
                    "E_LINEAGE_BROKEN",
                    "segment cache key required by dialog lineage is missing",
                    missing_key_level="segment_regression",
                    segment_position=index,
                    key=key,
                    record_id=record["record_id"],
                )
            scores.append(entry["data"])
        aggregate = aggregate_dialog(
            dialog_id=record["dialog_id"],
            segment_scores=scores,
            weight_basis=record["input_versions"]["weight_basis"],
        )
        return {
            "score": {
                "valence": aggregate["valence"],
                "arousal": aggregate["arousal"],
                "weight_basis": aggregate["weight_basis"],
                "total_weight": aggregate["total_weight"],
            },
            "method": "independently recomputed from cached segment scores and weights",
        }

    @staticmethod
    def _compare_score(
        record_id: str,
        expected: dict[str, Any],
        actual: dict[str, Any],
    ) -> None:
        comparable_keys = {
            "valence",
            "arousal",
            "valence_ci",
            "arousal_ci",
            "weight_basis",
            "total_weight",
        }
        for key in comparable_keys:
            if key in expected and expected[key] != actual.get(key):
                fail(
                    "E_LINEAGE_BROKEN",
                    "recorded score cannot be reproduced from lineage keys",
                    missing_key_level="score_reproduction",
                    record_id=record_id,
                    field=key,
                    expected=expected[key],
                    actual=actual.get(key),
                )
