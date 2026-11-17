from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

from .config import TrainingConfig
from .errors import CheckpointCorrupt
from .io import (
    atomic_write_json,
    atomic_write_jsonl,
    fsync_directory,
    read_json,
    read_jsonl,
    remove_tree,
    sha256_json,
)
from .model import DeterministicModel
from .shards import read_generation_payload


MANIFEST_SCHEMA_VERSION = 1


def _base_manifest(config_hash: str) -> dict[str, Any]:
    body = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "manifest_version": 0,
        "config_hash": config_hash,
        "global_generation": 0,
        "current_generation": None,
        "total_bytes": 0,
        "generations": [],
    }
    body["manifest_digest"] = sha256_json(body)
    return body


def _seal_manifest(body: dict[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(body)
    sealed.pop("manifest_digest", None)
    sealed["manifest_digest"] = sha256_json(sealed)
    return sealed


class CheckpointStore:
    def __init__(self, root: Path, config: TrainingConfig):
        self.root = root
        self.config = config
        self.manifest_path = root / "manifest.json"
        self.generations_dir = root / "generations"
        self.history_path = root / "lineage.jsonl"
        self.generations_dir.mkdir(parents=True, exist_ok=True)

    def load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return _base_manifest(self.config.config_hash)
        try:
            manifest = read_json(self.manifest_path)
        except Exception as exc:
            raise CheckpointCorrupt(f"cannot read checkpoint manifest: {exc}") from exc
        if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise CheckpointCorrupt("unsupported checkpoint manifest schema version")
        if manifest.get("config_hash") != self.config.config_hash:
            raise CheckpointCorrupt(
                "checkpoint manifest config hash does not match the active training config",
                details={
                    "manifest_config_hash": manifest.get("config_hash"),
                    "active_config_hash": self.config.config_hash,
                },
            )
        expected_digest = manifest.get("manifest_digest")
        body = copy.deepcopy(manifest)
        body.pop("manifest_digest", None)
        if expected_digest != sha256_json(body):
            raise CheckpointCorrupt("checkpoint manifest digest verification failed")
        if int(manifest.get("total_bytes", -1)) != sum(
            int(generation.get("payload_bytes", 0)) for generation in manifest.get("generations", [])
        ):
            raise CheckpointCorrupt("checkpoint manifest byte accounting is inconsistent")
        return manifest

    def current_generation(self, manifest: dict[str, Any]) -> dict[str, Any] | None:
        current_id = manifest.get("current_generation")
        if current_id is None:
            return None
        for generation in manifest.get("generations", []):
            if generation["generation_id"] == current_id:
                return generation
        raise CheckpointCorrupt(f"manifest current_generation {current_id!r} is not listed")

    def current_usage(self, manifest: dict[str, Any]) -> int:
        return sum(int(generation.get("payload_bytes", 0)) for generation in manifest.get("generations", []))

    def current_round(self, manifest: dict[str, Any]) -> int:
        generation = self.current_generation(manifest)
        return int(generation["round"]) if generation else 0

    def create_staging_dir(self, sequence: int) -> Path:
        path = self.root / f".staging-gen-{sequence:06d}"
        remove_tree(path)
        (path / "shards").mkdir(parents=True, exist_ok=False)
        return path

    def finalize_generation_dir(self, staging: Path, sequence: int) -> Path:
        final = self.generations_dir / f"gen-{sequence:06d}"
        if final.exists():
            raise CheckpointCorrupt(f"generation directory {final} already exists")
        staging.rename(final)
        fsync_directory(self.generations_dir)
        return final

    def commit_generation(
        self,
        manifest: dict[str, Any],
        generation: dict[str, Any],
        *,
        evicted_generation_ids: list[str],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        candidate = copy.deepcopy(manifest)
        evicted = set(evicted_generation_ids)
        retained = [
            copy.deepcopy(item)
            for item in candidate.get("generations", [])
            if item["generation_id"] not in evicted
        ]
        retained.append(copy.deepcopy(generation))
        retained.sort(key=lambda item: int(item["generation"]))
        candidate["generations"] = retained
        candidate["manifest_version"] = int(candidate.get("manifest_version", 0)) + 1
        candidate["global_generation"] = max(
            int(candidate.get("global_generation", 0)), int(generation["generation"])
        )
        candidate["current_generation"] = generation["generation_id"]
        candidate["total_bytes"] = sum(int(item.get("payload_bytes", 0)) for item in retained)
        candidate = _seal_manifest(candidate)
        atomic_write_json(self.manifest_path, candidate)

        history = self.load_history()
        history.append(
            {
                "event": "commit_generation",
                "manifest_version": candidate["manifest_version"],
                "global_generation": candidate["global_generation"],
                "generation_id": generation["generation_id"],
                "generation": generation["generation"],
                "round": generation["round"],
                "payload_bytes": generation["payload_bytes"],
                "evicted_generation_ids": evicted_generation_ids,
                "manifest_digest": candidate["manifest_digest"],
            }
        )
        atomic_write_jsonl(self.history_path, history)
        return candidate, history

    def delete_generation_dirs(self, generation_ids: list[str], manifest: dict[str, Any]) -> list[str]:
        removed: list[str] = []
        referenced = {item["generation_id"]: item for item in manifest.get("generations", [])}
        for generation_id in generation_ids:
            if generation_id in referenced:
                continue
            path = self.generations_dir / generation_id
            if path.exists():
                remove_tree(path)
                removed.append(generation_id)
        return removed

    def load_history(self) -> list[dict[str, Any]]:
        return read_jsonl(self.history_path)

    def recover_orphans(self, manifest: dict[str, Any]) -> list[str]:
        removed: list[str] = []
        for path in sorted(self.root.glob(".staging-gen-*")):
            if path.is_dir():
                remove_tree(path)
                removed.append(str(path))
        for path in sorted(self.generations_dir.glob("gen-*")):
            if path.is_dir() and path.name not in {
                item["generation_id"] for item in manifest.get("generations", [])
            }:
                remove_tree(path)
                removed.append(str(path))
        return removed

    def read_generation(
        self, generation: dict[str, Any]
    ) -> tuple[bytes, list[dict[str, Any]], list[dict[str, Any]]]:
        generation_dir = self.root / generation["directory"]
        if not generation_dir.is_dir():
            raise CheckpointCorrupt(
                f"generation directory {generation_dir} is missing",
                details={"generation_id": generation.get("generation_id")},
            )
        payload, logical, split_operations = read_generation_payload(generation_dir, generation)
        return payload, logical, split_operations

    def verify_and_restore(
        self, manifest: dict[str, Any]
    ) -> tuple[DeterministicModel | None, dict[str, Any] | None, list[dict[str, Any]]]:
        generation = self.current_generation(manifest)
        if generation is None:
            return None, None, []
        payload, logical, split_operations = self.read_generation(generation)
        if len(payload) != int(generation.get("parameter_bytes", -1)):
            raise CheckpointCorrupt("restored generation parameter length does not match manifest")
        logical_digest = sha256_json(
            [
                {
                    "logical_shard_id": item["logical_shard_id"],
                    "file_offset": item["file_offset"],
                    "size_bytes": item["size_bytes"],
                    "content_fingerprint": item["content_fingerprint"],
                }
                for item in logical
            ]
        )
        if logical_digest != generation.get("logical_manifest_digest"):
            raise CheckpointCorrupt("logical shard manifest digest mismatch")
        model = DeterministicModel.from_bytes(self.config, payload)[0]
        if model.digest != generation.get("params_digest"):
            raise CheckpointCorrupt("restored parameter digest does not match generation manifest")
        return model, generation, split_operations

    def audit_orphans_on_disk(self) -> list[str]:
        expected = {item["generation_id"] for item in self.load_manifest().get("generations", [])}
        return sorted(
            path.name for path in self.generations_dir.glob("gen-*") if path.name not in expected
        )

    def copy_generation_for_debug(self, generation_id: str, destination: Path) -> None:
        source = self.generations_dir / generation_id
        if not source.is_dir():
            raise FileNotFoundError(source)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
