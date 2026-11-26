"""Content-addressed cache and dependency-closure invalidation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .alignment import AlignmentResult
from .canonical import (
    atomic_write_json,
    read_json,
    sha256_value,
    verify_payload_checksum,
)
from .config import PipelineConfig
from .errors import DriftError, E_CACHE_CORRUPT
from .snapshots import SnapshotBundle
from .windows import WindowSpec


def window_content_hash(
    aligned: AlignmentResult,
    config: PipelineConfig,
    spec: WindowSpec,
) -> str:
    variable_ids = sorted(
        set(config.predictor.input_variables)
        | {config.predictor.output_variable}
    )
    content = {
        "times_ms": [aligned.times_ms[index] for index in spec.sample_indices],
        "variables": {
            str(variable_id): {
                "source": aligned.sources[variable_id],
                "values": [
                    aligned.values[variable_id][index]
                    for index in spec.sample_indices
                ],
                "fill_flags": [
                    aligned.fill_flags[variable_id][index]
                    for index in spec.sample_indices
                ],
            }
            for variable_id in variable_ids
        },
    }
    return sha256_value(content)


def build_window_cache_key(
    *,
    input_variable_content_hash: str,
    predictor_snapshot_version: str,
    residual_window_range: Iterable[int],
    recycle_rate_threshold: float,
    retrain_config_digest: str,
) -> str:
    payload = {
        "schema": "window-cache-key-v1",
        "input_variable_content_hash": input_variable_content_hash,
        "predictor_snapshot_version": predictor_snapshot_version,
        "residual_window_range": list(residual_window_range),
        "recycle_rate_threshold": recycle_rate_threshold,
        "retrain_config_digest": retrain_config_digest,
    }
    return "sha256:" + sha256_value(payload)


def snapshot_cache_version(snapshot: SnapshotBundle) -> str:
    return f"{snapshot.version}:{snapshot.current.version_id()}"


def build_run_cache_key(
    *,
    config: PipelineConfig,
    snapshot: SnapshotBundle,
    window_keys: Iterable[str],
) -> str:
    payload = {
        "schema": "pipeline-run-cache-key-v1",
        "config_digest": config.digest(),
        "snapshot_version": snapshot_cache_version(snapshot),
        "window_keys": list(window_keys),
        "recycle_rate_threshold": config.predictor.recycle_rate_threshold,
        "retrain_config_digest": config.retrain_digest(),
    }
    return "sha256:" + sha256_value(payload)


class ContentAddressedCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = key.replace(":", "_")
        return self.root / f"{safe}.json"

    def has(self, key: str) -> bool:
        return self._path(key).exists()

    def get(self, key: str) -> dict[str, Any]:
        path = self._path(key)
        try:
            entry = read_json(path)
        except DriftError as exc:
            raise DriftError(
                E_CACHE_CORRUPT,
                "cannot read cache entry",
                key=key,
                path=str(path),
            ) from exc
        if not isinstance(entry, dict) or entry.get("key") != key:
            raise DriftError(
                E_CACHE_CORRUPT,
                "cache key mismatch",
                key=key,
                path=str(path),
            )
        try:
            verify_payload_checksum(entry, "entry_checksum")
        except DriftError as exc:
            raise DriftError(
                E_CACHE_CORRUPT,
                "cache entry checksum mismatch",
                key=key,
                path=str(path),
            ) from exc
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            raise DriftError(
                E_CACHE_CORRUPT, "cache entry payload is invalid", key=key
            )
        if sha256_value(payload) != entry.get("payload_digest"):
            raise DriftError(
                E_CACHE_CORRUPT, "cache payload digest mismatch", key=key
            )
        return payload

    def put(self, key: str, payload: dict[str, Any]) -> None:
        body = {
            "schema": "content-addressed-cache-entry-v1",
            "key": key,
            "payload": payload,
            "payload_digest": sha256_value(payload),
        }
        body["entry_checksum"] = sha256_value(body)
        atomic_write_json(self._path(key), body)

    def inspect(self, keys: Iterable[str]) -> dict[str, str]:
        return {
            key: ("hit" if self.has(key) else "miss")
            for key in keys
        }


@dataclass(frozen=True)
class VariableRevision:
    variable: int
    start_ms: int
    end_ms: int
    reason: str = "VARIABLE_REVISION"


@dataclass(frozen=True)
class ImpactPlan:
    predictor_closure: tuple[str, ...]
    affected_windows: tuple[str, ...]
    reasons: dict[str, str]
    window_size_changed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "predictor_closure": list(self.predictor_closure),
            "affected_windows": list(self.affected_windows),
            "reasons": dict(sorted(self.reasons.items())),
            "window_size_changed": self.window_size_changed,
        }


class DependencyGraph:
    """Predictors depend on variables; windows depend on predictors and times."""

    def __init__(
        self,
        *,
        predictor_dependencies: dict[str, set[int]],
        predictor_edges: dict[str, set[str]] | None = None,
    ) -> None:
        self.predictor_dependencies = {
            key: set(value) for key, value in predictor_dependencies.items()
        }
        self.predictor_edges = {
            key: set(value)
            for key, value in (predictor_edges or {}).items()
        }
        self.window_predictors: dict[str, str] = {}
        self.window_variables: dict[str, set[int]] = {}
        self.window_ranges: dict[str, tuple[int, int]] = {}

    @classmethod
    def from_config(
        cls,
        config: PipelineConfig,
        specs: Iterable[WindowSpec],
    ) -> "DependencyGraph":
        variables = set(config.predictor.input_variables) | {
            config.predictor.output_variable
        }
        graph = cls(
            predictor_dependencies={config.predictor.id: variables},
            predictor_edges={config.predictor.id: set()},
        )
        for spec in specs:
            graph.window_predictors[spec.window_id] = config.predictor.id
            graph.window_variables[spec.window_id] = set(variables)
            graph.window_ranges[spec.window_id] = (spec.start_ms, spec.end_ms)
        return graph

    def _predictor_closure(
        self,
        changed_variables: set[int],
        changed_predictors: set[str],
    ) -> set[str]:
        closure = {
            predictor_id
            for predictor_id, dependencies in self.predictor_dependencies.items()
            if dependencies & changed_variables or predictor_id in changed_predictors
        }
        changed = True
        while changed:
            changed = False
            for source, dependents in self.predictor_edges.items():
                if source in closure:
                    for dependent in dependents:
                        if dependent not in closure:
                            closure.add(dependent)
                            changed = True
        return closure

    def propagate(
        self,
        *,
        changed_variables: Iterable[int] = (),
        changed_predictors: Iterable[str] = (),
        revisions: Iterable[VariableRevision] = (),
        window_size_changed: bool = False,
    ) -> ImpactPlan:
        variable_set = set(int(item) for item in changed_variables)
        predictor_set = set(str(item) for item in changed_predictors)
        revision_list = list(revisions)
        closure = self._predictor_closure(variable_set, predictor_set)
        reasons: dict[str, str] = {}
        if window_size_changed:
            for predictor_id in self.predictor_dependencies:
                closure.add(predictor_id)
            for window_id in self.window_predictors:
                reasons[window_id] = "WINDOW_SIZE_CHANGED"
            return ImpactPlan(
                predictor_closure=tuple(sorted(closure)),
                affected_windows=tuple(sorted(reasons)),
                reasons=reasons,
                window_size_changed=True,
            )

        revisions_by_variable: dict[int, list[VariableRevision]] = {}
        for revision in revision_list:
            revisions_by_variable.setdefault(revision.variable, []).append(revision)
            if revision.variable not in variable_set:
                variable_set.add(revision.variable)
                closure |= self._predictor_closure({revision.variable}, set())

        for window_id, predictor_id in sorted(self.window_predictors.items()):
            if predictor_id not in closure:
                continue
            if predictor_id in predictor_set:
                reasons[window_id] = "PREDICTOR_PARAMETER_CHANGED"
                continue
            variables = self.window_variables[window_id]
            relevant = variables & variable_set
            if not relevant:
                continue
            overlap = False
            start_ms, end_ms = self.window_ranges[window_id]
            for variable in relevant:
                revisions_for_variable = revisions_by_variable.get(variable, [])
                if not revisions_for_variable:
                    overlap = True
                    break
                if any(
                    revision.start_ms <= end_ms
                    and revision.end_ms >= start_ms
                    for revision in revisions_for_variable
                ):
                    overlap = True
                    break
            if overlap:
                reasons[window_id] = "VARIABLE_REVISION_OVERLAP"

        return ImpactPlan(
            predictor_closure=tuple(sorted(closure)),
            affected_windows=tuple(sorted(reasons)),
            reasons=reasons,
            window_size_changed=False,
        )

