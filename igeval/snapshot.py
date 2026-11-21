"""Versioned, checksummed evaluation snapshots and their local store."""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import DomainError, E_SNAPSHOT_CORRUPT
from .util import (
    atomic_write_bytes,
    canonical_bytes,
    canonical_json,
    deep_copy_json,
    read_json,
    sha256_json,
)


COMPONENT_NAMES = ("normalization", "alignment", "structure")
_VERSION_FILE_PATTERN = re.compile(r"^snapshot-v(\d{16})-([0-9a-f]{64})\.json$")


DEFAULT_NORMALIZATION = {
    "mean": 0.55,
    "std": 0.18,
    "fusion_weights": {
        "preprocess": 0.20,
        "semantic": 0.45,
        "structure": 0.35,
    },
}

DEFAULT_ALIGNMENT = {
    "prompt_weight": 0.75,
    "reference_weight": 0.25,
    "term_decay": 0.92,
}

DEFAULT_STRUCTURE = {
    "edge_weight": 0.45,
    "region_weight": 0.35,
    "orientation_weight": 0.20,
    "edge_target_low": 0.04,
    "edge_target_high": 0.32,
    "edge_tolerance": 0.11,
    "orientation_tolerance": 0.20,
}


def _component_checksum(kind: str, payload: dict[str, Any]) -> str:
    return sha256_json(
        {
            "schema": "igeval.snapshot-component/1",
            "kind": kind,
            "payload": payload,
        }
    )


def _snapshot_checksum(components: dict[str, Any], snapshot_version: int) -> str:
    return sha256_json(
        {
            "schema": "igeval.snapshot/1",
            "snapshot_version": snapshot_version,
            "components": components,
        }
    )


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainError(
            E_SNAPSHOT_CORRUPT,
            f"{field} must be numeric",
            field=field,
        )
    number = float(value)
    if not math.isfinite(number):
        raise DomainError(
            E_SNAPSHOT_CORRUPT,
            f"{field} must be finite",
            field=field,
        )
    return number


def _validate_snapshot_values(
    normalization: dict[str, Any],
    alignment: dict[str, Any],
    structure: dict[str, Any],
) -> None:
    _finite_number(normalization.get("mean"), "normalization.mean")
    if _finite_number(normalization.get("std"), "normalization.std") <= 0:
        raise DomainError(
            E_SNAPSHOT_CORRUPT,
            "normalization.std must be positive",
            field="normalization.std",
        )
    fusion = normalization.get("fusion_weights")
    if not isinstance(fusion, dict) or set(fusion) != {"preprocess", "semantic", "structure"}:
        raise DomainError(
            E_SNAPSHOT_CORRUPT,
            "normalization.fusion_weights must define preprocess/semantic/structure",
            field="normalization.fusion_weights",
        )
    fusion_values = [
        _finite_number(fusion[name], f"normalization.fusion_weights.{name}")
        for name in ("preprocess", "semantic", "structure")
    ]
    if any(value < 0 for value in fusion_values) or sum(fusion_values) <= 0:
        raise DomainError(
            E_SNAPSHOT_CORRUPT,
            "normalization fusion weights must be non-negative with a positive sum",
            field="normalization.fusion_weights",
        )

    prompt_weight = _finite_number(alignment.get("prompt_weight"), "alignment.prompt_weight")
    reference_weight = _finite_number(
        alignment.get("reference_weight"), "alignment.reference_weight"
    )
    if prompt_weight < 0 or reference_weight < 0 or prompt_weight + reference_weight <= 0:
        raise DomainError(
            E_SNAPSHOT_CORRUPT,
            "alignment weights must be non-negative with a positive sum",
            field="alignment.prompt_weight",
        )
    term_decay = _finite_number(alignment.get("term_decay"), "alignment.term_decay")
    if not 0 < term_decay <= 1:
        raise DomainError(
            E_SNAPSHOT_CORRUPT,
            "alignment.term_decay must be in (0, 1]",
            field="alignment.term_decay",
        )

    edge_weight = _finite_number(structure.get("edge_weight"), "structure.edge_weight")
    region_weight = _finite_number(structure.get("region_weight"), "structure.region_weight")
    orientation_weight = _finite_number(
        structure.get("orientation_weight"), "structure.orientation_weight"
    )
    if (
        min(edge_weight, region_weight, orientation_weight) < 0
        or edge_weight + region_weight + orientation_weight <= 0
    ):
        raise DomainError(
            E_SNAPSHOT_CORRUPT,
            "structure weights must be non-negative with a positive sum",
            field="structure.weights",
        )
    edge_low = _finite_number(structure.get("edge_target_low"), "structure.edge_target_low")
    edge_high = _finite_number(structure.get("edge_target_high"), "structure.edge_target_high")
    if not 0 <= edge_low < edge_high:
        raise DomainError(
            E_SNAPSHOT_CORRUPT,
            "structure edge target bounds must satisfy 0 <= low < high",
            field="structure.edge_target_low",
        )
    for field in ("edge_tolerance", "orientation_tolerance"):
        if _finite_number(structure.get(field), f"structure.{field}") <= 0:
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                f"structure.{field} must be positive",
                field=f"structure.{field}",
            )


