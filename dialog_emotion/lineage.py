from __future__ import annotations

from pathlib import Path
from typing import Any

from .cache import CacheStore
from .errors import DomainError, E_LINEAGE_BROKEN
from .util import (
    atomic_write_json,
    canonical_json_bytes,
    read_json,
    sha256_text,
    sha256_value,
)


NODE_PATH = ["frame_features", "speaker_embedding", "segment_emotion", "dialog_aggregate"]


def make_score_id(
    dialog_id: str,
    scope: str,
    segment_id: str | None,
    target_key: str,
    run_id: str,
) -> str:
    digest = sha256_text(f"{dialog_id}\n{scope}\n{segment_id or ''}\n{target_key}\n{run_id}").split(":", 1)[-1]
    return "scr_" + digest[:24]


def make_segment_record(
    dialog_id: str,
    detail: dict[str, Any],
    cache_keys: dict[str, str],
    artifact_hashes: dict[str, str],
    input_versions: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    score = {
        "valence": detail["valence"],
        "arousal": detail["arousal"],
        "ci_lower_valence": detail["ci_lower_valence"],
        "ci_upper_valence": detail["ci_upper_valence"],
        "ci_lower_arousal": detail["ci_lower_arousal"],
        "ci_upper_arousal": detail["ci_upper_arousal"],
        "confidence_level": detail["confidence_level"],
    }
    target_key = cache_keys["segment_emotion"]
    return {
        "schema_version": "lineage-record-v1",
        "score_id": make_score_id(dialog_id, "segment", detail["segment_id"], target_key, run_id),
        "dialog_id": dialog_id,
        "scope": "segment",
        "segment_id": detail["segment_id"],
        "score": score,
        "cache_key": target_key,
        "node_path": NODE_PATH[:3],
        "cache_keys": {key: cache_keys[key] for key in NODE_PATH[:3]},
        "artifact_hashes": {key: artifact_hashes[key] for key in NODE_PATH[:3]},
        "input_versions": input_versions,
        "run_id": run_id,
    }


def make_dialog_record(
    dialog_id: str,
    aggregate: dict[str, Any],
    cache_keys: dict[str, str],
    artifact_hashes: dict[str, str],
    input_versions: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    score = {
        "valence": aggregate["valence"],
        "arousal": aggregate["arousal"],
        "ci_lower_valence": aggregate["ci_lower_valence"],
        "ci_upper_valence": aggregate["ci_upper_valence"],
        "ci_lower_arousal": aggregate["ci_lower_arousal"],
        "ci_upper_arousal": aggregate["ci_upper_arousal"],
        "confidence_level": aggregate["confidence_level"],
        "weight_basis": aggregate["weight_basis"],
        "sum_weights": aggregate["sum_weights"],
    }
    target_key = cache_keys["dialog_aggregate"]
    return {
        "schema_version": "lineage-record-v1",
        "score_id": make_score_id(dialog_id, "dialog", None, target_key, run_id),
        "dialog_id": dialog_id,
        "scope": "dialog",
        "segment_id": None,
        "score": score,
        "cache_key": target_key,
        "node_path": NODE_PATH,
        "cache_keys": {key: cache_keys[key] for key in NODE_PATH},
        "artifact_hashes": {key: artifact_hashes[key] for key in NODE_PATH},
        "input_versions": input_versions,
        "run_id": run_id,
    }


class LineageStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path_for(self, score_id: str) -> Path:
        if not score_id.startswith("scr_") or any(character not in "0123456789abcdef" for character in score_id[4:]):
            raise DomainError(
                E_LINEAGE_BROKEN,
                "score_id is not a valid lineage identifier",
                breakpoint_node="lineage_record",
                breakpoint_key=score_id,
            )
        return self.root / f"{score_id}.json"

    def save(self, record: dict[str, Any]) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(record["score_id"])
        if path.exists():
            existing = read_json(path)
            if canonical_json_bytes(existing) != canonical_json_bytes(record):
                raise DomainError(
                    E_LINEAGE_BROKEN,
                    "immutable lineage record already exists with different content",
                    breakpoint_node="lineage_record",
                    breakpoint_key=record["score_id"],
                )
            return path
        atomic_write_json(path, record)
        return path

    def load(self, score_id: str) -> dict[str, Any]:
        path = self.path_for(score_id)
        if not path.exists():
            raise DomainError(
                E_LINEAGE_BROKEN,
                "lineage record does not exist",
                breakpoint_node="lineage_record",
                breakpoint_key=score_id,
            )
        try:
            record = read_json(path)
        except Exception as exc:
            raise DomainError(
                E_LINEAGE_BROKEN,
                "lineage record is not readable",
                breakpoint_node="lineage_record",
                breakpoint_key=score_id,
            ) from exc
        if record.get("score_id") != score_id:
            raise DomainError(
                E_LINEAGE_BROKEN,
                "lineage record score_id does not match its filename",
                breakpoint_node="lineage_record",
                breakpoint_key=score_id,
            )
        return record

    def replay(self, score_id: str, cache: CacheStore) -> dict[str, Any]:
        record = self.load(score_id)
        node_path = list(record.get("node_path", []))
        if record.get("scope") == "segment" and node_path != NODE_PATH[:3]:
            raise DomainError(
                E_LINEAGE_BROKEN,
                "segment lineage node path is incomplete",
                breakpoint_node="lineage_record",
                breakpoint_key=score_id,
            )
        if record.get("scope") == "dialog" and node_path != NODE_PATH:
            raise DomainError(
                E_LINEAGE_BROKEN,
                "dialog lineage node path is incomplete",
                breakpoint_node="lineage_record",
                breakpoint_key=score_id,
            )
        loaded: dict[str, dict[str, Any]] = {}
        for node in node_path:
            key = record.get("cache_keys", {}).get(node)
            if not key:
                raise DomainError(
                    E_LINEAGE_BROKEN,
                    "lineage record has no cache key for a node",
                    breakpoint_node=node,
                    breakpoint_key=None,
                )
            try:
                entry = cache.get(key)
            except DomainError as exc:
                raise DomainError(
                    E_LINEAGE_BROKEN,
                    "cache entry required by the lineage chain cannot be verified",
                    breakpoint_node=node,
                    breakpoint_key=key,
                    cause_code=exc.code,
                    cause=exc.details,
                ) from exc
            if entry is None:
                raise DomainError(
                    E_LINEAGE_BROKEN,
                    "cache entry required by the lineage chain is missing",
                    breakpoint_node=node,
                    breakpoint_key=key,
                )
            expected_hash = record.get("artifact_hashes", {}).get(node)
            if expected_hash != entry.get("artifact_hash"):
                raise DomainError(
                    E_LINEAGE_BROKEN,
                    "lineage artifact hash does not match the cache entry",
                    breakpoint_node=node,
                    breakpoint_key=key,
                    expected_artifact_hash=expected_hash,
                    actual_artifact_hash=entry.get("artifact_hash"),
                )
            loaded[node] = entry
        if record["scope"] == "segment":
            target = loaded["segment_emotion"]["payload"]
            segment = next(
                (
                    detail
                    for detail in target.get("segments", [])
                    if detail.get("segment_id") == record.get("segment_id")
                ),
                None,
            )
            if segment is None:
                raise DomainError(
                    E_LINEAGE_BROKEN,
                    "recorded segment is absent from the replayed segment artifact",
                    breakpoint_node="segment_emotion",
                    breakpoint_key=record["cache_key"],
                    segment_id=record.get("segment_id"),
                )
            replayed = {
                key: segment[key]
                for key in (
                    "valence",
                    "arousal",
                    "ci_lower_valence",
                    "ci_upper_valence",
                    "ci_lower_arousal",
                    "ci_upper_arousal",
                    "confidence_level",
                )
            }
        else:
            target = loaded["dialog_aggregate"]["payload"]
            segment_target = loaded["segment_emotion"]["payload"]
            replayed = {
                "valence": target["valence"],
                "arousal": target["arousal"],
                "ci_lower_valence": target["ci_lower_valence"],
                "ci_upper_valence": target["ci_upper_valence"],
                "ci_lower_arousal": target["ci_lower_arousal"],
                "ci_upper_arousal": target["ci_upper_arousal"],
                "confidence_level": target["confidence_level"],
                "weight_basis": target["weight_basis"],
                "sum_weights": target["sum_weights"],
            }
            detail_map = {detail["segment_id"]: detail for detail in segment_target["segments"]}
            fields = (
                "valence",
                "arousal",
                "ci_lower_valence",
                "ci_upper_valence",
                "ci_lower_arousal",
                "ci_upper_arousal",
            )
            for field in fields:
                weighted = round(
                    sum(
                        float(weight_row["weight"]) * float(detail_map[weight_row["segment_id"]][field])
                        for weight_row in target["weights"]
                    ),
                    8,
                )
                if weighted != target[field]:
                    raise DomainError(
                        E_LINEAGE_BROKEN,
                        "dialog score cannot be recomputed from its segment details and weights",
                        breakpoint_node="dialog_aggregate",
                        breakpoint_key=record["cache_key"],
                        mismatched_field=field,
                    )
        expected_score = record.get("score", {})
        mismatched = [field for field in expected_score if replayed.get(field) != expected_score[field]]
        if mismatched:
            raise DomainError(
                E_LINEAGE_BROKEN,
                "replayed score does not match the recorded score",
                breakpoint_node=record["node_path"][-1],
                breakpoint_key=record["cache_key"],
                mismatched_fields=mismatched,
            )
        return {
            "status": "OK",
            "business_code": "OK",
            "score_id": score_id,
            "dialog_id": record["dialog_id"],
            "scope": record["scope"],
            "segment_id": record.get("segment_id"),
            "replayed_score": replayed,
            "recorded_score": expected_score,
            "cache_key": record["cache_key"],
            "cache_keys": record["cache_keys"],
            "node_path": record["node_path"],
            "input_versions": record["input_versions"],
            "artifact_hashes": record["artifact_hashes"],
            "run_id": record["run_id"],
            "cache_writes_performed": 0,
        }
