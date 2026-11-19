"""Source-set loading and deterministic package descriptor handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .errors import CACHE_MANIFEST_INVALID, CacheError
from .models import LoadedDataset
from .util import (
    load_json_file,
    object_digest,
    require_exact_keys,
    require_int,
    require_list,
    require_object,
    require_str,
)
from .v1 import parse_v1
from .v2 import parse_v2


def load_dataset(
    v1_paths: Iterable[str | Path] = (),
    v2_paths: Iterable[str | Path] = (),
    *,
    now_ms: int | None = None,
) -> LoadedDataset:
    paths: list[tuple[str, Path]] = []
    for raw in v1_paths:
        paths.append(("v1", Path(raw)))
    for raw in v2_paths:
        paths.append(("v2", Path(raw)))

    unique: dict[tuple[str, Path], None] = {}
    for kind, path in paths:
        unique[(kind, path.resolve())] = None

    dataset = LoadedDataset()
    source_ids: set[str] = set()
    for kind, path in sorted(unique, key=lambda item: (item[0], item[1].as_posix())):
        parsed = (
            parse_v1(path, time_reference_ms=now_ms)
            if kind == "v1"
            else parse_v2(path)
        )
        for source in parsed.sources:
            if source.source_id in source_ids:
                raise CacheError(
                    CACHE_MANIFEST_INVALID,
                    "duplicate source id in the input cache set",
                    fields={"source_id": source.source_id, "path": str(path)},
                )
            source_ids.add(source.source_id)
        dataset.add(parsed)
    if not dataset.sources:
        raise CacheError(
            CACHE_MANIFEST_INVALID,
            "at least one v1 or v2 cache source is required",
            fields={},
        )
    return dataset


def dataset_input_hash(dataset: LoadedDataset) -> str:
    return object_digest(dataset.semantic_descriptor())


def load_dataset_for_clock(
    v1_paths: Iterable[str | Path] = (),
    v2_paths: Iterable[str | Path] = (),
    *,
    now_ms: int | None = None,
) -> tuple[LoadedDataset, int]:
    v1_paths = list(v1_paths)
    v2_paths = list(v2_paths)
    if now_ms is None:
        probe = load_dataset(v1_paths=v1_paths, v2_paths=v2_paths)
        effective_now = probe.default_now_ms()
        dataset = load_dataset(
            v1_paths=v1_paths,
            v2_paths=v2_paths,
            now_ms=effective_now,
        )
        return dataset, effective_now
    return load_dataset(v1_paths=v1_paths, v2_paths=v2_paths, now_ms=now_ms), now_ms


def parse_package(
    path: str | Path,
    *,
    now_ms: int | None = None,
) -> tuple[dict[str, Any], LoadedDataset]:
    package_path = Path(path)
    if package_path.is_dir():
        package_path = package_path / "package.json"
    package, _ = load_json_file(package_path)
    require_exact_keys(
        package,
        {
            "format",
            "package_id",
            "manifest_version",
            "cache_generation_floor",
            "logical_clock_ms",
            "sources",
        },
        optional={"description"},
        field="cache_package",
    )
    if package["format"] != "cacheman-package-v1":
        raise CacheError(
            CACHE_MANIFEST_INVALID,
            "cache package has an unexpected format marker",
            fields={"path": str(package_path), "format": package["format"]},
        )
    package_id = require_str(package["package_id"], "package_id")
    manifest_version = require_int(package["manifest_version"], "manifest_version", minimum=1)
    generation_floor = require_int(
        package["cache_generation_floor"],
        "cache_generation_floor",
        minimum=0,
    )
    logical_clock_ms = require_int(package["logical_clock_ms"], "logical_clock_ms", minimum=0)
    raw_sources = require_list(package["sources"], "sources")
    v1_paths: list[Path] = []
    v2_paths: list[Path] = []
    package_root = package_path.resolve().parent
    for index, raw_source in enumerate(raw_sources):
        source = require_object(raw_source, f"sources[{index}]")
        require_exact_keys(source, {"type", "path"}, field=f"sources[{index}]")
        kind = require_str(source["type"], f"sources[{index}].type")
        raw_path = require_str(source["path"], f"sources[{index}].path")
        source_path = Path(raw_path)
        if not source_path.is_absolute():
            source_path = package_root / source_path
        if kind == "v1":
            v1_paths.append(source_path)
        elif kind == "v2":
            v2_paths.append(source_path)
        else:
            raise CacheError(
                CACHE_MANIFEST_INVALID,
                "cache package source type must be v1 or v2",
                fields={"path": str(package_path), "index": index, "type": kind},
            )
    if not raw_sources:
        raise CacheError(
            CACHE_MANIFEST_INVALID,
            "cache package must declare at least one source",
            fields={"path": str(package_path)},
        )
    dataset = load_dataset(
        v1_paths=v1_paths,
        v2_paths=v2_paths,
        now_ms=logical_clock_ms if now_ms is None else now_ms,
    )
    for source in dataset.sources:
        if source.manifest_version > manifest_version:
            raise CacheError(
                CACHE_MANIFEST_INVALID,
                "cache package manifest version is lower than one of its sources",
                fields={
                    "path": str(package_path),
                    "package_manifest_version": manifest_version,
                    "source_id": source.source_id,
                    "source_manifest_version": source.manifest_version,
                },
            )
    if dataset.sources:
        v2_sources = [source for source in dataset.sources if source.source_version == 2]
        if v2_sources:
            actual_floor = min(
                source.generation_min
                for source in v2_sources
                if source.generation_min is not None
            )
            if generation_floor > actual_floor:
                raise CacheError(
                    CACHE_MANIFEST_INVALID,
                    "cache package generation floor is above a source generation",
                    fields={
                        "path": str(package_path),
                        "package_generation_floor": generation_floor,
                        "source_generation_min": actual_floor,
                    },
                )
        elif generation_floor != 0:
            raise CacheError(
                CACHE_MANIFEST_INVALID,
                "a package without v2 sources must use generation floor 0",
                fields={
                    "path": str(package_path),
                    "package_generation_floor": generation_floor,
                },
            )
    descriptor = {
        "format": "cacheman-package-v1",
        "package_id": package_id,
        "manifest_version": manifest_version,
        "cache_generation_floor": generation_floor,
        "logical_clock_ms": logical_clock_ms,
        "description": package.get("description"),
        "sources": [
            {
                "type": "v1" if source.source_version == 1 else "v2",
                "source_id": source.source_id,
            }
            for source in sorted(dataset.sources, key=lambda item: item.source_id)
        ],
    }
    return descriptor, dataset
