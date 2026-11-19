"""Parser and validator for the single-file v1 cache manifest."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import (
    CACHE_ARTIFACT_MISSING,
    CACHE_DIGEST_MISMATCH,
    CACHE_MANIFEST_INVALID,
    CacheError,
)
from .models import Candidate, LoadedDataset, SourceInfo
from .util import (
    artifact_name,
    canonical_json,
    canonical_task_key,
    digest_hex,
    display_path,
    load_json_file,
    normalize_sha256,
    object_digest,
    require_exact_keys,
    require_int,
    require_list,
    require_object,
    require_str,
    resolve_content_path,
    sha256_bytes,
    sha256_text,
)


def _binding_error(
    message: str,
    *,
    path: Path,
    detail: dict[str, Any] | None = None,
) -> CacheError:
    fields = {"path": str(path)}
    if detail:
        fields.update(detail)
    return CacheError(CACHE_MANIFEST_INVALID, message, fields=fields)


def parse_v1(
    path: Path,
    *,
    time_reference_ms: int | None = None,
) -> LoadedDataset:
    manifest_path = Path(path)
    manifest, raw = load_json_file(manifest_path)
    root = manifest_path.resolve().parent

    require_exact_keys(
        manifest,
        {
            "format",
            "manifest_id",
            "manifest_version",
            "logical_clock_ms",
            "artifacts",
            "task_artifacts",
        },
        field="v1_manifest",
    )
    if manifest["format"] != "cache-v1":
        raise CacheError(
            CACHE_MANIFEST_INVALID,
            "v1 manifest has an unexpected format marker",
            fields={"path": str(manifest_path), "format": manifest["format"]},
        )
    manifest_id = require_str(manifest["manifest_id"], "manifest_id")
    manifest_version = require_int(manifest["manifest_version"], "manifest_version", minimum=1)
    logical_clock_ms = require_int(manifest["logical_clock_ms"], "logical_clock_ms", minimum=0)
    conversion_reference_ms = (
        logical_clock_ms
        if time_reference_ms is None
        else require_int(time_reference_ms, "injected_logical_clock_ms", minimum=0)
    )
    artifacts = require_list(manifest["artifacts"], "artifacts")
    task_artifacts_raw = require_object(manifest["task_artifacts"], "task_artifacts")

    seen: set[tuple[str, str]] = set()
    declared_map: dict[str, list[str]] = {}
    candidates: list[Candidate] = []

    for index, raw_entry in enumerate(artifacts):
        field = f"artifacts[{index}]"
        entry = require_object(raw_entry, field)
        require_exact_keys(
            entry,
            {
                "task",
                "artifact",
                "sha256",
                "relative_written_seconds",
                "size_bytes",
                "content_path",
            },
            field=field,
        )
        task_key = canonical_task_key(entry["task"], field=f"{field}.task")
        artifact = artifact_name(entry["artifact"], field=f"{field}.artifact")
        key = (task_key, artifact)
        if key in seen:
            raise _binding_error(
                "duplicate task/artifact declaration in v1 manifest",
                path=manifest_path,
                detail={"task_key": task_key, "artifact": artifact, "index": index},
            )
        seen.add(key)

        digest = normalize_sha256(entry["sha256"], field=f"{field}.sha256")
        relative_seconds = require_int(
            entry["relative_written_seconds"],
            f"{field}.relative_written_seconds",
            minimum=0,
        )
        size_bytes = require_int(entry["size_bytes"], f"{field}.size_bytes", minimum=0)
        content_path = require_str(entry["content_path"], f"{field}.content_path")
        resolved = resolve_content_path(root, content_path, field=f"{field}.content_path")
        try:
            content = resolved.read_bytes()
        except FileNotFoundError:
            raise CacheError(
                CACHE_ARTIFACT_MISSING,
                "v1 artifact content file is missing",
                fields={
                    "path": str(manifest_path),
                    "task_key": task_key,
                    "artifact": artifact,
                    "content_path": content_path,
                    "resolved_path": str(resolved),
                },
            )
        except OSError as exc:
            raise CacheError(
                CACHE_ARTIFACT_MISSING,
                "v1 artifact content file cannot be read",
                fields={
                    "path": str(manifest_path),
                    "task_key": task_key,
                    "artifact": artifact,
                    "content_path": content_path,
                    "reason": str(exc),
                },
            )

        actual_digest = sha256_bytes(content)
        expected_hex = digest_hex(digest)
        if actual_digest != expected_hex:
            raise CacheError(
                CACHE_DIGEST_MISMATCH,
                "v1 artifact bytes do not match the declared SHA-256 digest",
                fields={
                    "path": str(manifest_path),
                    "task_key": task_key,
                    "artifact": artifact,
                    "expected_sha256": digest,
                    "actual_sha256": "sha256:" + actual_digest,
                },
            )
        if len(content) != size_bytes:
            raise CacheError(
                CACHE_DIGEST_MISMATCH,
                "v1 artifact byte size does not match the declaration",
                fields={
                    "path": str(manifest_path),
                    "task_key": task_key,
                    "artifact": artifact,
                    "expected_size_bytes": size_bytes,
                    "actual_size_bytes": len(content),
                },
            )

        absolute_written_ms = conversion_reference_ms - relative_seconds * 1000
        if absolute_written_ms < 0:
            raise _binding_error(
                "v1 relative write time converts to a negative absolute timestamp",
                path=manifest_path,
                detail={
                    "task_key": task_key,
                    "artifact": artifact,
                    "logical_clock_ms": conversion_reference_ms,
                    "relative_written_seconds": relative_seconds,
                },
            )

        declared_map.setdefault(task_key, []).append(artifact)
        candidates.append(
            Candidate(
                task_key=task_key,
                artifact=artifact,
                original_digests=(digest,),
                size_bytes=size_bytes,
                absolute_written_ms=absolute_written_ms,
                source_version=1,
                source_id=f"v1:{manifest_id}",
                source_manifest_id=manifest_id,
                source_manifest_version=manifest_version,
                source_logical_clock_ms=logical_clock_ms,
                source_cache_generation=None,
                source_hit_tag=None,
                original_time={
                    "kind": "relative_seconds",
                    "value": relative_seconds,
                    "scale_ms_per_second": 1000,
                    "rounding": "floor",
                    "direction": "subtract_from_logical_clock",
                    "reference_logical_clock_ms": conversion_reference_ms,
                    "manifest_logical_clock_ms": logical_clock_ms,
                },
                content_path=content_path,
                source_root=display_path(Path(manifest_path).parent, Path.cwd()),
                resolved_content_path=resolved,
            )
        )

    normalized_map: dict[str, list[str]] = {}
    for raw_task, raw_names in task_artifacts_raw.items():
        task_key = canonical_task_key(raw_task, field="task_artifacts key")
        if task_key in normalized_map:
            raise _binding_error(
                "task_artifacts contains duplicate task keys after canonicalization",
                path=manifest_path,
                detail={"task_key": task_key},
            )
        names = require_list(raw_names, f"task_artifacts.{task_key}")
        normalized_names: list[str] = []
        for item_index, raw_name in enumerate(names):
            name = artifact_name(raw_name, field=f"task_artifacts.{task_key}[{item_index}]")
            if name in normalized_names:
                raise _binding_error(
                    "task_artifacts repeats an artifact name",
                    path=manifest_path,
                    detail={"task_key": task_key, "artifact": name},
                )
            normalized_names.append(name)
        normalized_map[task_key] = normalized_names

    expected_map = {
        task: sorted(names)
        for task, names in declared_map.items()
    }
    actual_map = {
        task: sorted(names)
        for task, names in normalized_map.items()
    }
    if expected_map != actual_map:
        missing_tasks = sorted(set(expected_map) - set(actual_map))
        extra_tasks = sorted(set(actual_map) - set(expected_map))
        mismatch_tasks = sorted(
            task
            for task in set(expected_map) & set(actual_map)
            if expected_map[task] != actual_map[task]
        )
        raise _binding_error(
            "the flat task_artifacts map is inconsistent with artifact declarations",
            path=manifest_path,
            detail={
                "missing_tasks": missing_tasks,
                "unexpected_tasks": extra_tasks,
                "mismatched_tasks": mismatch_tasks,
            },
        )

    source = SourceInfo(
        source_id=f"v1:{manifest_id}",
        source_version=1,
        manifest_id=manifest_id,
        manifest_version=manifest_version,
        logical_clock_ms=logical_clock_ms,
        source_root=display_path(Path(manifest_path).parent, Path.cwd()),
        source_hash=object_digest(
            {
                "type": "cache-v1",
                "manifest_id": manifest_id,
                "manifest_version": manifest_version,
                "manifest_logical_clock_ms": logical_clock_ms,
                "time_reference_ms": conversion_reference_ms,
                "manifest_sha256": "sha256:" + sha256_bytes(raw),
            }
        ),
        descriptor={
            "type": "cache-v1",
            "manifest_id": manifest_id,
            "manifest_version": manifest_version,
            "logical_clock_ms": logical_clock_ms,
            "time_reference_ms": conversion_reference_ms,
            "manifest_sha256": "sha256:" + sha256_bytes(raw),
            "manifest_canonical_sha256": "sha256:" + sha256_text(canonical_json(manifest)),
            "artifact_count": len(candidates),
        },
    )
    return LoadedDataset(candidates=candidates, sources=[source])
