"""Project configuration and path resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import GitOpsError
from .util import is_int, read_json_file, require_int, require_mapping


@dataclass(frozen=True)
class Project:
    config_path: Path
    root: Path
    name: str
    environments: tuple[str, ...]
    v1_manifest: Path
    v2_metadata: Path
    v2_shards_dir: Path
    cluster_state: Path
    cluster_lock: Path
    runtime_db: Path
    initial_clock_ms: int
    operation_cost_ms: int
    default_budget_ms: int
    clock_tolerance_ms: int

    @classmethod
    def load(cls, config_path: Path) -> "Project":
        config_path = config_path.expanduser().resolve()
        raw = read_json_file(config_path, error_code="E_BAD_CONFIG")
        data = require_mapping(raw, "project", context=str(config_path))
        if data.get("format") != "gitops-project-v1":
            raise GitOpsError(
                "E_BAD_CONFIG",
                "project format must be gitops-project-v1",
                {"path": str(config_path)},
            )
        root = config_path.parent

        def local_path(value: Any, field: str) -> Path:
            if not isinstance(value, str) or not value:
                raise GitOpsError(
                    "E_BAD_CONFIG",
                    f"{field} must be a non-empty relative path",
                    {"field": field},
                )
            candidate = Path(value)
            if candidate.is_absolute():
                raise GitOpsError(
                    "E_BAD_CONFIG",
                    f"{field} must be relative to the project file",
                    {"field": field},
                )
            resolved = (root / candidate).resolve()
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise GitOpsError(
                    "E_BAD_CONFIG",
                    f"{field} escapes the project directory",
                    {"field": field},
                ) from exc
            return resolved

        environments_raw = data.get("environments")
        if (
            not isinstance(environments_raw, list)
            or not environments_raw
            or not all(isinstance(item, str) and item for item in environments_raw)
            or len(set(environments_raw)) != len(environments_raw)
        ):
            raise GitOpsError(
                "E_BAD_CONFIG",
                "environments must be a non-empty array of unique names",
                {"path": str(config_path)},
            )
        sources = require_mapping(
            data.get("sources"),
            "sources",
            context=str(config_path),
        )
        v2 = require_mapping(
            sources.get("v2"),
            "sources.v2",
            context=str(config_path),
        )
        clock = require_mapping(
            data.get("clock", {}),
            "clock",
            context=str(config_path),
        )
        runtime = require_mapping(
            data.get("runtime", {}),
            "runtime",
            context=str(config_path),
        )
        default_budget = require_int(
            clock.get("default_budget_ms", 10_000),
            "clock.default_budget_ms",
            minimum=0,
            code="E_BAD_CONFIG",
            context=str(config_path),
        )
        return cls(
            config_path=config_path,
            root=root,
            name=str(data.get("name", config_path.parent.name)),
            environments=tuple(environments_raw),
            v1_manifest=local_path(sources.get("v1"), "sources.v1"),
            v2_metadata=local_path(v2.get("metadata_db"), "sources.v2.metadata_db"),
            v2_shards_dir=local_path(v2.get("shards_dir"), "sources.v2.shards_dir"),
            cluster_state=local_path(data.get("cluster_state"), "cluster_state"),
            cluster_lock=local_path(
                runtime.get("cluster_lock", "runtime/cluster-state.lock"),
                "runtime.cluster_lock",
            ),
            runtime_db=local_path(
                runtime.get("database", "runtime/controller.db"),
                "runtime.database",
            ),
            initial_clock_ms=require_int(
                clock.get("initial_clock_ms", 0),
                "clock.initial_clock_ms",
                minimum=0,
                code="E_BAD_CONFIG",
                context=str(config_path),
            ),
            operation_cost_ms=require_int(
                clock.get("operation_cost_ms", 250),
                "clock.operation_cost_ms",
                minimum=1,
                code="E_BAD_CONFIG",
                context=str(config_path),
            ),
            default_budget_ms=default_budget,
            clock_tolerance_ms=require_int(
                clock.get("clock_tolerance_ms", 5_000),
                "clock.clock_tolerance_ms",
                minimum=0,
                code="E_BAD_CONFIG",
                context=str(config_path),
            ),
        )

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    @property
    def pid(self) -> int:
        return os.getpid()


def default_project_path() -> Path:
    return Path(__file__).resolve().parent.parent / "samples" / "project.json"
