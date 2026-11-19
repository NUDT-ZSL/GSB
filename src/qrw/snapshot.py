from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import checksum_tag
from .errors import (
    BusinessError,
    E_FORMAT_CORRUPT,
    E_SLOT_TYPE_RETIRED,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
)
from .readers import load_slot_types


@dataclass
class Snapshot:
    version: int
    set_id: str
    checksum: str
    slot_types: dict[str, Any]
    sources: dict[str, str]
    manifest_path: Path
    manifest: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "set_id": self.set_id,
            "checksum": self.checksum,
            "slot_types": sorted(self.slot_types["types"]),
            "dialects": sorted(self.sources),
            "manifest": str(self.manifest_path),
        }


def snapshot_payload(
    *,
    version: int,
    set_id: str,
    slot_types: dict[str, Any],
    sources: dict[str, str],
) -> dict[str, Any]:
    return {
        "version": version,
        "set_id": set_id,
        "slot_types": slot_types,
        "dialects": sources,
    }


def _snapshot_corrupt(message: str, path: Path, **details: Any) -> BusinessError:
    return BusinessError(
        E_SNAPSHOT_CORRUPT,
        message,
        line=details.pop("line", 1),
        field=details.pop("field", "snapshot"),
        column=details.pop("column", 1),
        details={"manifest": str(path), **details},
    )


def load_snapshot(path: Path) -> Snapshot:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise _snapshot_corrupt(f"cannot read snapshot manifest: {exc}", path) from exc
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _snapshot_corrupt(
            "snapshot manifest is not valid JSON",
            path,
            line=exc.lineno,
            field="manifest",
            column=exc.colno,
            reason=exc.msg,
        ) from exc
    if not isinstance(manifest, dict):
        raise _snapshot_corrupt("snapshot manifest must be a JSON object", path)
    version = manifest.get("version")
    set_id = manifest.get("set_id")
    expected_checksum = manifest.get("checksum")
    files = manifest.get("dialect_files")
    slot_types_file = manifest.get("slot_types_file")
    if not isinstance(version, int) or version < 1:
        raise _snapshot_corrupt("snapshot version must be a positive integer", path, field="version")
    if not isinstance(set_id, str) or not set_id:
        raise _snapshot_corrupt("snapshot set_id must be a non-empty string", path, field="set_id")
    if not isinstance(expected_checksum, str) or not expected_checksum.startswith("sha256:"):
        raise _snapshot_corrupt("snapshot checksum is missing or malformed", path, field="checksum")
    if not isinstance(files, dict) or set(files) != {"v1", "v2", "v3"}:
        raise _snapshot_corrupt(
            "snapshot dialect_files must contain v1, v2 and v3",
            path,
            field="dialect_files",
        )
    if not isinstance(slot_types_file, str) or not slot_types_file:
        raise _snapshot_corrupt(
            "snapshot slot_types_file is missing",
            path,
            field="slot_types_file",
        )
    base = path.parent
    sources: dict[str, str] = {}
    for dialect in ("v1", "v2", "v3"):
        relative = files[dialect]
        if not isinstance(relative, str) or not relative:
            raise _snapshot_corrupt(
                f"snapshot dialect file for {dialect} is invalid",
                path,
                field=f"dialect_files.{dialect}",
            )
        target = (base / relative).resolve()
        try:
            sources[dialect] = target.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise _snapshot_corrupt(
                f"cannot read dialect file for {dialect}: {exc}",
                path,
                field=f"dialect_files.{dialect}",
                file=str(target),
            ) from exc
    slot_types_path = (base / slot_types_file).resolve()
    try:
        slot_types = load_slot_types(slot_types_path)
    except BusinessError as exc:
        raise _snapshot_corrupt(
            "snapshot slot type document is corrupt",
            path,
            field="slot_types_file",
            cause=exc.to_dict(),
        ) from exc
    payload = snapshot_payload(
        version=version,
        set_id=set_id,
        slot_types=slot_types,
        sources=sources,
    )
    actual_checksum = checksum_tag(payload)
    if actual_checksum != expected_checksum:
        raise _snapshot_corrupt(
            "snapshot content checksum does not match the manifest",
            path,
            field="checksum",
            expected=expected_checksum,
            actual=actual_checksum,
        )
    return Snapshot(
        version=version,
        set_id=set_id,
        checksum=actual_checksum,
        slot_types=slot_types,
        sources=sources,
        manifest_path=path.resolve(),
        manifest=manifest,
    )


