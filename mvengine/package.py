"""Top-level versioned package loader used for load and replace operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .definitions import ParsedV1, ParsedV2, parse_v1, parse_v2
from .errors import fail
from .logs import LogPackage, parse_logs, validate_log_against_index
from .merge import merge_definitions
from .models import UnifiedIndex
from .util import (
    digest_file,
    read_json,
    require_dict,
    require_int,
    require_str,
    safe_child,
)


@dataclass(frozen=True)
class DefinitionPackage:
    root: Path
    manifest_path: Path
    package_version: int
    view_generation_floor: int
    logical_clock_ms: int
    definition_digest: str
    file_hashes: dict[str, str]
    v1: ParsedV1
    v2: ParsedV2
    index: UnifiedIndex
    logs: LogPackage | None
    snapshots_path: Path | None

    @property
    def log_digest(self) -> str | None:
        return None if self.logs is None else self.logs.log_digest


def load_package(path: Path) -> DefinitionPackage:
    manifest_path = path / "package.json" if path.is_dir() else path
    root = manifest_path.parent
    raw = read_json(manifest_path, code="E_PACKAGE_INVALID")
    manifest = require_dict(raw, "package manifest", code="E_PACKAGE_INVALID")
    if manifest.get("format") != "mv-package-v1":
        raise fail(
            "E_PACKAGE_INVALID",
            "package manifest must declare format=mv-package-v1",
            path=str(manifest_path),
        )
    package_version = require_int(
        manifest.get("package_version"),
        "package_version",
        minimum=0,
        code="E_PACKAGE_INVALID",
    )
    generation_floor = require_int(
        manifest.get("view_generation_floor"),
        "view_generation_floor",
        minimum=0,
        code="E_PACKAGE_INVALID",
    )
    logical_clock_ms = require_int(
        manifest.get("logical_clock_ms"),
        "logical_clock_ms",
        minimum=0,
        code="E_PACKAGE_INVALID",
    )
    declared_digest = require_str(
        manifest.get("definition_digest"),
        "definition_digest",
        code="E_PACKAGE_INVALID",
    )
    paths = require_dict(manifest.get("definition_paths"), "definition_paths", code="E_PACKAGE_INVALID")
    v1_rel = require_str(paths.get("v1"), "definition_paths.v1", code="E_PACKAGE_INVALID")
    v2_rel = require_str(paths.get("v2"), "definition_paths.v2", code="E_PACKAGE_INVALID")
    file_hashes_raw = require_dict(manifest.get("file_hashes"), "file_hashes", code="E_PACKAGE_INVALID")
    file_hashes: dict[str, str] = {}
    for relative, expected in file_hashes_raw.items():
        rel = require_str(relative, "file_hashes path", code="E_PACKAGE_INVALID")
        hash_value = require_str(expected, f"file_hashes[{rel}]", code="E_PACKAGE_INVALID")
        if len(hash_value) != 64 or any(char not in "0123456789abcdef" for char in hash_value):
            raise fail(
                "E_PACKAGE_INVALID",
                "package file hash must be a lowercase SHA-256 digest",
                file=rel,
            )
        candidate = safe_child(root, rel)
        if not candidate.is_file():
            raise fail(
                "E_INPUT_NOT_FOUND",
                "package file listed in file_hashes does not exist",
                file=rel,
            )
        actual = digest_file(candidate)
        if actual != hash_value:
            raise fail(
                "E_PACKAGE_HASH_MISMATCH",
                "package file hash does not match the manifest",
                file=rel,
                expected=hash_value,
                actual=actual,
            )
        file_hashes[rel] = actual

    v1_path = safe_child(root, v1_rel)
    v2_path = safe_child(root, v2_rel)
    parsed_v1 = parse_v1(v1_path)
    parsed_v2 = parse_v2(v2_path)
    if generation_floor != parsed_v2.view_generation_floor:
        raise fail(
            "E_PACKAGE_INVALID",
            "package view_generation_floor differs from the v2 manifest",
            package=generation_floor,
            v2=parsed_v2.view_generation_floor,
        )
    index = merge_definitions(parsed_v1, parsed_v2)
    if index.index_digest != declared_digest:
        raise fail(
            "E_PACKAGE_DIGEST_MISMATCH",
            "package definition_digest differs from the merged unified index",
            expected=declared_digest,
            actual=index.index_digest,
        )

    logs_path: Path | None = None
    logs: LogPackage | None = None
    if manifest.get("logs_path") is not None:
        logs_path = safe_child(root, require_str(manifest["logs_path"], "logs_path"))
        if logs_path.exists():
            logs = parse_logs(logs_path)
            validate_log_against_index(logs.events, index)
        else:
            raise fail(
                "E_INPUT_NOT_FOUND",
                "log path declared by the package does not exist",
                path=str(logs_path),
            )
    snapshots_path: Path | None = None
    if manifest.get("snapshots_path") is not None:
        snapshots_path = safe_child(
            root,
            require_str(manifest["snapshots_path"], "snapshots_path"),
        )
        if not snapshots_path.exists():
            raise fail(
                "E_INPUT_NOT_FOUND",
                "snapshot path declared by the package does not exist",
                path=str(snapshots_path),
            )
    return DefinitionPackage(
        root=root,
        manifest_path=manifest_path,
        package_version=package_version,
        view_generation_floor=generation_floor,
        logical_clock_ms=logical_clock_ms,
        definition_digest=declared_digest,
        file_hashes=file_hashes,
        v1=parsed_v1,
        v2=parsed_v2,
        index=index,
        logs=logs,
        snapshots_path=snapshots_path,
    )

