from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ConfigMismatchError
from .utils import atomic_write_json, digest_json, to_jsonable


SNAPSHOT_VERSION = "gbdt-workbench-snapshot-v1"
MANIFEST_VERSION = SNAPSHOT_VERSION


class SnapshotStore:
    def __init__(
        self,
        output_dir: Path,
        config_fingerprint: str,
        *,
        strict_config_match: bool = False,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.snapshot_dir = self.output_dir / "snapshots"
        self.manifest_path = self.snapshot_dir / "manifest.json"
        self.config_fingerprint = config_fingerprint
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.previous_state = self._load_manifest()
        self.state = copy.deepcopy(self.previous_state)
        self.state.setdefault("stage_keys", {})
        self.state.setdefault("tree_keys", {})
        self.state.setdefault("artifacts", {})
        self.state.setdefault("commit_count", 0)
        if strict_config_match and self.previous_state:
            previous_fingerprint = self.previous_state.get("config_fingerprint")
            if previous_fingerprint != config_fingerprint:
                raise ConfigMismatchError(
                    "restored manifest configuration fingerprint does not match current configuration",
                    details={
                        "reason": "config_fingerprint_mismatch",
                        "manifest_config_fingerprint": previous_fingerprint,
                        "current_config_fingerprint": config_fingerprint,
                    },
                )
        self.state["config_fingerprint"] = config_fingerprint
        self.state["snapshot_version"] = SNAPSHOT_VERSION

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {}
        try:
            with self.manifest_path.open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigMismatchError(
                "snapshot manifest cannot be restored",
                details={"reason": "manifest_unreadable", "error": str(exc)},
            ) from exc
        if not isinstance(manifest, dict):
            raise ConfigMismatchError(
                "snapshot manifest root is not a mapping",
                details={"reason": "manifest_invalid"},
            )
        if manifest.get("snapshot_version") != MANIFEST_VERSION:
            raise ConfigMismatchError(
                "snapshot manifest version mismatch",
                details={
                    "reason": "snapshot_version_mismatch",
                    "expected": MANIFEST_VERSION,
                    "actual": manifest.get("snapshot_version"),
                },
            )
        return manifest

    def _path(self, stage: str, key: str) -> Path:
        return self.snapshot_dir / stage / f"{key}.json"

    def load(self, stage: str, key: str) -> dict[str, Any] | None:
        path = self._path(stage, key)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                envelope = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigMismatchError(
                "snapshot cannot be restored",
                details={"reason": "snapshot_unreadable", "stage": stage, "key": key},
            ) from exc
        if not isinstance(envelope, dict):
            raise ConfigMismatchError(
                "snapshot envelope is not a mapping",
                details={"reason": "snapshot_envelope_invalid", "stage": stage, "key": key},
            )
        if envelope.get("snapshot_version") != SNAPSHOT_VERSION:
            raise ConfigMismatchError(
                "snapshot version mismatch",
                details={
                    "reason": "snapshot_version_mismatch",
                    "stage": stage,
                    "key": key,
                    "expected": SNAPSHOT_VERSION,
                    "actual": envelope.get("snapshot_version"),
                },
            )
        if envelope.get("stage") != stage or envelope.get("key") != key:
            raise ConfigMismatchError(
                "snapshot stage/key mismatch",
                details={"reason": "snapshot_identity_mismatch", "stage": stage, "key": key},
            )
        if not envelope.get("config_fingerprint"):
            raise ConfigMismatchError(
                "snapshot configuration fingerprint is missing",
                details={"reason": "config_fingerprint_missing", "stage": stage, "key": key},
            )
        payload = envelope.get("payload")
        payload_fingerprint = envelope.get("payload_fingerprint")
        if not isinstance(payload, dict) or payload_fingerprint != digest_json(payload):
            raise ConfigMismatchError(
                "snapshot payload fingerprint mismatch",
                details={"reason": "payload_fingerprint_mismatch", "stage": stage, "key": key},
            )
        artifact_record = (
            self.state.get("artifacts", {}).get(stage, {}).get(key)
        )
        if (
            artifact_record is not None
            and artifact_record != envelope.get("config_fingerprint")
        ):
            raise ConfigMismatchError(
                "snapshot configuration fingerprint mismatch",
                details={
                    "reason": "artifact_config_fingerprint_mismatch",
                    "stage": stage,
                    "key": key,
                    "manifest_config_fingerprint": artifact_record,
                    "snapshot_config_fingerprint": envelope.get("config_fingerprint"),
                },
            )
        return payload

    def save(self, stage: str, key: str, payload: dict[str, Any]) -> None:
        envelope = {
            "snapshot_version": SNAPSHOT_VERSION,
            "stage": stage,
            "key": key,
            "config_fingerprint": self.config_fingerprint,
            "payload_fingerprint": digest_json(payload),
            "payload": to_jsonable(payload),
        }
        path = self._path(stage, key)
        atomic_write_json(path, envelope)
        self.state.setdefault("artifacts", {}).setdefault(stage, {})[key] = (
            self.config_fingerprint
        )

    def record_stage_key(self, stage: str, key: str) -> None:
        self.state.setdefault("stage_keys", {})[stage] = key

    def record_tree_key(self, tree_index: int, key: str) -> None:
        self.state.setdefault("tree_keys", {})[str(int(tree_index))] = key

    def previous_stage_key(self, stage: str) -> str | None:
        return self.previous_state.get("stage_keys", {}).get(stage)

    def previous_tree_key(self, tree_index: int) -> str | None:
        return self.previous_state.get("tree_keys", {}).get(str(int(tree_index)))

    def commit(self) -> None:
        self.state["commit_count"] = int(self.state.get("commit_count", 0)) + 1
        self.state["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(self.manifest_path, self.state)

    def stage_invalidation(self, stage: str, current_key: str) -> dict[str, Any]:
        previous_key = self.previous_stage_key(stage)
        return {
            "stage": stage,
            "previous_key": previous_key,
            "current_key": current_key,
            "invalidated": previous_key != current_key,
            "changed": previous_key is not None and previous_key != current_key,
            "initial": previous_key is None,
        }

    def tree_invalidation(self, tree_index: int, current_key: str) -> dict[str, Any]:
        previous_key = self.previous_tree_key(tree_index)
        return {
            "tree_index": int(tree_index),
            "previous_key": previous_key,
            "current_key": current_key,
            "invalidated": previous_key != current_key,
            "changed": previous_key is not None and previous_key != current_key,
            "initial": previous_key is None,
        }

    def audit(self, snapshot_interval: int) -> dict[str, Any]:
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "snapshot_directory": str(self.snapshot_dir),
            "manifest_path": str(self.manifest_path),
            "manifest_restored": bool(self.previous_state),
            "commit_count": int(self.state.get("commit_count", 0)),
            "snapshot_interval": int(snapshot_interval),
            "producer_config_fingerprint": self.config_fingerprint,
            "previous_config_fingerprint": self.previous_state.get("config_fingerprint"),
        }