def make_component(kind: str, version: int, payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise DomainError(
            E_SNAPSHOT_CORRUPT,
            f"{kind}.version must be a positive integer",
            field=f"{kind}.version",
        )
    body = deep_copy_json(payload)
    return {
        **body,
        "version": version,
        "checksum": _component_checksum(kind, body),
    }


@dataclass(frozen=True)
class EvaluationSnapshot:
    snapshot_version: int
    normalization: dict[str, Any]
    alignment: dict[str, Any]
    structure: dict[str, Any]
    checksum: str

    @property
    def snapshot_id(self) -> str:
        return self.checksum

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "igeval.snapshot/1",
            "snapshot_version": self.snapshot_version,
            "snapshot_id": self.snapshot_id,
            "components": {
                "normalization": copy.deepcopy(self.normalization),
                "alignment": copy.deepcopy(self.alignment),
                "structure": copy.deepcopy(self.structure),
            },
            "checksum": self.checksum,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "snapshot_version": self.snapshot_version,
            "components": {
                name: {
                    "version": self.to_dict()["components"][name]["version"],
                    "checksum": self.to_dict()["components"][name]["checksum"],
                }
                for name in COMPONENT_NAMES
            },
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvaluationSnapshot":
        if not isinstance(value, dict):
            raise DomainError(E_SNAPSHOT_CORRUPT, "snapshot must be an object", field="snapshot")
        if value.get("schema") != "igeval.snapshot/1":
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                "unsupported or missing snapshot schema",
                field="schema",
            )
        snapshot_version = value.get("snapshot_version")
        if (
            isinstance(snapshot_version, bool)
            or not isinstance(snapshot_version, int)
            or snapshot_version <= 0
        ):
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                "snapshot_version must be a positive integer",
                field="snapshot_version",
            )
        components = value.get("components")
        if not isinstance(components, dict):
            raise DomainError(E_SNAPSHOT_CORRUPT, "components must be an object", field="components")
        validated: dict[str, dict[str, Any]] = {}
        for name in COMPONENT_NAMES:
            component = components.get(name)
            if not isinstance(component, dict):
                raise DomainError(
                    E_SNAPSHOT_CORRUPT,
                    f"missing snapshot component: {name}",
                    field=f"components.{name}",
                )
            checksum = component.get("checksum")
            if not isinstance(checksum, str):
                raise DomainError(
                    E_SNAPSHOT_CORRUPT,
                    f"missing checksum for {name}",
                    field=f"components.{name}.checksum",
                )
            body = {key: copy.deepcopy(item) for key, item in component.items() if key != "checksum"}
            version = body.get("version")
            if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
                raise DomainError(
                    E_SNAPSHOT_CORRUPT,
                    f"invalid version for {name}",
                    field=f"components.{name}.version",
                )
            payload = {key: item for key, item in body.items() if key != "version"}
            expected = _component_checksum(name, payload)
            if checksum != expected:
                raise DomainError(
                    E_SNAPSHOT_CORRUPT,
                    f"checksum mismatch for {name}",
                    field=f"components.{name}.checksum",
                    details={"expected": expected, "actual": checksum},
                )
            validated[name] = {
                **copy.deepcopy(payload),
                "version": version,
                "checksum": checksum,
            }
        _validate_snapshot_values(
            validated["normalization"],
            validated["alignment"],
            validated["structure"],
        )

        checksum = value.get("checksum")
        expected_snapshot = _snapshot_checksum(validated, snapshot_version)
        if checksum != expected_snapshot:
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                "snapshot checksum mismatch",
                field="checksum",
                details={"expected": expected_snapshot, "actual": checksum},
            )
        snapshot_id = value.get("snapshot_id")
        if snapshot_id is not None and snapshot_id != checksum:
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                "snapshot_id does not match checksum",
                field="snapshot_id",
            )
        return cls(
            snapshot_version=snapshot_version,
            normalization=validated["normalization"],
            alignment=validated["alignment"],
            structure=validated["structure"],
            checksum=expected_snapshot,
        )


