"""Log package descriptor loading and path resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .clock import ClockSpec
from .errors import E_NOT_FOUND, E_SCHEMA, WorkbenchError
from .jsonio import read_json
from .paths import Paths


@dataclass
class PackageBundle:
    descriptor_path: Path
    descriptor: dict[str, Any]
    base_dir: Path
    clock: ClockSpec
    clock_path: Path
    v1: dict[str, Any] | None
    v1_path: Path | None
    v2_manifest: dict[str, Any]
    v2_manifest_path: Path
    v1_table: dict[str, Any] | None
    v2_table: dict[str, Any] | None
    test_plan: dict[str, Any] | None

    @property
    def package_id(self) -> str:
        return str(self.descriptor["package_id"])

    @property
    def manifest_version(self) -> int:
        value = self.descriptor["manifest_version"]
        if not isinstance(value, int) or isinstance(value, bool):
            raise WorkbenchError(E_SCHEMA, "package manifest_version must be an integer")
        return value


def _require_dict(data: Any, code: str = E_SCHEMA, message: str = "JSON document must be an object") -> dict[str, Any]:
    if not isinstance(data, dict):
        raise WorkbenchError(code, message)
    return data


def load_package(paths: Paths, descriptor: str | Path | None = None) -> PackageBundle:
    descriptor_path = Path(descriptor) if descriptor else paths.current_package
    if not descriptor_path.is_absolute():
        descriptor_path = (paths.root / descriptor_path).resolve()
    if not descriptor_path.exists():
        raise WorkbenchError(E_NOT_FOUND, "package descriptor not found", {"path": str(descriptor_path)})
    data = _require_dict(read_json(descriptor_path), message="package descriptor must be an object")
    if data.get("format") != "proxy-log-package":
        raise WorkbenchError(E_SCHEMA, "unsupported package format", {"format": data.get("format")})

    base_dir = descriptor_path.parent

    def resolve(key: str, *, required: bool = True) -> Path | None:
        raw = data.get(key)
        if raw is None:
            if required:
                raise WorkbenchError(E_SCHEMA, f"package descriptor missing {key}")
            return None
        candidate = Path(str(raw))
        if not candidate.is_absolute():
            candidate = (base_dir / candidate).resolve()
        if required and not candidate.exists():
            raise WorkbenchError(E_NOT_FOUND, f"package file not found: {key}", {"path": str(candidate)})
        return candidate

    clock_path = resolve("clock_file")
    assert clock_path is not None
    clock = ClockSpec.load(clock_path)

    v1_path = resolve("v1_file", required=False)
    v1 = _require_dict(read_json(v1_path), message="v1 log must be an object") if v1_path else None

    v2_manifest_path = resolve("v2_manifest")
    assert v2_manifest_path is not None
    v2_manifest = _require_dict(read_json(v2_manifest_path), message="v2 manifest must be an object")

    v1_table_path = resolve("v1_table", required=False)
    v2_table_path = resolve("v2_table", required=False)
    plan_path = resolve("test_plan", required=False)
    return PackageBundle(
        descriptor_path=descriptor_path,
        descriptor=data,
        base_dir=base_dir,
        clock=clock,
        clock_path=clock_path,
        v1=v1,
        v1_path=v1_path,
        v2_manifest=v2_manifest,
        v2_manifest_path=v2_manifest_path,
        v1_table=_require_dict(read_json(v1_table_path)) if v1_table_path else None,
        v2_table=_require_dict(read_json(v2_table_path)) if v2_table_path else None,
        test_plan=_require_dict(read_json(plan_path)) if plan_path else None,
    )
