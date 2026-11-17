"""Versioned, atomically published stage snapshots."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .constants import SCHEMA_VERSION, STAGES
from .errors import ConfigMismatchError
from .utils import atomic_write_json, read_json


@dataclass
class SnapshotState:
    manifest: dict[str, Any]
    results: list[dict[str, Any]]
    version: int


class SnapshotStore:
    """Publish a complete manifest by replacing one small pointer atomically."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.snapshots_dir = self.state_dir / "snapshots"
        self.latest_path = self.state_dir / "latest.json"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    def load_latest(self) -> SnapshotState | None:
        if not self.latest_path.is_file():
            return None
        try:
            manifest = read_json(self.latest_path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ConfigMismatchError(
                f"latest snapshot cannot be read: {exc}",
                details={"reason": "snapshot_unreadable", "path": str(self.latest_path)},
            ) from exc
        if not isinstance(manifest, dict):
            raise ConfigMismatchError(
                "latest snapshot is not a JSON object",
                details={"reason": "snapshot_schema", "path": str(self.latest_path)},
            )
        version = manifest.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ConfigMismatchError(
                "snapshot version is invalid",
                details={"reason": "snapshot_version", "version": version},
            )
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise ConfigMismatchError(
                "snapshot schema version does not match this component",
                details={
                    "reason": "snapshot_schema_version",
                    "expected": SCHEMA_VERSION,
                    "actual": manifest.get("schema_version"),
                },
            )
        stages = manifest.get("stages")
        if stages != list(STAGES):
            raise ConfigMismatchError(
                "snapshot stage list is invalid",
                details={"reason": "snapshot_stages", "expected": list(STAGES), "actual": stages},
            )
        results = manifest.get("results", [])
        if not isinstance(results, list):
            raise ConfigMismatchError(
                "snapshot results are invalid",
                details={"reason": "snapshot_results"},
            )
        return SnapshotState(manifest=manifest, results=results, version=version)

    def validate(
        self,
        state: SnapshotState,
        *,
        config_hash: str,
        resource_hash: str,
        corpus_hash: str,
        injection_hash: str,
    ) -> None:
        checks = {
            "config_hash": config_hash,
            "resource_hash": resource_hash,
            "corpus_hash": corpus_hash,
            "injection_hash": injection_hash,
        }
        mismatches = {
            key: {"expected": expected, "actual": state.manifest.get(key)}
            for key, expected in checks.items()
            if state.manifest.get(key) != expected
        }
        if mismatches:
            raise ConfigMismatchError(
                "snapshot is incompatible with the current configuration or resource package",
                details={"reason": "snapshot_fingerprint_mismatch", "mismatches": mismatches},
            )

    def commit(
        self,
        *,
        results: Iterable[dict[str, Any]],
        config_hash: str,
        resource_hash: str,
        corpus_hash: str,
        injection_hash: str,
        base_version: int | None = None,
    ) -> dict[str, Any]:
        result_list = sorted(results, key=lambda item: str(item.get("sample_id", "")))
        version = (base_version or self._latest_version()) + 1
        directory_name = f"v{version:08d}"
        final_directory = self.snapshots_dir / directory_name
        temporary_directory = Path(
            tempfile.mkdtemp(prefix=f".{directory_name}.", dir=str(self.snapshots_dir))
        )
        try:
            stage_files: dict[str, str] = {}
            for stage in STAGES:
                stage_payload: dict[str, Any] = {}
                for result in result_list:
                    products = result.get("stage_products", {})
                    if isinstance(products, dict) and stage in products:
                        stage_payload[str(result["sample_id"])] = products[stage]
                stage_path = temporary_directory / f"{stage}.json"
                atomic_write_json(stage_path, stage_payload)
                stage_files[stage] = str(Path("snapshots") / directory_name / f"{stage}.json")
            completed = [str(item["sample_id"]) for item in result_list]
            successes = [item for item in result_list if item.get("status") == "ok"]
            failures = [item for item in result_list if item.get("status") != "ok"]
            intent_distribution: dict[str, int] = {}
            for result in successes:
                intent = str(result.get("intent", ""))
                intent_distribution[intent] = intent_distribution.get(intent, 0) + 1
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "stage": "all",
                "stages": list(STAGES),
                "stage_files": stage_files,
                "version": version,
                "config_hash": config_hash,
                "resource_hash": resource_hash,
                "corpus_hash": corpus_hash,
                "injection_hash": injection_hash,
                "completed_sample_ids": completed,
                "partial_result_summary": {
                    "completed_count": len(completed),
                    "success_count": len(successes),
                    "failure_count": len(failures),
                    "intent_distribution": dict(sorted(intent_distribution.items())),
                },
                "results": result_list,
                "snapshot_directory": str(Path("snapshots") / directory_name),
            }
            atomic_write_json(temporary_directory / "manifest.json", manifest)
            if final_directory.exists():
                raise ConfigMismatchError(
                    "snapshot version already exists",
                    details={"reason": "snapshot_version_collision", "version": version},
                )
            os.replace(temporary_directory, final_directory)
            # The pointer is the only visible commit point and is replaced
            # atomically after every stage file and manifest is durable.
            atomic_write_json(self.latest_path, manifest)
            return manifest
        except BaseException:
            if temporary_directory.exists():
                shutil.rmtree(temporary_directory, ignore_errors=True)
            raise

    def _latest_version(self) -> int:
        try:
            manifest = read_json(self.latest_path)
            version = manifest.get("version", 0) if isinstance(manifest, dict) else 0
            return int(version) if isinstance(version, int) and version > 0 else 0
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
            return 0

    def latest_version(self) -> int:
        return self._latest_version()