def load_version_index(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise BusinessError(
            E_FORMAT_CORRUPT,
            f"cannot read version index: {exc}",
            field="version_index",
            details={"path": str(path)},
        ) from exc
    try:
        index = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "version index is not valid JSON",
            line=exc.lineno,
            field="version_index",
            column=exc.colno,
            details={"path": str(path), "reason": exc.msg},
        ) from exc
    if not isinstance(index, dict) or not isinstance(index.get("snapshots"), list):
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "version index must contain a snapshots array",
            field="snapshots",
            details={"path": str(path)},
        )
    current = index.get("current_version")
    if not isinstance(current, int):
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "version index current_version must be an integer",
            field="current_version",
            details={"path": str(path)},
        )
    versions: list[int] = []
    for position, entry in enumerate(index["snapshots"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("version"), int):
            raise BusinessError(
                E_FORMAT_CORRUPT,
                "version index snapshot entry is malformed",
                field=f"snapshots[{position}].version",
                details={"path": str(path)},
            )
        versions.append(entry["version"])
    if versions != sorted(set(versions)) or current not in versions:
        raise BusinessError(
            E_FORMAT_CORRUPT,
            "snapshot versions must be strictly increasing and include current_version",
            field="snapshots",
            details={"path": str(path), "versions": versions, "current": current},
        )
    index["_base"] = str(path.parent)
    return index


def load_current_snapshot(index_path: Path) -> tuple[Snapshot, dict[str, Any]]:
    index = load_version_index(index_path)
    current = index["current_version"]
    entry = next(item for item in index["snapshots"] if item["version"] == current)
    manifest_path = index_path.parent / entry["manifest"]
    snapshot = load_snapshot(manifest_path)
    if snapshot.version != current:
        raise _snapshot_corrupt(
            "snapshot manifest version does not match the version index",
            manifest_path,
            field="version",
            expected=current,
            actual=snapshot.version,
        )
    return snapshot, index


class SnapshotManager:
    def __init__(self) -> None:
        self.current: Snapshot | None = None
        self.history: list[dict[str, Any]] = []

    def install(
        self,
        snapshot: Snapshot,
        *,
        active_ir: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        previous = self.current
        if previous is not None and snapshot.version < previous.version:
            raise BusinessError(
                E_SNAPSHOT_ROLLBACK,
                "snapshot rollback is not allowed",
                field="version",
                details={
                    "current_version": previous.version,
                    "attempted_version": snapshot.version,
                    "current_checksum": previous.checksum,
                    "attempted_checksum": snapshot.checksum,
                },
            )
        if (
            previous is not None
            and snapshot.version == previous.version
            and snapshot.checksum == previous.checksum
        ):
            result = {
                "ok": True,
                "replaced": False,
                "reason": "identical_snapshot",
                "version": snapshot.version,
                "checksum": snapshot.checksum,
            }
            self.history.append(result)
            return result
        if active_ir is not None:
            self._assert_slot_types_retained(snapshot, active_ir)
        self.current = snapshot
        result = {
            "ok": True,
            "replaced": True,
            "incremental": False,
            "strategy": "whole_snapshot_replace",
            "previous_version": previous.version if previous else None,
            "previous_checksum": previous.checksum if previous else None,
            "version": snapshot.version,
            "checksum": snapshot.checksum,
        }
        self.history.append(result)
        return result

    @staticmethod
    def _assert_slot_types_retained(snapshot: Snapshot, active_ir: dict[str, Any]) -> None:
        available = set(snapshot.slot_types["types"])
        for rule in sorted(active_ir["rules"], key=lambda item: item["rule_id"]):
            for slot in sorted(rule["slots"], key=lambda item: item["name"]):
                if slot["type"] not in available:
                    raise BusinessError(
                        E_SLOT_TYPE_RETIRED,
                        "new snapshot retires a slot type used by compiled rules",
                        line=slot["source"]["line"],
                        field=f"rules.{rule['rule_id']}.slots.{slot['name']}.type",
                        column=slot["source"]["column"],
                        details={
                            "rule_id": rule["rule_id"],
                            "slot": slot["name"],
                            "slot_type": slot["type"],
                            "source": slot["source"]["source"],
                            "source_line": slot["source"]["line"],
                            "snapshot_version": snapshot.version,
                            "available_types": sorted(available),
                        },
                    )

