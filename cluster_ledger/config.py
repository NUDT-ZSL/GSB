"""Strict declaration and deterministic sample-data loading."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .deterministic import (
    SUPPORTED_AGGREGATIONS,
    AggregationDataError,
    canonical_json,
    compute_aggregate,
    digest_text,
)
from .errors import E_CONFIG, BusinessError


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


@dataclass(frozen=True)
class Limits:
    global_concurrency: int
    per_cluster_concurrency: int
    query_budget_ms: int


@dataclass(frozen=True)
class ClusterSpec:
    cluster_id: str
    host: str
    port: int
    nodes: tuple[str, ...]
    config_version: int
    version_history: tuple[int, ...]
    per_cluster_concurrency: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "cluster_id": self.cluster_id,
            "host": self.host,
            "port": self.port,
            "nodes": list(self.nodes),
            "config_version": self.config_version,
            "version_history": list(self.version_history),
        }
        if self.per_cluster_concurrency is not None:
            payload["per_cluster_concurrency"] = self.per_cluster_concurrency
        return payload


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    config_keys: tuple[str, ...]
    target_clusters: tuple[str, ...]
    aggregation_mode: str
    timeout_ms: int
    retries: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "config_keys": list(self.config_keys),
            "target_clusters": list(self.target_clusters),
            "aggregation": {"mode": self.aggregation_mode},
            "timeout_ms": self.timeout_ms,
            "retries": self.retries,
        }


@dataclass(frozen=True)
class Declaration:
    schema_version: int
    declaration_version: str
    limits: Limits
    config_keys: tuple[str, ...]
    clusters: tuple[ClusterSpec, ...]
    tasks: tuple[TaskSpec, ...]
    raw: Mapping[str, Any]

    def cluster_map(self) -> dict[str, ClusterSpec]:
        return {cluster.cluster_id: cluster for cluster in self.clusters}

    def task_map(self) -> dict[str, TaskSpec]:
        return {task.task_id: task for task in self.tasks}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "declaration_version": self.declaration_version,
            "limits": {
                "global_concurrency": self.limits.global_concurrency,
                "per_cluster_concurrency": self.limits.per_cluster_concurrency,
                "query_budget_ms": self.limits.query_budget_ms,
            },
            "config_keys": list(self.config_keys),
            "clusters": [cluster.to_dict() for cluster in self.clusters],
            "tasks": [task.to_dict() for task in self.tasks],
        }

    @property
    def fingerprint(self) -> str:
        return digest_text(canonical_json(self.to_dict()))


def _fail(message: str, field_path: str, **details: Any) -> None:
    raise BusinessError(E_CONFIG, message, field_path=field_path, details=details)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_json(path: str | Path, label: str) -> Any:
    file_path = Path(path)
    try:
        with file_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        _fail(f"{label} file not found", "$", path=str(file_path))
    except UnicodeDecodeError as exc:
        _fail(f"{label} is not valid UTF-8: {exc}", "$", path=str(file_path))
    except json.JSONDecodeError as exc:
        _fail(
            f"{label} is not valid JSON: {exc.msg}",
            "$",
            path=str(file_path),
            line=exc.lineno,
            column=exc.colno,
        )


def _string(value: Any, field_path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        _fail("must be a non-empty string" if not allow_empty else "must be a string", field_path)
    return value


def _positive_int(value: Any, field_path: str, *, minimum: int = 1, maximum: int | None = None) -> int:
    if not _is_int(value) or value < minimum or (maximum is not None and value > maximum):
        bounds = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        _fail(f"must be an integer {bounds}", field_path)
    return value


def _unique_strings(value: Any, field_path: str, *, nonempty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail("must be an array of strings", field_path)
    if nonempty and not value:
        _fail("must not be empty", field_path)
    seen: set[str] = set()
    result: list[str] = []
    for index, item in enumerate(value):
        item_path = f"{field_path}[{index}]"
        text = _string(item, item_path)
        if text in seen:
            _fail("duplicate value", item_path, value=text)
        seen.add(text)
        result.append(text)
    return tuple(result)


def load_declaration(path: str | Path) -> Declaration:
    return parse_declaration(_read_json(path, "declaration"))


def parse_declaration(payload: Any) -> Declaration:
    """Validate an already-decoded declaration object."""

    if not isinstance(payload, dict):
        _fail("declaration root must be an object", "$")

    allowed_root = {
        "schema_version", "declaration_version", "limits", "clusters", "tasks", "config_keys"
    }
    unknown_root = sorted(set(payload) - allowed_root)
    if unknown_root:
        _fail("unknown declaration fields", "$", fields=unknown_root)

    schema_version = payload.get("schema_version")
    if not _is_int(schema_version) or schema_version != 1:
        _fail("must be integer 1", "$.schema_version")
    declaration_version = _string(payload.get("declaration_version"), "$.declaration_version")

    limits_payload = payload.get("limits")
    if not isinstance(limits_payload, dict):
        _fail("must be an object", "$.limits")
    allowed_limits = {"global_concurrency", "per_cluster_concurrency", "query_budget_ms"}
    unknown_limits = sorted(set(limits_payload) - allowed_limits)
    if unknown_limits:
        _fail("unknown limits fields", "$.limits", fields=unknown_limits)
    limits = Limits(
        global_concurrency=_positive_int(
            limits_payload.get("global_concurrency"), "$.limits.global_concurrency"
        ),
        per_cluster_concurrency=_positive_int(
            limits_payload.get("per_cluster_concurrency"), "$.limits.per_cluster_concurrency"
        ),
        query_budget_ms=_positive_int(
            limits_payload.get("query_budget_ms"), "$.limits.query_budget_ms"
        ),
    )

    config_keys = _unique_strings(payload.get("config_keys"), "$.config_keys")
    known_keys = set(config_keys)

    clusters_payload = payload.get("clusters")
    if not isinstance(clusters_payload, list) or not clusters_payload:
        _fail("must be a non-empty array", "$.clusters")
    clusters: list[ClusterSpec] = []
    seen_cluster_ids: set[str] = set()
    seen_cluster_ports: set[int] = set()
    allowed_cluster = {
        "cluster_id", "host", "port", "nodes", "config_version", "version_history",
        "per_cluster_concurrency",
    }
    for index, item in enumerate(clusters_payload):
        base = f"$.clusters[{index}]"
        if not isinstance(item, dict):
            _fail("must be an object", base)
        unknown = sorted(set(item) - allowed_cluster)
        if unknown:
            _fail("unknown cluster fields", base, fields=unknown)
        cluster_id = _string(item.get("cluster_id"), f"{base}.cluster_id")
        cluster_path = f"{base}.cluster_id"
        if cluster_id in seen_cluster_ids:
            _fail("duplicate cluster_id", cluster_path, cluster_id=cluster_id)
        seen_cluster_ids.add(cluster_id)

        host = _string(item.get("host"), f"{base}.host")
        if host not in LOOPBACK_HOSTS:
            _fail("host must be the loopback literal 127.0.0.1 or ::1", f"{base}.host")
        port = _positive_int(item.get("port"), f"{base}.port", maximum=65535)
        if port in seen_cluster_ports:
            _fail("port must be globally unique across clusters", f"{base}.port", port=port)
        seen_cluster_ports.add(port)
        nodes = _unique_strings(item.get("nodes"), f"{base}.nodes")
        config_version = _positive_int(item.get("config_version"), f"{base}.config_version")
        version_history_raw = item.get("version_history", [config_version])
        if not isinstance(version_history_raw, list) or not version_history_raw:
            _fail("must be a non-empty integer array", f"{base}.version_history")
        version_history: list[int] = []
        for history_index, version in enumerate(version_history_raw):
            version = _positive_int(
                version, f"{base}.version_history[{history_index}]"
            )
            if version_history and version <= version_history[-1]:
                _fail(
                    "config versions must be strictly increasing",
                    f"{base}.version_history[{history_index}]",
                )
            version_history.append(version)
        if version_history[-1] != config_version:
            _fail(
                "last version_history entry must equal config_version",
                f"{base}.config_version",
            )
        per_cluster = item.get("per_cluster_concurrency")
        if per_cluster is not None:
            per_cluster = _positive_int(per_cluster, f"{base}.per_cluster_concurrency")
        clusters.append(
            ClusterSpec(
                cluster_id=cluster_id,
                host=host,
                port=port,
                nodes=nodes,
                config_version=config_version,
                version_history=tuple(version_history),
                per_cluster_concurrency=per_cluster,
            )
        )

    tasks_payload = payload.get("tasks")
    if not isinstance(tasks_payload, list) or not tasks_payload:
        _fail("must be a non-empty array", "$.tasks")
    tasks: list[TaskSpec] = []
    seen_task_ids: set[str] = set()
    allowed_task = {
        "task_id", "config_keys", "target_clusters", "aggregation", "timeout_ms", "retries",
    }
    for index, item in enumerate(tasks_payload):
        base = f"$.tasks[{index}]"
        if not isinstance(item, dict):
            _fail("must be an object", base)
        unknown = sorted(set(item) - allowed_task)
        if unknown:
            _fail("unknown task fields", base, fields=unknown)
        task_id = _string(item.get("task_id"), f"{base}.task_id")
        if task_id in seen_task_ids:
            _fail("duplicate task_id", f"{base}.task_id", task_id=task_id)
        seen_task_ids.add(task_id)
        task_keys = _unique_strings(item.get("config_keys"), f"{base}.config_keys")
        for key_index, key in enumerate(task_keys):
            if key not in known_keys:
                _fail(
                    f"config key {key!r} is not declared",
                    f"{base}.config_keys[{key_index}]",
                    config_key=key,
                )
        targets = _unique_strings(item.get("target_clusters"), f"{base}.target_clusters")
        for target_index, target in enumerate(targets):
            if target not in seen_cluster_ids:
                _fail(
                    f"target cluster {target!r} is not declared",
                    f"{base}.target_clusters[{target_index}]",
                    cluster_id=target,
                )

        aggregation = item.get("aggregation")
        if not isinstance(aggregation, dict):
            _fail("must be an object", f"{base}.aggregation")
        unknown_agg = sorted(set(aggregation) - {"mode"})
        if unknown_agg:
            _fail("unknown aggregation fields", f"{base}.aggregation", fields=unknown_agg)
        mode = aggregation.get("mode")
        if mode not in SUPPORTED_AGGREGATIONS:
            _fail(
                f"aggregation mode must be one of {', '.join(SUPPORTED_AGGREGATIONS)}",
                f"{base}.aggregation.mode",
            )
        timeout_ms = _positive_int(item.get("timeout_ms"), f"{base}.timeout_ms")
        retries = _positive_int(item.get("retries"), f"{base}.retries", minimum=0)
        tasks.append(
            TaskSpec(
                task_id=task_id,
                config_keys=task_keys,
                target_clusters=targets,
                aggregation_mode=mode,
                timeout_ms=timeout_ms,
                retries=retries,
            )
        )

    return Declaration(
        schema_version=schema_version,
        declaration_version=declaration_version,
        limits=limits,
        config_keys=config_keys,
        clusters=tuple(clusters),
        tasks=tuple(tasks),
        raw=payload,
    )


def load_sample_data(path: str | Path) -> dict[str, Any]:
    payload = _read_json(path, "sample data")
    if not isinstance(payload, dict):
        _fail("sample data root must be an object", "$")
    if payload.get("schema_version") not in (None, 1):
        _fail("must be integer 1", "$.schema_version")
    clusters = payload.get("clusters")
    if not isinstance(clusters, dict) or not clusters:
        _fail("must be a non-empty object", "$.clusters")
    for cluster_id, versions in clusters.items():
        if not isinstance(cluster_id, str) or not cluster_id:
            _fail("cluster key must be a non-empty string", "$.clusters")
        cluster_path = f"$.clusters.{cluster_id}"
        if not isinstance(versions, dict) or not versions:
            _fail("must be a non-empty version object", cluster_path)
        for version, keys in versions.items():
            version_path = f"{cluster_path}.{version}"
            try:
                version_int = int(version)
            except (TypeError, ValueError):
                _fail("version keys must be positive integers", version_path)
            if version_int < 1 or str(version_int) != str(version):
                _fail("version keys must be canonical positive integers", version_path)
            if not isinstance(keys, dict):
                _fail("must be an object of config keys", version_path)
            for key, node_values in keys.items():
                key_path = f"{version_path}.{key}"
                if not isinstance(node_values, dict):
                    _fail("must be an object keyed by node", key_path)
    return payload


def load_inputs(declaration_path: str | Path, sample_path: str | Path) -> tuple[Declaration, dict[str, Any]]:
    """Load both files before returning; any failure leaves no partial model."""

    declaration = load_declaration(declaration_path)
    samples = load_sample_data(sample_path)
    cluster_map = declaration.cluster_map()
    for task in declaration.tasks:
        for cluster_id in task.target_clusters:
            cluster = cluster_map[cluster_id]
            for config_version in cluster.version_history:
                try:
                    compute_aggregate(
                        cluster_id=cluster_id,
                        nodes=cluster.nodes,
                        config_version=config_version,
                        config_keys=task.config_keys,
                        aggregation={"mode": task.aggregation_mode},
                        sample_data=samples,
                    )
                except AggregationDataError as exc:
                    _fail(
                        f"sample data cannot reproduce task {task.task_id!r} at version "
                        f"{config_version}: {exc}",
                        exc.field_path,
                        task_id=task.task_id,
                        cluster_id=cluster_id,
                        config_version=config_version,
                    )
    return declaration, samples
