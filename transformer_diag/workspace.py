from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BusinessError
from .models import RuntimeConfig
from .util import atomic_write_json, read_json


DEFAULT_CONFIG: dict[str, Any] = {
    "tolerance": {
        "atol": 1e-9,
        "rtol": 1e-8,
        "stat_atol": 1e-10,
        "stat_rtol": 1e-8,
    },
    "policy": {
        "strategy": "band",
        "band": 1e-15,
        "eps_min": 1e-12,
        "block_size_max": 1048576,
        "denom_min": 1e-300,
        "subnormal_policy": "flush",
    },
    "degrade_policy": "fail",
    "injections": [],
}


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path

    @property
    def managed(self) -> Path:
        return self.root / ".workbench"

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def cache(self) -> Path:
        return self.managed / "cache"

    @property
    def cache_objects(self) -> Path:
        return self.cache / "objects"

    @property
    def cache_index(self) -> Path:
        return self.cache / "index.json"

    @property
    def inputs(self) -> Path:
        return self.managed / "inputs"

    @property
    def input_meta(self) -> Path:
        return self.managed / "state" / "inputs.json"

    @property
    def state(self) -> Path:
        return self.managed / "state"

    @property
    def jobs_registry(self) -> Path:
        return self.state / "jobs.json"

    @property
    def nodes(self) -> Path:
        return self.state / "nodes.json"

    @property
    def run_state(self) -> Path:
        return self.state / "run_state.json"

    @property
    def versions(self) -> Path:
        return self.state / "versions.json"

    @property
    def errors(self) -> Path:
        return self.state / "errors.jsonl"

    @property
    def latest_report(self) -> Path:
        return self.state / "report.json"

    @property
    def latest_selfcheck(self) -> Path:
        return self.state / "selfcheck.json"

    @property
    def verification(self) -> Path:
        return self.state / "verification.json"

    @property
    def snapshots(self) -> Path:
        return self.managed / "snapshots"

    @property
    def outputs(self) -> Path:
        return self.root / "outputs"

    @property
    def diagnostics(self) -> Path:
        return self.root / "diagnostics"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def html_report(self) -> Path:
        return self.reports / "workbench.html"

    @property
    def lock_dir(self) -> Path:
        return self.managed / ".commit.lock"

    def ensure(self) -> None:
        for path in (
            self.managed,
            self.cache_objects,
            self.inputs,
            self.state,
            self.snapshots,
            self.outputs,
            self.diagnostics,
            self.diagnostics / "deviation",
            self.reports,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def reset(self) -> None:
        if self.managed.exists():
            shutil.rmtree(self.managed)
        for path in (self.outputs, self.diagnostics, self.reports):
            if path.exists():
                shutil.rmtree(path)
        self.ensure()


class Workspace:
    def __init__(self, root: str | Path) -> None:
        self.paths = WorkspacePaths(Path(root).resolve())

    def initialize(self, *, reset: bool = False) -> None:
        if reset:
            self.paths.reset()
        else:
            self.paths.ensure()
        if reset or not self.paths.config.exists():
            atomic_write_json(self.paths.config, DEFAULT_CONFIG, pretty=True)

    def load_config(self, override_path: str | Path | None = None) -> RuntimeConfig:
        raw = read_json(self.paths.config, code="E_POLICY_INVALID", default=DEFAULT_CONFIG)
        if override_path is not None:
            override = read_json(Path(override_path), code="E_POLICY_INVALID")
            if not isinstance(override, dict):
                raise BusinessError(
                    "E_POLICY_INVALID", "override config must be a JSON object", field="config"
                )
            raw = _deep_merge(raw, override)
        return RuntimeConfig.from_dict(raw)

    def load_jobs_registry(self) -> dict[str, dict[str, Any]]:
        value = read_json(self.paths.jobs_registry, default={})
        if not isinstance(value, dict):
            raise BusinessError(
                "E_CACHE_CORRUPT", "jobs registry is not an object", field=str(self.paths.jobs_registry)
            )
        return value

    def save_jobs_registry(self, value: dict[str, dict[str, Any]]) -> None:
        atomic_write_json(self.paths.jobs_registry, value, pretty=True)


def _deep_merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    result = dict(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result

