from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import content_hash
from .config import Rules
from .errors import E_SEMANTIC_DRIFT, fail
from .formats import (
    V1,
    V2,
    SUPPORTED_VERSIONS,
    _normalize_version,
    export_state,
    read_state,
)
from .models import InternalState


@dataclass(frozen=True)
class MigrationResult:
    source_version: str
    target_version: str
    state: InternalState
    source_path: str
    target_path: str
    completions: tuple[dict[str, Any], ...]
    roundtrip_completions: tuple[dict[str, Any], ...]
    drops: tuple[dict[str, Any], ...]
    semantic_equal: bool
    migration_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "source_version": self.source_version,
            "target_version": self.target_version,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "internal_state": self.state.to_view(),
            "completions": list(self.completions),
            "roundtrip_completions": list(self.roundtrip_completions),
            "drops": list(self.drops),
            "semantic_equal": self.semantic_equal,
        }


def migrate(
    source_path: str | Path,
    target_path: str | Path,
    *,
    target_version: str,
    rules: Rules,
    overwrite: bool = False,
    strict_audit_drops: bool = False,
) -> MigrationResult:
    normalized_target = _normalize_version(target_version)
    if normalized_target is None:
        fail(
            E_SEMANTIC_DRIFT,
            "target version cannot be represented",
            field="target_version",
            details={"value": target_version, "supported": list(SUPPORTED_VERSIONS)},
        )
    source = read_state(source_path, rules)
    source_version = _detect_result_version(source_path)
    if (
        strict_audit_drops
        and source.audit_history
        and normalized_target in (V1, V2)
    ):
        fail(
            E_SEMANTIC_DRIFT,
            "target format cannot preserve nested change history",
            field="change_history",
            details={
                "source_version": source_version,
                "target_version": normalized_target,
                "event_count": len(source.audit_history),
            },
        )
    exported = export_state(
        target_path,
        source.state,
        rules,
        target_version=normalized_target,
        audit_history=source.audit_history,
        overwrite=overwrite,
    )
    target = read_state(target_path, rules)
    semantic_equal = (
        source.state.to_semantic_dict() == target.state.to_semantic_dict()
    )
    if not semantic_equal:
        fail(
            E_SEMANTIC_DRIFT,
            "format migration changed the semantic internal state",
            field="internal_state",
            details={
                "source_version": source_version,
                "target_version": normalized_target,
                "source_checksum": source.state.state_checksum,
                "target_checksum": target.state.state_checksum,
            },
        )
    migration_id = content_hash(
        {
            "node": "migration",
            "source_version": source_version,
            "target_version": normalized_target,
            "state_checksum": source.state.state_checksum,
            "drops": list(exported.dropped_audit_history),
        }
    )
    return MigrationResult(
        source_version=source_version,
        target_version=normalized_target,
        state=target.state,
        source_path=str(source_path),
        target_path=str(target_path),
        completions=source.completions,
        roundtrip_completions=target.completions,
        drops=exported.dropped_audit_history,
        semantic_equal=True,
        migration_id=migration_id,
    )


def _detect_result_version(path: str | Path) -> str:
    # Reading once more only for dispatch keeps format detection private to the
    # format layer while preserving a concise public migration contract.
    from .formats import detect_version
    from .canonical import read_json

    source = Path(path)
    manifest = source / "manifest.json" if source.is_dir() else source
    data = read_json(manifest)
    return detect_version(data)

