"""Persistent stage cache keyed by content fingerprints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .utils import atomic_write_json, read_json


class PipelineCache:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = {
            "schema_version": 1,
            "resource_manifest": {},
            "config_hash": "",
            "config_product_manifest": {},
            "injection_hash": "",
            "injection_stage_hashes": {},
            "references": {},
            "samples": {},
        }
        self.loaded = False

    def load(self) -> "PipelineCache":
        if not self.path.is_file():
            return self
        try:
            raw = read_json(self.path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return self
        if not isinstance(raw, dict):
            return self
        for key in ("resource_manifest", "config_hash", "config_product_manifest", "injection_hash", "injection_stage_hashes", "references", "samples"):
            if key in raw:
                self.data[key] = raw[key]
        self.loaded = True
        return self

    @property
    def samples(self) -> dict[str, Any]:
        value = self.data.get("samples")
        return value if isinstance(value, dict) else {}

    @property
    def references(self) -> dict[str, Any]:
        value = self.data.get("references")
        return value if isinstance(value, dict) else {}

    @property
    def resource_manifest(self) -> dict[str, str]:
        value = self.data.get("resource_manifest")
        return value if isinstance(value, dict) else {}

    @property
    def config_product_manifest(self) -> dict[str, str]:
        value = self.data.get("config_product_manifest")
        return value if isinstance(value, dict) else {}

    @property
    def injection_stage_hashes(self) -> dict[str, Any]:
        value = self.data.get("injection_stage_hashes")
        return value if isinstance(value, dict) else {}

    def get(self, sample_id: str, stage: str, key: str) -> tuple[bool, Any]:
        sample = self.samples.get(sample_id)
        if not isinstance(sample, dict):
            return False, None
        stages = sample.get("stages")
        if not isinstance(stages, dict):
            return False, None
        entry = stages.get(stage)
        if not isinstance(entry, dict) or entry.get("key") != key:
            return False, None
        if "value" not in entry:
            return False, None
        return True, entry["value"]

    def put(self, sample_id: str, stage: str, key: str, value: Any) -> None:
        samples = self.data.setdefault("samples", {})
        sample = samples.setdefault(sample_id, {"stages": {}})
        if not isinstance(sample, dict):
            sample = {"stages": {}}
            samples[sample_id] = sample
        stages = sample.setdefault("stages", {})
        stages[stage] = {"key": key, "value": value}

    def set_sample_metadata(self, sample_id: str, metadata: dict[str, Any]) -> None:
        samples = self.data.setdefault("samples", {})
        sample = samples.setdefault(sample_id, {"stages": {}})
        if not isinstance(sample, dict):
            sample = {"stages": {}}
            samples[sample_id] = sample
        sample.update(metadata)

    def update_headers(
        self,
        *,
        resource_manifest: dict[str, str],
        config_hash: str,
        config_product_manifest: dict[str, str],
        injection_hash: str,
        injection_stage_hashes: dict[str, Any],
        references: dict[str, Any],
    ) -> None:
        self.data["resource_manifest"] = dict(resource_manifest)
        self.data["config_hash"] = config_hash
        self.data["config_product_manifest"] = dict(config_product_manifest)
        self.data["injection_hash"] = injection_hash
        self.data["injection_stage_hashes"] = injection_stage_hashes
        self.data["references"] = references

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.path, self.data)

    def stage_fingerprint(self, sample_id: str, stage: str) -> str | None:
        sample = self.samples.get(sample_id)
        if not isinstance(sample, dict):
            return None
        entry = sample.get("stages", {}).get(stage)
        return str(entry.get("key")) if isinstance(entry, dict) else None