def make_snapshot(
    *,
    snapshot_version: int = 1,
    normalization_version: int = 1,
    alignment_version: int = 1,
    structure_version: int = 1,
    normalization: dict[str, Any] | None = None,
    alignment: dict[str, Any] | None = None,
    structure: dict[str, Any] | None = None,
) -> EvaluationSnapshot:
    normalization_payload = deep_copy_json(normalization or DEFAULT_NORMALIZATION)
    alignment_payload = deep_copy_json(alignment or DEFAULT_ALIGNMENT)
    structure_payload = deep_copy_json(structure or DEFAULT_STRUCTURE)
    components = {
        "normalization": make_component(
            "normalization", normalization_version, normalization_payload
        ),
        "alignment": make_component("alignment", alignment_version, alignment_payload),
        "structure": make_component("structure", structure_version, structure_payload),
    }
    _validate_snapshot_values(
        components["normalization"],
        components["alignment"],
        components["structure"],
    )
    checksum = _snapshot_checksum(components, snapshot_version)
    return EvaluationSnapshot(
        snapshot_version=snapshot_version,
        normalization=components["normalization"],
        alignment=components["alignment"],
        structure=components["structure"],
        checksum=checksum,
    )


def evolve_snapshot(
    current: EvaluationSnapshot,
    *,
    snapshot_version: int | None = None,
    normalization_version: int | None = None,
    alignment_version: int | None = None,
    structure_version: int | None = None,
    normalization_changes: dict[str, Any] | None = None,
    alignment_changes: dict[str, Any] | None = None,
    structure_changes: dict[str, Any] | None = None,
) -> EvaluationSnapshot:
    """Create a new complete snapshot; this is never an incremental patch."""
    normalization = {
        key: copy.deepcopy(value)
        for key, value in current.normalization.items()
        if key not in {"version", "checksum"}
    }
    alignment = {
        key: copy.deepcopy(value)
        for key, value in current.alignment.items()
        if key not in {"version", "checksum"}
    }
    structure = {
        key: copy.deepcopy(value)
        for key, value in current.structure.items()
        if key not in {"version", "checksum"}
    }
    normalization.update(deep_copy_json(normalization_changes or {}))
    alignment.update(deep_copy_json(alignment_changes or {}))
    structure.update(deep_copy_json(structure_changes or {}))
    return make_snapshot(
        snapshot_version=snapshot_version
        if snapshot_version is not None
        else current.snapshot_version + 1,
        normalization_version=normalization_version
        if normalization_version is not None
        else current.normalization["version"],
        alignment_version=alignment_version
        if alignment_version is not None
        else current.alignment["version"],
        structure_version=structure_version
        if structure_version is not None
        else current.structure["version"],
        normalization=normalization,
        alignment=alignment,
        structure=structure,
    )


class SnapshotStore:
    """Append-only versioned snapshot files plus an atomically replaced CURRENT."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.directory = self.root / "snapshots"
        self.current_path = self.directory / "CURRENT"
        self.directory.mkdir(parents=True, exist_ok=True)

    def _filename(self, snapshot: EvaluationSnapshot) -> str:
        return (
            f"snapshot-v{snapshot.snapshot_version:016d}-"
            f"{snapshot.checksum}.json"
        )

    def save(self, snapshot: EvaluationSnapshot) -> Path:
        path = self.directory / self._filename(snapshot)
        atomic_write_bytes(path, canonical_bytes(snapshot.to_dict()))
        return path

    def set_current(self, snapshot: EvaluationSnapshot) -> None:
        path = self.save(snapshot)
        atomic_write_bytes(self.current_path, (path.name + "\n").encode("utf-8"))

    def load_file(self, path: str | Path) -> EvaluationSnapshot:
        source = Path(path)
        try:
            payload = read_json(source)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                f"cannot read snapshot file: {exc}",
                field="snapshot_file",
                details={"path": str(source)},
            ) from exc
        return EvaluationSnapshot.from_dict(payload)

    def load_current(self) -> EvaluationSnapshot | None:
        if not self.current_path.exists():
            return None
        try:
            filename = self.current_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                f"cannot read CURRENT: {exc}",
                field="CURRENT",
            ) from exc
        if not filename or Path(filename).name != filename:
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                "CURRENT does not contain a plain snapshot filename",
                field="CURRENT",
            )
        path = self.directory / filename
        if not path.exists():
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                "CURRENT points to a missing snapshot",
                field="CURRENT",
                details={"filename": filename},
            )
        return self.load_file(path)

    def latest_version(self) -> int:
        current = self.load_current()
        return current.snapshot_version if current is not None else 0

    def list_files(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("snapshot-v*.json")):
            match = _VERSION_FILE_PATTERN.match(path.name)
            if not match:
                continue
            entries.append(
                {
                    "path": str(path),
                    "filename": path.name,
                    "snapshot_version": int(match.group(1)),
                    "checksum": match.group(2),
                }
            )
        return entries

    def current_summary(self) -> dict[str, Any] | None:
        current = self.load_current()
        if current is None:
            return None
        return {
            "snapshot": current.summary(),
            "path": str(self.current_path),
            "files": self.list_files(),
        }


def canonical_snapshot(snapshot: EvaluationSnapshot) -> str:
    return canonical_json(snapshot.to_dict())

