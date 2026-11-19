"""Versioned runtime configuration with checksum validation."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AppError, ErrorCode, fail
from .model import checksum, stable_json
from .storage import atomic_write_json, read_json


CONFIG_SCHEMA = "annotation-app-config/1"


@dataclass(frozen=True)
class Limits:
    max_import_bytes: int
    max_annotation_total: int
    max_graph_nodes: int
    on_limit: str

    @classmethod
    def from_dict(cls, value: Any, path: str) -> "Limits":
        if not isinstance(value, dict):
            fail(
                ErrorCode.SCHEMA_TYPE,
                "limits must be an object",
                path=path,
                stage="config",
            )
        required = (
            "max_import_bytes",
            "max_annotation_total",
            "max_graph_nodes",
            "on_limit",
        )
        for key in required:
            if key not in value:
                fail(
                    ErrorCode.SCHEMA_MISSING,
                    f"missing required config field {key}",
                    path=f"{path}.{key}",
                    stage="config",
                )
        for key in required[:3]:
            item = value[key]
            if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
                fail(
                    ErrorCode.SCHEMA_TYPE,
                    f"{key} must be a positive integer",
                    path=f"{path}.{key}",
                    stage="config",
                )
        if value["on_limit"] not in {"reject", "entities_only"}:
            fail(
                ErrorCode.SCHEMA_TYPE,
                "on_limit must be 'reject' or 'entities_only'",
                path=f"{path}.on_limit",
                stage="config",
            )
        return cls(
            max_import_bytes=value["max_import_bytes"],
            max_annotation_total=value["max_annotation_total"],
            max_graph_nodes=value["max_graph_nodes"],
            on_limit=value["on_limit"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_import_bytes": self.max_import_bytes,
            "max_annotation_total": self.max_annotation_total,
            "max_graph_nodes": self.max_graph_nodes,
            "on_limit": self.on_limit,
        }


@dataclass(frozen=True)
class RuntimeConfig:
    version: int
    limits: Limits
    ontology_contract_mode: str
    checksum_value: str
    raw: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Any) -> "RuntimeConfig":
        if not isinstance(value, dict):
            fail(
                ErrorCode.SCHEMA_TYPE,
                "config document must be an object",
                path="$",
                stage="config",
            )
        for key in ("schema", "version", "limits", "ontology_contract_mode", "checksum"):
            if key not in value:
                fail(
                    ErrorCode.SCHEMA_MISSING,
                    f"missing required config field {key}",
                    path=f"$.{key}",
                    stage="config",
                )
        if value["schema"] != CONFIG_SCHEMA:
            fail(
                ErrorCode.VERSION_UNSUPPORTED,
                f"unsupported config schema {value['schema']!r}",
                path="$.schema",
                stage="config",
            )
        version = value["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            fail(
                ErrorCode.SCHEMA_TYPE,
                "config version must be a positive integer",
                path="$.version",
                stage="config",
            )
        if value["ontology_contract_mode"] not in {"strict", "lenient"}:
            fail(
                ErrorCode.SCHEMA_TYPE,
                "ontology_contract_mode must be 'strict' or 'lenient'",
                path="$.ontology_contract_mode",
                stage="config",
            )
        material = {key: item for key, item in value.items() if key != "checksum"}
        actual = checksum(material)
        if value["checksum"] != actual:
            fail(
                ErrorCode.ONTOLOGY_CONTRACT,
                "config checksum does not match its content",
                path="$.checksum",
                stage="config",
                details={"expected": actual, "actual": value["checksum"]},
            )
        return cls(
            version=version,
            limits=Limits.from_dict(value["limits"], "$.limits"),
            ontology_contract_mode=value["ontology_contract_mode"],
            checksum_value=value["checksum"],
            raw=copy.deepcopy(value),
        )


class ConfigStore:
    """Load config snapshots as whole replacements."""

    def __init__(self, active_path: Path | None = None) -> None:
        self.active_path = active_path
        self._active: RuntimeConfig | None = None

    @property
    def active(self) -> RuntimeConfig | None:
        return self._active

    def load(self, path: Path) -> RuntimeConfig:
        try:
            data = read_json(path)
        except (OSError, ValueError) as exc:
            raise AppError(
                ErrorCode.FORMAT_INVALID,
                f"cannot load config: {exc}",
                path="$",
                details={"file": str(path)},
                cause=exc,
            ) from exc
        candidate = RuntimeConfig.from_dict(data)
        self.activate(candidate)
        return candidate

    def activate(self, candidate: RuntimeConfig) -> RuntimeConfig:
        if self._active is not None:
            old = self._active
            if candidate.version < old.version:
                fail(
                    ErrorCode.ONTOLOGY_CONTRACT,
                    "config version must increase monotonically",
                    path="$.version",
                    stage="config",
                    details={"active": old.version, "incoming": candidate.version},
                )
            if candidate.version == old.version:
                if candidate.checksum_value != old.checksum_value:
                    fail(
                        ErrorCode.ONTOLOGY_CONTRACT,
                        "same config version cannot carry different content",
                        path="$.checksum",
                        stage="config",
                        details={
                            "active": old.checksum_value,
                            "incoming": candidate.checksum_value,
                        },
                    )
                return old
        # Whole-object replacement, never an in-place patch of nested fields.
        self._active = candidate
        if self.active_path is not None:
            atomic_write_json(
                self.active_path,
                {
                    "snapshot": candidate.raw,
                    "content_checksum": candidate.checksum_value,
                    "stable_json_sha256": checksum(stable_json(candidate.raw)),
                },
            )
        return candidate

