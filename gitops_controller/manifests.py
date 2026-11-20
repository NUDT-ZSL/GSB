"""Strict readers for the legacy v1 and layered v2 manifest formats."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import GitOpsError
from .project import Project
from .util import (
    is_int,
    read_json_file,
    require_app_name,
    require_int,
    require_list,
    require_mapping,
    require_version,
    sha256_bytes,
    sha256_file,
    validate_dependencies,
)


@dataclass(frozen=True)
class V1App:
    name: str
    image_version: str
    replicas: int
    effective_seconds: int
    params: dict[str, Any]
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class V1Manifest:
    source_id: str
    logical_clock_ms: int
    generation: int
    environment_scope: tuple[str, ...] | None
    apps: dict[str, V1App]
    raw: dict[str, Any]


@dataclass(frozen=True)
class V2App:
    name: str
    image_version: str
    replicas: int
    resources: dict[str, Any]
    depends_on: tuple[str, ...]
    effective_at_ms: int
    generation: int
    tombstone: bool


@dataclass(frozen=True)
class V2Shard:
    environment: str
    source_id: str
    logical_clock_ms: int
    generation: int
    shard_file: str
    apps: dict[str, V2App]


def _first_present(
    data: dict[str, Any],
    fields: tuple[str, ...],
    *,
    context: str,
) -> Any:
    for field in fields:
        if field in data:
            return data[field]
    raise GitOpsError(
        "E_BAD_MANIFEST",
        f"missing required field {fields[0]}",
        {"context": context, "accepted_fields": list(fields)},
    )


def _dependencies(
    app_data: dict[str, Any],
    params: dict[str, Any],
    *,
    context: str,
) -> tuple[str, ...]:
    direct = app_data.get("depends_on", [])
    from_params = params.get("depends_on", [])
    if not isinstance(direct, list) or not isinstance(from_params, list):
        raise GitOpsError(
            "E_BAD_MANIFEST",
            "depends_on must be an array",
            {"context": context},
        )
    values = direct + from_params
    if not all(isinstance(value, str) for value in values):
        raise GitOpsError(
            "E_BAD_MANIFEST",
            "depends_on entries must be application names",
            {"context": context},
        )
    return tuple(sorted(set(values)))


def load_v1(project: Project) -> V1Manifest:
    raw = read_json_file(project.v1_manifest)
    data = require_mapping(raw, "v1 manifest", context=str(project.v1_manifest))
    context = str(project.v1_manifest)
    if data.get("format") != "v1":
        raise GitOpsError(
            "E_BAD_MANIFEST",
            "v1 manifest format must be 'v1'",
            {"path": context, "format": data.get("format")},
        )
    logical_clock_ms = require_int(
        _first_present(data, ("logical_clock_ms", "logical_clock"), context=context),
        "logical_clock_ms",
        minimum=0,
        context=context,
    )
    generation = require_int(
        _first_present(
            data,
            ("manifest_generation", "generation", "manifest_version"),
            context=context,
        ),
        "manifest_generation",
        minimum=1,
        context=context,
    )

    scope: tuple[str, ...] | None
    if "environment" in data:
        environment = data["environment"]
        if not isinstance(environment, str) or not environment:
            raise GitOpsError(
                "E_BAD_MANIFEST",
                "environment must be a non-empty string",
                {"context": context},
            )
        if environment not in project.environments:
            raise GitOpsError(
                "E_BAD_MANIFEST",
                "v1 manifest references an unknown environment",
                {"environment": environment, "context": context},
            )
        scope = (environment,)
    elif "environments" in data:
        environments = require_list(data["environments"], "environments", context=context)
        if (
            not environments
            or not all(isinstance(item, str) and item for item in environments)
            or len(set(environments)) != len(environments)
        ):
            raise GitOpsError(
                "E_BAD_MANIFEST",
                "v1 environments must be unique non-empty strings",
                {"context": context},
            )
        unknown = sorted(set(environments) - set(project.environments))
        if unknown:
            raise GitOpsError(
                "E_BAD_MANIFEST",
                "v1 manifest references unknown environments",
                {"context": context, "environments": unknown},
            )
        scope = tuple(environments)
    else:
        scope = None

    applications = require_mapping(
        data.get("applications"),
        "applications",
        context=context,
    )
    apps: dict[str, V1App] = {}
    for raw_name in sorted(applications):
        name = require_app_name(raw_name, context=context)
        if name in apps:
            raise GitOpsError(
                "E_BAD_MANIFEST",
                "duplicate v1 application name",
                {"app": name, "context": context},
            )
        app_context = f"{context}:applications.{name}"
        app_data = require_mapping(applications[raw_name], "application", context=app_context)
        image_version = require_version(
            _first_present(
                app_data,
                ("image_version", "image", "version"),
                context=app_context,
            ),
            context=app_context,
        )
        replicas = require_int(
            app_data.get("replicas"),
            "replicas",
            minimum=1,
            context=app_context,
        )
        effective_seconds = require_int(
            _first_present(
                app_data,
                ("effective_seconds", "relative_effective_seconds"),
                context=app_context,
            ),
            "effective_seconds",
            minimum=0,
            context=app_context,
        )
        params_raw = app_data.get("params", {})
        params = require_mapping(params_raw, "params", context=app_context)
        depends_on = _dependencies(app_data, params, context=app_context)
        apps[name] = V1App(
            name=name,
            image_version=image_version,
            replicas=replicas,
            effective_seconds=effective_seconds,
            params=params,
            depends_on=depends_on,
        )
    if not apps:
        raise GitOpsError(
            "E_BAD_MANIFEST",
            "v1 manifest has no applications",
            {"context": context},
        )

    dependencies = {app.name: list(app.depends_on) for app in apps.values()}
    validate_dependencies(sorted(apps), dependencies, context=context)
    source_id = sha256_file(project.v1_manifest)
    return V1Manifest(
        source_id=source_id,
        logical_clock_ms=logical_clock_ms,
        generation=generation,
        environment_scope=scope,
        apps=apps,
        raw=data,
    )


def _resolve_shard(shards_dir: Path, shard_file: Any, *, context: str) -> Path:
    if not isinstance(shard_file, str) or not shard_file:
        raise GitOpsError(
            "E_BAD_MANIFEST",
            "shard_file must be a non-empty relative path",
            {"context": context},
        )
    candidate = (shards_dir / shard_file).resolve()
    try:
        candidate.relative_to(shards_dir.resolve())
    except ValueError as exc:
        raise GitOpsError(
            "E_BAD_MANIFEST",
            "shard_file escapes the shards directory",
            {"context": context, "shard_file": shard_file},
        ) from exc
    return candidate


def _load_v2_shard(
    shards_dir: Path,
    environment: str,
    shard_file: str,
    expected_hash: str,
    expected_generation: int,
    expected_clock_ms: int,
) -> V2Shard:
    path = _resolve_shard(shards_dir, shard_file, context=f"v2 metadata:{environment}")
    if not path.is_file():
        raise GitOpsError(
            "E_NOT_FOUND",
            "v2 shard file not found",
            {"environment": environment, "path": str(path)},
        )
    actual_hash = sha256_file(path)
    if actual_hash != expected_hash:
        raise GitOpsError(
            "E_BAD_MANIFEST",
            "v2 shard hash does not match SQLite metadata",
            {
                "environment": environment,
                "expected_sha256": expected_hash,
                "actual_sha256": actual_hash,
                "shard_file": shard_file,
            },
        )
    raw = read_json_file(path)
    data = require_mapping(raw, "v2 shard", context=str(path))
    context = str(path)
    if data.get("format") != "v2":
        raise GitOpsError(
            "E_BAD_MANIFEST",
            "v2 shard format must be 'v2'",
            {"path": context},
        )
    if data.get("environment") != environment:
        raise GitOpsError(
            "E_BAD_MANIFEST",
            "v2 shard environment does not match metadata",
            {"expected": environment, "actual": data.get("environment")},
        )
    generation = require_int(
        data.get("generation"),
        "generation",
        minimum=1,
        context=context,
    )
    if generation != expected_generation:
        raise GitOpsError(
            "E_BAD_MANIFEST",
            "v2 shard generation does not match metadata",
            {"environment": environment, "expected": expected_generation, "actual": generation},
        )
    clock_ms = require_int(
        data.get("logical_clock_ms"),
        "logical_clock_ms",
        minimum=0,
        context=context,
    )
    if clock_ms != expected_clock_ms:
        raise GitOpsError(
            "E_BAD_MANIFEST",
            "v2 shard logical clock does not match metadata",
            {"environment": environment, "expected": expected_clock_ms, "actual": clock_ms},
        )

    applications = require_list(data.get("applications"), "applications", context=context)
    apps: dict[str, V2App] = {}
    for raw_app in applications:
        app_data = require_mapping(raw_app, "application", context=context)
        name = require_app_name(app_data.get("name"), context=context)
        if name in apps:
            raise GitOpsError(
                "E_BAD_MANIFEST",
                "duplicate v2 application name",
                {"app": name, "environment": environment},
            )
        app_context = f"{context}:applications.{name}"
        image_version = require_version(app_data.get("image_version"), context=app_context)
        replicas = require_int(
            app_data.get("replicas"),
            "replicas",
            minimum=1,
            context=app_context,
        )
        resources = require_mapping(
            app_data.get("resources", {}),
            "resources",
            context=app_context,
        )
        depends_on_raw = require_list(
            app_data.get("depends_on", []),
            "depends_on",
            context=app_context,
        )
        if not all(isinstance(value, str) for value in depends_on_raw):
            raise GitOpsError(
                "E_BAD_MANIFEST",
                "depends_on entries must be application names",
                {"context": app_context},
            )
        effective_at_ms = require_int(
            app_data.get("effective_at_ms"),
            "effective_at_ms",
            minimum=0,
            context=app_context,
        )
        app_generation = require_int(
            app_data.get("generation"),
            "generation",
            minimum=1,
            maximum=generation,
            context=app_context,
        )
        tombstone = app_data.get("tombstone", False)
        if not isinstance(tombstone, bool):
            raise GitOpsError(
                "E_BAD_MANIFEST",
                "tombstone must be a boolean",
                {"context": app_context},
            )
        apps[name] = V2App(
            name=name,
            image_version=image_version,
            replicas=replicas,
            resources=dict(resources),
            depends_on=tuple(sorted(set(depends_on_raw))),
            effective_at_ms=effective_at_ms,
            generation=app_generation,
            tombstone=tombstone,
        )
    if not apps:
        raise GitOpsError(
            "E_BAD_MANIFEST",
            "v2 shard has no applications",
            {"environment": environment},
        )
    dependencies = {app.name: list(app.depends_on) for app in apps.values()}
    validate_dependencies(sorted(apps), dependencies, context=context)
    return V2Shard(
        environment=environment,
        source_id=sha256_bytes(path.read_bytes()),
        logical_clock_ms=clock_ms,
        generation=generation,
        shard_file=shard_file,
        apps=apps,
    )


def load_v2(project: Project) -> dict[str, V2Shard]:
    if not project.v2_metadata.is_file():
        raise GitOpsError(
            "E_NOT_FOUND",
            "v2 metadata database not found",
            {"path": str(project.v2_metadata)},
        )
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(project.v2_metadata), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "manifest_revisions" not in tables:
            raise GitOpsError(
                "E_BAD_MANIFEST",
                "v2 metadata is missing manifest_revisions",
                {"path": str(project.v2_metadata)},
            )
        rows = connection.execute(
            """
            SELECT environment, sequence, generation, logical_clock_ms,
                   shard_file, shard_sha256
            FROM manifest_revisions
            ORDER BY environment, sequence
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise GitOpsError(
            "E_BAD_MANIFEST",
            "cannot read v2 metadata database",
            {"path": str(project.v2_metadata), "reason": str(exc)},
        ) from exc
    finally:
        if connection is not None:
            connection.close()

    rows_by_env: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        rows_by_env.setdefault(row["environment"], []).append(row)
    missing = sorted(set(project.environments) - set(rows_by_env))
    unknown = sorted(set(rows_by_env) - set(project.environments))
    if missing or unknown:
        raise GitOpsError(
            "E_BAD_MANIFEST",
            "v2 metadata environments do not match the project",
            {"missing": missing, "unknown": unknown},
        )

    shards: dict[str, V2Shard] = {}
    for environment in project.environments:
        env_rows = rows_by_env[environment]
        previous_sequence: int | None = None
        previous_generation: int | None = None
        for row in env_rows:
            sequence = require_int(
                row["sequence"],
                "sequence",
                minimum=1,
                context=f"v2 metadata:{environment}",
            )
            generation = require_int(
                row["generation"],
                "generation",
                minimum=1,
                context=f"v2 metadata:{environment}",
            )
            clock_ms = require_int(
                row["logical_clock_ms"],
                "logical_clock_ms",
                minimum=0,
                context=f"v2 metadata:{environment}",
            )
            if previous_sequence is not None and sequence <= previous_sequence:
                raise GitOpsError(
                    "E_BAD_MANIFEST",
                    "v2 metadata sequence is not strictly increasing",
                    {
                        "environment": environment,
                        "previous": previous_sequence,
                        "current": sequence,
                    },
                )
            if previous_generation is not None and generation <= previous_generation:
                raise GitOpsError(
                    "E_BAD_MANIFEST",
                    "v2 manifest generation is not strictly increasing",
                    {
                        "environment": environment,
                        "previous": previous_generation,
                        "current": generation,
                    },
                )
            shard_file = row["shard_file"]
            shard_hash = row["shard_sha256"]
            if not isinstance(shard_file, str) or not isinstance(shard_hash, str):
                raise GitOpsError(
                    "E_BAD_MANIFEST",
                    "v2 metadata shard fields must be strings",
                    {"environment": environment},
                )
            loaded = _load_v2_shard(
                project.v2_shards_dir,
                environment,
                shard_file,
                shard_hash,
                generation,
                clock_ms,
            )
            shards[environment] = loaded
            previous_sequence = sequence
            previous_generation = generation
    return shards


def load_sources(project: Project) -> tuple[V1Manifest, dict[str, V2Shard]]:
    return load_v1(project), load_v2(project)
