"""v1/v2 PEFT snapshot loading, writing, validation and bidirectional migration."""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import (
    atomic_write_bytes,
    canonical_bytes,
    layer_first_diff,
    normalize_number,
    read_json,
    sha256_bytes,
)
from .config import DegradationLog, ResourceLimits
from .errors import (
    corrupt,
    format_error,
    semantic_drift,
    unsupported,
)
from .tasks import (
    MODEL_REGISTRY,
    NormalizedTask,
    assert_complete_layers,
    validate_layer_key,
)


def milliseconds_from_seconds(origin_ms: int, seconds: Any) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise format_error("effective_seconds must be a number", field="effective_seconds")
    decimal_seconds = Decimal(str(seconds))
    if not decimal_seconds.is_finite() or decimal_seconds < 0:
        raise format_error(
            "effective_seconds must be finite and non-negative",
            field="effective_seconds",
            details={"value": seconds},
        )
    relative_ms = int((decimal_seconds * 1000).to_integral_value(rounding=ROUND_HALF_UP))
    return int(origin_ms) + relative_ms


def seconds_from_milliseconds(origin_ms: int, absolute_ms: int) -> float:
    return normalize_number((int(absolute_ms) - int(origin_ms)) / 1000.0, 9)


@dataclass
class LayerState:
    key: str
    layer_idx: int
    shape: tuple[int, int]
    values: np.ndarray
    momentum: np.ndarray
    variance: np.ndarray

    @property
    def expected_size(self) -> int:
        return int(self.shape[0]) * int(self.shape[1])

    def canonical_values(self) -> list[float]:
        return [normalize_number(value) for value in self.values.reshape(-1).tolist()]

    def canonical_momentum(self) -> list[float]:
        return [normalize_number(value) for value in self.momentum.reshape(-1).tolist()]

    def canonical_variance(self) -> list[float]:
        return [normalize_number(value) for value in self.variance.reshape(-1).tolist()]

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "layer_idx": self.layer_idx,
            "shape": list(self.shape),
            "values": self.canonical_values(),
            "momentum": self.canonical_momentum(),
            "variance": self.canonical_variance(),
        }

    def clone(self) -> "LayerState":
        return LayerState(
            key=self.key,
            layer_idx=self.layer_idx,
            shape=self.shape,
            values=self.values.copy(),
            momentum=self.momentum.copy(),
            variance=self.variance.copy(),
        )

    @property
    def resident_bytes(self) -> int:
        return int(self.values.nbytes + self.momentum.nbytes + self.variance.nbytes)


@dataclass
class Snapshot:
    snapshot_id: str
    format_version: str
    case_id: str
    base_model_rev: str
    adapter_type: str
    adapter_dim: int
    num_layers: int
    seed: int
    revision: int
    time_origin_ms: int
    absolute_ms: int
    generation: int
    layers: dict[str, LayerState]
    tombstones: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    source_path: str = ""
    source_format: str = ""

    @property
    def effective_seconds(self) -> float:
        return seconds_from_milliseconds(self.time_origin_ms, self.absolute_ms)

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "base_model_rev": self.base_model_rev,
            "adapter_type": self.adapter_type,
            "adapter_dim": self.adapter_dim,
            "num_layers": self.num_layers,
            "seed": self.seed,
            "revision": self.revision,
            "time_origin_ms": self.time_origin_ms,
            "absolute_ms": self.absolute_ms,
            "effective_seconds": self.effective_seconds,
            "layers": {
                key: self.layers[key].semantic_dict()
                for key in sorted(self.layers)
            },
        }

    def semantic_bytes(self) -> bytes:
        return canonical_bytes(self.semantic_dict())

    @property
    def semantic_hash(self) -> str:
        return sha256_bytes(self.semantic_bytes())

    @property
    def resident_bytes(self) -> int:
        return sum(layer.resident_bytes for layer in self.layers.values())

    def validate_against_task(self, task: NormalizedTask) -> None:
        comparisons = {
            "case_id": (self.case_id, task.case_id),
            "base_model_rev": (self.base_model_rev, task.base_model_rev),
            "adapter_type": (self.adapter_type, task.adapter_type),
            "adapter_dim": (self.adapter_dim, task.adapter_dim),
            "num_layers": (self.num_layers, task.num_layers),
            "seed": (self.seed, task.seed),
            "revision": (self.revision, task.revision),
        }
        for field_name, (actual, expected) in comparisons.items():
            if actual != expected:
                raise format_error(
                    f"snapshot {field_name} does not match task",
                    task_id=task.case_id,
                    field=field_name,
                    details={"snapshot": actual, "task": expected},
                )
        assert_complete_layers(task, self.layers)


def _number_list(
    value: Any,
    *,
    task: NormalizedTask,
    key: str,
    field_name: str,
    expected_count: int,
) -> np.ndarray:
    if not isinstance(value, list) or len(value) != expected_count:
        raise format_error(
            f"{field_name} must contain exactly {expected_count} numeric values",
            task_id=task.case_id,
            field=f"layers.{key}.{field_name}",
        )
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            raise format_error(
                f"{field_name} contains a non-finite or non-numeric value",
                task_id=task.case_id,
                field=f"layers.{key}.{field_name}",
            )
    return np.asarray([normalize_number(v) for v in value], dtype=np.float64)


def _make_layer(
    task: NormalizedTask,
    *,
    key: str,
    layer_idx: Any,
    shape: Any,
    values: Any,
    momentum: Any,
    variance: Any,
    seed: Any,
) -> LayerState:
    spec = validate_layer_key(task, key=key, layer_idx=layer_idx, shape=shape, seed=seed)
    count = spec.shape[0] * spec.shape[1]
    value_array = _number_list(
        values,
        task=task,
        key=key,
        field_name="values",
        expected_count=count,
    )
    momentum_array = _number_list(
        momentum,
        task=task,
        key=key,
        field_name="momentum",
        expected_count=count,
    )
    variance_array = _number_list(
        variance,
        task=task,
        key=key,
        field_name="variance",
        expected_count=count,
    )
    return LayerState(
        key=key,
        layer_idx=spec.layer_idx,
        shape=spec.shape,
        values=value_array.reshape(spec.shape),
        momentum=momentum_array.reshape(spec.shape),
        variance=variance_array.reshape(spec.shape),
    )


def _common_header_validate(
    data: dict[str, Any],
    *,
    task: NormalizedTask,
    path: Path,
    expected_format: str,
) -> None:
    required = {
        "format_version",
        "snapshot_id",
        "case_id",
        "base_model_rev",
        "adapter_type",
        "adapter_dim",
        "num_layers",
        "seed",
        "revision",
    }
    missing = sorted(required - set(data))
    if missing:
        raise format_error(
            f"snapshot missing required field {missing[0]}",
            task_id=task.case_id,
            field=missing[0],
            details={"path": str(path)},
        )
    if data.get("format_version") != expected_format:
        raise unsupported(
            f"snapshot format does not match {expected_format} path dispatch",
            task_id=task.case_id,
            field="format_version",
            details={"actual": data.get("format_version")},
        )
    if not isinstance(data.get("snapshot_id"), str) or not data["snapshot_id"]:
        raise format_error("snapshot_id must be a non-empty string", task_id=task.case_id, field="snapshot_id")
    _require_plain_int(data, "adapter_dim", task=task)
    _require_plain_int(data, "num_layers", task=task)
    _require_plain_int(data, "seed", task=task)
    _require_plain_int(data, "revision", task=task)


def _require_plain_int(data: dict[str, Any], field_name: str, *, task: NormalizedTask) -> int:
    value = data.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise format_error(
            f"{field_name} must be an integer",
            task_id=task.case_id,
            field=field_name,
        )
    return value


def _load_v1(path: Path, task: NormalizedTask) -> Snapshot:
    try:
        data = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise format_error(
            f"cannot parse v1 snapshot: {exc}",
            task_id=task.case_id,
            field="format_version",
            details={"path": str(path)},
        )
    if not isinstance(data, dict):
        raise format_error("v1 snapshot root must be an object", task_id=task.case_id, field="format_version")
    if "format_version" not in data:
        raise format_error("v1 snapshot missing format_version", task_id=task.case_id, field="format_version")
    version = data["format_version"]
    if version != "v1":
        raise unsupported(
            f"unsupported snapshot format_version {version!r}",
            task_id=task.case_id,
            field="format_version",
            details={"supported": ["v1", "v2"]},
        )
    _common_header_validate(data, task=task, path=path, expected_format="v1")
    required = {"time_origin_ms", "effective_seconds", "layers"}
    missing = sorted(required - set(data))
    if missing:
        raise format_error(
            f"v1 snapshot missing {missing[0]}",
            task_id=task.case_id,
            field=missing[0],
        )
    origin_ms = _require_plain_int(data, "time_origin_ms", task=task)
    if origin_ms < 0:
        raise format_error("time_origin_ms must be non-negative", task_id=task.case_id, field="time_origin_ms")
    absolute_ms = milliseconds_from_seconds(origin_ms, data["effective_seconds"])
    if absolute_ms < origin_ms:
        raise format_error(
            "absolute time precedes conversion baseline",
            task_id=task.case_id,
            field="effective_seconds",
        )
    layers_data = data["layers"]
    if not isinstance(layers_data, dict) or not layers_data:
        raise format_error("v1 layers must be a non-empty object", task_id=task.case_id, field="layers")
    layers: dict[str, LayerState] = {}
    for key, layer_data in layers_data.items():
        if not isinstance(layer_data, dict):
            raise format_error(
                "v1 layer entry must be an object",
                task_id=task.case_id,
                field=f"layers.{key}",
            )
        required_layer = {"layer_idx", "shape", "values", "momentum", "variance", "seed"}
        missing_layer = sorted(required_layer - set(layer_data))
        if missing_layer:
            raise format_error(
                f"v1 layer missing {missing_layer[0]}",
                task_id=task.case_id,
                field=f"layers.{key}.{missing_layer[0]}",
            )
        layers[key] = _make_layer(task, key=key, **layer_data)
    snapshot = Snapshot(
        snapshot_id=data["snapshot_id"],
        format_version="v1",
        case_id=data["case_id"],
        base_model_rev=data["base_model_rev"],
        adapter_type=data["adapter_type"],
        adapter_dim=data["adapter_dim"],
        num_layers=data["num_layers"],
        seed=data["seed"],
        revision=data["revision"],
        time_origin_ms=origin_ms,
        absolute_ms=absolute_ms,
        generation=0,
        layers=layers,
        tombstones=[],
        provenance={"format": "v1", "source_path": str(path)},
        source_path=str(path),
        source_format="v1",
    )
    snapshot.validate_against_task(task)
    return snapshot


def _open_metadata_db(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise format_error(
            "v2 metadata database is missing",
            field="metadata_db",
            details={"path": str(path)},
        )
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise corrupt(
            f"cannot open v2 metadata database: {exc}",
            field="metadata_db",
            details={"path": str(path)},
        )
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error as exc:
        raise corrupt(f"cannot inspect metadata table {table}: {exc}", field="metadata_db")
    return [str(row["name"]) for row in rows]


def _load_v2(path: Path, task: NormalizedTask, limits: ResourceLimits, degradation: DegradationLog) -> Snapshot:
    root = path if path.is_dir() else path.parent
    index_path = path / "index.json" if path.is_dir() else path
    if not index_path.is_file():
        raise format_error(
            "v2 index.json is missing",
            task_id=task.case_id,
            field="format_version",
            details={"path": str(index_path)},
        )
    try:
        index = read_json(index_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise format_error(
            f"cannot parse v2 index: {exc}",
            task_id=task.case_id,
            field="format_version",
            details={"path": str(index_path)},
        )
    if not isinstance(index, dict):
        raise format_error("v2 index root must be an object", task_id=task.case_id, field="format_version")
    if index.get("format_version") != "v2":
        raise unsupported(
            f"unsupported snapshot format_version {index.get('format_version')!r}",
            task_id=task.case_id,
            field="format_version",
            details={"supported": ["v1", "v2"]},
        )
    _common_header_validate(index, task=task, path=index_path, expected_format="v2")
    for field_name in ("metadata_db", "shard_dir", "generation"):
        if field_name not in index:
            raise format_error(
                f"v2 index missing {field_name}",
                task_id=task.case_id,
                field=field_name,
            )
    metadata_name = index["metadata_db"]
    shard_dir_name = index["shard_dir"]
    if not isinstance(metadata_name, str) or not isinstance(shard_dir_name, str):
        raise format_error("v2 index path fields must be strings", task_id=task.case_id, field="metadata_db")
    generation = _require_plain_int(index, "generation", task=task)
    if generation < 0:
        raise format_error("snapshot generation must be non-negative", task_id=task.case_id, field="generation")
    metadata_path = (root / metadata_name).resolve()
    shard_root = (root / shard_dir_name).resolve()
    if not metadata_path.is_relative_to(root.resolve()) or not shard_root.is_relative_to(root.resolve()):
        raise format_error(
            "v2 metadata/shard paths escape snapshot directory",
            task_id=task.case_id,
            field="metadata_db",
        )
    connection = _open_metadata_db(metadata_path)
    try:
        required_tables = {"meta", "snapshot_records", "shards"}
        tables = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        missing_tables = sorted(required_tables - tables)
        if missing_tables:
            raise format_error(
                f"v2 metadata database missing table {missing_tables[0]}",
                task_id=task.case_id,
                field="metadata_db",
            )
        record_columns = set(_table_columns(connection, "snapshot_records"))
        shard_columns = set(_table_columns(connection, "shards"))
        missing_record_columns = sorted(
            {"generation", "absolute_ms", "time_origin_ms", "revision"} - record_columns
        )
        missing_shard_columns = sorted(
            {
                "shard_id",
                "layer_key",
                "layer_idx",
                "generation",
                "path",
                "content_sha256",
                "tombstone",
                "shape",
            }
            - shard_columns
        )
        if missing_record_columns or missing_shard_columns:
            raise format_error(
                "v2 metadata database schema is incomplete",
                task_id=task.case_id,
                field="metadata_db",
                details={
                    "missing_snapshot_columns": missing_record_columns,
                    "missing_shard_columns": missing_shard_columns,
                },
            )
        record_rows = connection.execute(
            "SELECT generation, absolute_ms, time_origin_ms, revision "
            "FROM snapshot_records ORDER BY generation"
        ).fetchall()
        if not record_rows:
            raise format_error(
                "v2 metadata database has no snapshot records",
                task_id=task.case_id,
                field="snapshot_records",
            )
        previous_generation = -1
        for record_row in record_rows:
            record_generation = int(record_row["generation"])
            if record_generation <= previous_generation:
                raise format_error(
                    "v2 snapshot generation sequence is not strictly increasing",
                    task_id=task.case_id,
                    field="snapshot_records.generation",
                    details={"previous": previous_generation, "actual": record_generation},
                )
            previous_generation = record_generation
        latest_record = record_rows[-1]
        if int(latest_record["generation"]) != generation:
            raise format_error(
                "v2 index generation does not match latest metadata record",
                task_id=task.case_id,
                field="generation",
                details={"index": generation, "metadata": int(latest_record["generation"])},
            )
        origin_ms = int(latest_record["time_origin_ms"])
        absolute_ms = int(latest_record["absolute_ms"])
        revision = int(latest_record["revision"])
        if origin_ms < 0 or absolute_ms < origin_ms:
            raise format_error(
                "v2 absolute time and conversion baseline are invalid",
                task_id=task.case_id,
                field="absolute_ms",
                details={"origin_ms": origin_ms, "absolute_ms": absolute_ms},
            )
        if revision != task.revision:
            raise format_error(
                "v2 metadata revision does not match task",
                task_id=task.case_id,
                field="revision",
                details={"metadata": revision, "task": task.revision},
            )
        shard_rows = connection.execute(
            "SELECT shard_id, layer_key, layer_idx, generation, path, content_sha256, "
            "tombstone, shape FROM shards ORDER BY layer_key, generation, shard_id"
        ).fetchall()
    finally:
        connection.close()
    if not shard_rows:
        raise format_error("v2 metadata database has no shards", task_id=task.case_id, field="shards")
    if len(shard_rows) > limits.max_open_shards:
        degradation.record(
            reason="shard_count_exceeds_open_limit",
            priority=3,
            action="sequential_batched_reads",
            task_id=task.case_id,
            details={"shards": len(shard_rows), "limit": limits.max_open_shards},
        )
    verified_files: dict[str, dict[str, Any]] = {}
    tombstones: list[dict[str, Any]] = []
    for row in shard_rows:
        shard_id = str(row["shard_id"])
        layer_key = str(row["layer_key"])
        shard_generation = int(row["generation"])
        if shard_generation < 0 or shard_generation > generation:
            raise format_error(
                "v2 shard generation is outside the snapshot generation range",
                task_id=task.case_id,
                field=f"layers.{layer_key}.generation",
                details={"shard_generation": shard_generation, "snapshot_generation": generation},
            )
        tombstone = int(row["tombstone"]) != 0
        row_dict = {
            "shard_id": shard_id,
            "layer_key": layer_key,
            "layer_idx": int(row["layer_idx"]),
            "generation": shard_generation,
            "path": str(row["path"]),
            "content_sha256": str(row["content_sha256"]),
            "tombstone": tombstone,
            "shape": json.loads(row["shape"]),
        }
        if tombstone:
            tombstones.append(row_dict)
            continue
        relative_path = Path(str(row["path"]))
        shard_path = (root / relative_path).resolve()
        if not shard_path.is_relative_to(root.resolve()):
            raise format_error(
                "v2 shard path escapes shard directory",
                task_id=task.case_id,
                field=f"layers.{layer_key}.path",
                details={"path": str(relative_path)},
            )
        if not shard_path.is_file():
            raise corrupt(
                "v2 shard file is missing",
                task_id=task.case_id,
                field=f"layers.{layer_key}.content_sha256",
                details={"path": str(shard_path)},
            )
        raw = shard_path.read_bytes()
        actual_hash = sha256_bytes(raw)
        if actual_hash != row_dict["content_sha256"]:
            raise corrupt(
                "v2 shard content hash does not match metadata",
                task_id=task.case_id,
                field=f"layers.{layer_key}.content_sha256",
                details={
                    "expected": row_dict["content_sha256"],
                    "actual": actual_hash,
                    "shard_id": shard_id,
                },
            )
        try:
            shard_data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise corrupt(
                "v2 shard JSON is invalid",
                task_id=task.case_id,
                field=f"layers.{layer_key}.content_sha256",
                details={"error": exc.msg, "shard_id": shard_id},
            )
        if not isinstance(shard_data, dict):
            raise corrupt(
                "v2 shard root must be an object",
                task_id=task.case_id,
                field=f"layers.{layer_key}.content_sha256",
            )
        if shard_data.get("format_version") != "v2-shard":
            raise unsupported(
                f"unsupported v2 shard format {shard_data.get('format_version')!r}",
                task_id=task.case_id,
                field=f"layers.{layer_key}.format_version",
            )
        if shard_data.get("shard_id") != shard_id:
            raise corrupt(
                "v2 shard id differs from metadata",
                task_id=task.case_id,
                field=f"layers.{layer_key}.shard_id",
            )
        if shard_data.get("layer_key") != layer_key:
            raise corrupt(
                "v2 shard layer key differs from metadata",
                task_id=task.case_id,
                field=f"layers.{layer_key}.key",
            )
        if shard_data.get("generation") != shard_generation:
            raise corrupt(
                "v2 shard generation differs from metadata",
                task_id=task.case_id,
                field=f"layers.{layer_key}.generation",
            )
        required_shard = {"layer_idx", "shape", "values", "momentum", "variance", "seed"}
        missing_shard = sorted(required_shard - set(shard_data))
        if missing_shard:
            raise format_error(
                f"v2 shard missing {missing_shard[0]}",
                task_id=task.case_id,
                field=f"layers.{layer_key}.{missing_shard[0]}",
            )
        shard_spec = validate_layer_key(
            task,
            key=layer_key,
            layer_idx=shard_data["layer_idx"],
            shape=shard_data["shape"],
            seed=shard_data["seed"],
        )
        if list(shard_spec.shape) != list(row_dict["shape"]):
            raise format_error(
                "v2 metadata shape differs from shard content",
                task_id=task.case_id,
                field=f"layers.{layer_key}.shape",
                details={"metadata": row_dict["shape"], "shard": shard_data["shape"]},
            )
        verified_files[shard_id] = {
            "row": row_dict,
            "data": shard_data,
            "layer": _make_layer(
                task,
                key=layer_key,
                layer_idx=shard_data["layer_idx"],
                shape=shard_data["shape"],
                values=shard_data["values"],
                momentum=shard_data["momentum"],
                variance=shard_data["variance"],
                seed=shard_data["seed"],
            ),
        }
    # Select the highest non-tombstoned generation for each active layer.
    by_layer: dict[str, list[dict[str, Any]]] = {}
    for row in shard_rows:
        by_layer.setdefault(str(row["layer_key"]), []).append(
            {
                "shard_id": str(row["shard_id"]),
                "generation": int(row["generation"]),
                "tombstone": int(row["tombstone"]) != 0,
            }
        )
    active_layers: dict[str, LayerState] = {}
    active_shards: list[dict[str, Any]] = []
    for layer_key in sorted(by_layer):
        rows = by_layer[layer_key]
        latest_generation = max(row["generation"] for row in rows)
        latest_valid = [
            row for row in rows
            if row["generation"] == latest_generation and not row["tombstone"]
        ]
        latest_tombstones = [
            row for row in rows
            if row["generation"] == latest_generation and row["tombstone"]
        ]
        if not latest_valid:
            if latest_tombstones:
                raise format_error(
                    "active adapter layer is tombstoned",
                    task_id=task.case_id,
                    field=f"layers.{layer_key}",
                    details={"generation": latest_generation},
                )
            raise corrupt(
                "v2 metadata has no active shard for layer",
                task_id=task.case_id,
                field=f"layers.{layer_key}.content_sha256",
            )
        if len(latest_valid) > 1:
            raise format_error(
                "multiple active shards share the highest generation",
                task_id=task.case_id,
                field=f"layers.{layer_key}.generation",
                details={"generation": latest_generation, "shards": sorted(row["shard_id"] for row in latest_valid)},
            )
        shard_id = latest_valid[0]["shard_id"]
        record = verified_files[shard_id]
        active_layers[layer_key] = record["layer"]
        active_shards.append(
            {
                "shard_id": shard_id,
                "layer_key": layer_key,
                "generation": latest_generation,
                "content_sha256": record["row"]["content_sha256"],
            }
        )
    snapshot = Snapshot(
        snapshot_id=index["snapshot_id"],
        format_version="v2",
        case_id=index["case_id"],
        base_model_rev=index["base_model_rev"],
        adapter_type=index["adapter_type"],
        adapter_dim=index["adapter_dim"],
        num_layers=index["num_layers"],
        seed=index["seed"],
        revision=index["revision"],
        time_origin_ms=origin_ms,
        absolute_ms=absolute_ms,
        generation=generation,
        layers=active_layers,
        tombstones=tombstones,
        provenance={
            "format": "v2",
            "source_path": str(root),
            "active_shards": active_shards,
            "snapshot_records": [
                {
                    "generation": int(row["generation"]),
                    "absolute_ms": int(row["absolute_ms"]),
                    "time_origin_ms": int(row["time_origin_ms"]),
                    "revision": int(row["revision"]),
                }
                for row in record_rows
            ],
        },
        source_path=str(root),
        source_format="v2",
    )
    snapshot.validate_against_task(task)
    return snapshot


def detect_format(path: str | Path) -> str:
    candidate = Path(path)
    index_path = candidate / "index.json" if candidate.is_dir() else candidate
    if not index_path.exists():
        raise format_error(
            "snapshot path does not exist",
            field="format_version",
            details={"path": str(candidate)},
        )
    if candidate.is_dir():
        try:
            header = read_json(index_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise format_error(
                f"cannot read v2 index header: {exc}",
                field="format_version",
                details={"path": str(index_path)},
            )
    else:
        try:
            header = read_json(index_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise format_error(
                f"cannot read snapshot header: {exc}",
                field="format_version",
                details={"path": str(index_path)},
            )
    if not isinstance(header, dict) or "format_version" not in header:
        raise format_error("snapshot header is missing format_version", field="format_version")
    version = header["format_version"]
    if version not in {"v1", "v2"}:
        raise unsupported(
            f"unsupported snapshot format_version {version!r}",
            field="format_version",
            details={"supported": ["v1", "v2"]},
        )
    if version == "v1" and candidate.is_dir():
        raise format_error("v1 snapshot must be a single JSON file", field="format_version")
    if version == "v2" and not candidate.is_dir():
        raise format_error("v2 snapshot must be a directory", field="format_version")
    return version


def load_snapshot(
    path: str | Path,
    task: NormalizedTask,
    *,
    limits: ResourceLimits | None = None,
    degradation: DegradationLog | None = None,
) -> Snapshot:
    limits = limits or ResourceLimits()
    degradation = degradation or DegradationLog()
    version = detect_format(path)
    if version == "v1":
        return _load_v1(Path(path), task)
    return _load_v2(Path(path), task, limits, degradation)


def _shard_filename(layer_key: str, generation: int, sequence: int) -> str:
    safe_prefix = sha256_bytes(layer_key.encode("utf-8"))[:12]
    return f"g{generation:04d}-{sequence:05d}-{safe_prefix}.json"


def write_v1(snapshot: Snapshot, path: str | Path, *, provenance: dict[str, Any] | None = None) -> str:
    layers = {
        key: {
            "layer_idx": layer.layer_idx,
            "shape": list(layer.shape),
            "values": layer.canonical_values(),
            "momentum": layer.canonical_momentum(),
            "variance": layer.canonical_variance(),
            "seed": snapshot.seed,
        }
        for key, layer in sorted(snapshot.layers.items())
    }
    data = {
        "format_version": "v1",
        "snapshot_id": snapshot.snapshot_id,
        "case_id": snapshot.case_id,
        "base_model_rev": snapshot.base_model_rev,
        "adapter_type": snapshot.adapter_type,
        "adapter_dim": snapshot.adapter_dim,
        "num_layers": snapshot.num_layers,
        "seed": snapshot.seed,
        "revision": snapshot.revision,
        "time_origin_ms": snapshot.time_origin_ms,
        "effective_seconds": seconds_from_milliseconds(snapshot.time_origin_ms, snapshot.absolute_ms),
        "layers": layers,
        "provenance": provenance or {"format": "v1", "source": "native"},
    }
    payload = canonical_bytes(data)
    atomic_write_bytes(path, payload)
    return sha256_bytes(payload)


def _create_v2_schema(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute(
        "CREATE TABLE snapshot_records ("
        "generation INTEGER PRIMARY KEY,"
        "absolute_ms INTEGER NOT NULL,"
        "time_origin_ms INTEGER NOT NULL,"
        "revision INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE shards ("
        "shard_id TEXT PRIMARY KEY,"
        "layer_key TEXT NOT NULL,"
        "layer_idx INTEGER NOT NULL,"
        "generation INTEGER NOT NULL,"
        "path TEXT NOT NULL,"
        "content_sha256 TEXT NOT NULL,"
        "tombstone INTEGER NOT NULL DEFAULT 0,"
        "shape TEXT NOT NULL)"
    )


def write_v2(
    snapshot: Snapshot,
    path: str | Path,
    *,
    provenance: dict[str, Any] | None = None,
    generation: int | None = None,
) -> dict[str, Any]:
    root = Path(path)
    if root.exists():
        if root.is_dir():
            shutil.rmtree(root)
        else:
            root.unlink()
    shard_dir = root / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    generation = snapshot.generation if generation is None else int(generation)
    connection = sqlite3.connect(root / "metadata.sqlite")
    try:
        _create_v2_schema(connection)
        connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?)",
            ("schema_version", "v2"),
        )
        connection.execute(
            "INSERT INTO snapshot_records(generation, absolute_ms, time_origin_ms, revision) VALUES(?, ?, ?, ?)",
            (generation, snapshot.absolute_ms, snapshot.time_origin_ms, snapshot.revision),
        )
        records: list[dict[str, Any]] = []
        for sequence, (layer_key, layer) in enumerate(sorted(snapshot.layers.items())):
            shard_id = f"g{generation:04d}-{sequence:05d}"
            filename = _shard_filename(layer_key, generation, sequence)
            relative_path = f"shards/{filename}"
            shard_data = {
                "format_version": "v2-shard",
                "shard_id": shard_id,
                "layer_key": layer_key,
                "layer_idx": layer.layer_idx,
                "generation": generation,
                "shape": list(layer.shape),
                "values": layer.canonical_values(),
                "momentum": layer.canonical_momentum(),
                "variance": layer.canonical_variance(),
                "seed": snapshot.seed,
            }
            raw = canonical_bytes(shard_data)
            atomic_write_bytes(shard_dir / filename, raw)
            digest = sha256_bytes(raw)
            connection.execute(
                "INSERT INTO shards(shard_id, layer_key, layer_idx, generation, path, content_sha256, tombstone, shape) "
                "VALUES(?, ?, ?, ?, ?, ?, 0, ?)",
                (
                    shard_id,
                    layer_key,
                    layer.layer_idx,
                    generation,
                    relative_path,
                    digest,
                    canonical_bytes(list(layer.shape)).decode("utf-8"),
                ),
            )
            records.append(
                {
                    "shard_id": shard_id,
                    "layer_key": layer_key,
                    "generation": generation,
                    "content_sha256": digest,
                    "path": relative_path,
                }
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    index = {
        "format_version": "v2",
        "snapshot_id": snapshot.snapshot_id,
        "case_id": snapshot.case_id,
        "base_model_rev": snapshot.base_model_rev,
        "adapter_type": snapshot.adapter_type,
        "adapter_dim": snapshot.adapter_dim,
        "num_layers": snapshot.num_layers,
        "seed": snapshot.seed,
        "revision": snapshot.revision,
        "generation": generation,
        "metadata_db": "metadata.sqlite",
        "shard_dir": "shards",
        "provenance": provenance or {"format": "v2", "source": "native"},
    }
    atomic_write_bytes(root / "index.json", canonical_bytes(index))
    return {
        "root": str(root),
        "index": index,
        "shards": records,
        "semantic_hash": snapshot.semantic_hash,
    }


def _default_completion_provenance() -> list[dict[str, str]]:
    return [
        {"field": "absolute_ms", "source": "derived:time_origin_ms+round(effective_seconds*1000)"},
        {"field": "generation", "source": "default:0"},
        {"field": "tombstones", "source": "default:[]"},
        {"field": "content_sha256", "source": "computed:sha256(canonical shard file bytes)"},
        {"field": "shard_boundaries", "source": "derived:one shard per normalized layer key"},
    ]


def migrate_snapshot(
    source: str | Path,
    task: NormalizedTask,
    *,
    target_format: str,
    output: str | Path,
    limits: ResourceLimits | None = None,
    degradation: DegradationLog | None = None,
) -> dict[str, Any]:
    if target_format not in {"v1", "v2"}:
        raise unsupported(
            f"unsupported migration target format {target_format!r}",
            task_id=task.case_id,
            field="format_version",
            details={"supported": ["v1", "v2"]},
        )
    degradation = degradation or DegradationLog()
    original = load_snapshot(source, task, limits=limits, degradation=degradation)
    source_version = original.format_version
    target_path = Path(output)
    if target_format == "v2":
        if not (target_path.is_dir()) and target_path.exists():
            raise format_error(
                "v2 migration target must be a directory",
                task_id=task.case_id,
                field="output",
            )
        provenance = {
            "migration": f"{source_version}_to_v2",
            "completions": _default_completion_provenance(),
            "source_format": source_version,
        }
        write_v2(original, target_path, provenance=provenance, generation=0)
    else:
        if target_path.exists() and target_path.is_dir():
            raise format_error(
                "v1 migration target must be a file",
                task_id=task.case_id,
                field="output",
            )
        dropped = [
            {"field": "generation", "rule": "discard: v1 has no generation header"},
            {"field": "tombstones", "rule": "discard: inactive revisions are already omitted"},
            {"field": "shard_boundaries", "rule": "flatten: materialize active layer map"},
            {"field": "incremental_history", "rule": "discard: v1 stores only the effective snapshot"},
        ]
        provenance = {
            "migration": f"{source_version}_to_v1",
            "dropped": dropped,
            "source_format": source_version,
        }
        write_v1(original, target_path, provenance=provenance)
    migrated = load_snapshot(target_path, task, limits=limits, degradation=degradation)
    original_view = original.semantic_dict()
    migrated_view = migrated.semantic_dict()
    difference = layer_first_diff(original_view, migrated_view)
    if difference:
        raise semantic_drift(
            "snapshot migration changed semantic content",
            task_id=task.case_id,
            field=difference["field"],
            details={
                "first_difference": difference,
                "source": str(source),
                "output": str(output),
                "target_format": target_format,
            },
        )
    return {
        "ok": True,
        "task_id": task.case_id,
        "source": str(source),
        "source_format": source_version,
        "target": str(output),
        "target_format": target_format,
        "source_semantic_hash": original.semantic_hash,
        "target_semantic_hash": migrated.semantic_hash,
        "semantic_equal": True,
        "completions": _default_completion_provenance() if target_format == "v2" else [],
        "dropped": [] if target_format == "v2" else [
            "generation",
            "tombstones",
            "shard_boundaries",
            "incremental_history",
        ],
    }


def append_v2_incremental_revision(
    root: str | Path,
    task: NormalizedTask,
    *,
    changed_layer_key: str,
    tombstone_previous: bool = True,
) -> dict[str, Any]:
    """Create a deterministic higher-generation shard plus a superseded-shard tombstone."""
    root_path = Path(root)
    index = read_json(root_path / "index.json")
    if index.get("format_version") != "v2":
        raise unsupported("incremental revision requires a v2 snapshot", task_id=task.case_id, field="format_version")
    base = load_snapshot(root_path, task, degradation=DegradationLog())
    connection = sqlite3.connect(root_path / "metadata.sqlite")
    connection.row_factory = sqlite3.Row
    try:
        latest_generation = int(
            connection.execute("SELECT MAX(generation) FROM snapshot_records").fetchone()[0]
        )
        next_generation = latest_generation + 1
        previous = connection.execute(
            "SELECT shard_id, path, content_sha256 FROM shards "
            "WHERE layer_key = ? AND tombstone = 0 ORDER BY generation DESC LIMIT 1",
            (changed_layer_key,),
        ).fetchone()
        if previous is None:
            raise format_error(
                "changed layer is absent from v2 metadata",
                task_id=task.case_id,
                field="changed_layer_key",
            )
        layer = base.layers[changed_layer_key]
        values = (layer.values + 0.000317 * np.cos(np.arange(layer.values.size).reshape(layer.shape))).reshape(layer.shape)
        values = np.asarray([normalize_number(v) for v in values.reshape(-1)], dtype=np.float64).reshape(layer.shape)
        sequence = int(connection.execute("SELECT COUNT(*) FROM shards").fetchone()[0])
        shard_id = f"g{next_generation:04d}-{sequence:05d}"
        filename = _shard_filename(changed_layer_key, next_generation, sequence)
        relative_path = f"shards/{filename}"
        shard_data = {
            "format_version": "v2-shard",
            "shard_id": shard_id,
            "layer_key": changed_layer_key,
            "layer_idx": layer.layer_idx,
            "generation": next_generation,
            "shape": list(layer.shape),
            "values": [normalize_number(v) for v in values.reshape(-1).tolist()],
            "momentum": layer.canonical_momentum(),
            "variance": layer.canonical_variance(),
            "seed": task.seed,
        }
        raw = canonical_bytes(shard_data)
        atomic_write_bytes(root_path / relative_path, raw)
        digest = sha256_bytes(raw)
        connection.execute(
            "INSERT INTO shards(shard_id, layer_key, layer_idx, generation, path, content_sha256, tombstone, shape) "
            "VALUES(?, ?, ?, ?, ?, ?, 0, ?)",
            (
                shard_id,
                changed_layer_key,
                layer.layer_idx,
                next_generation,
                relative_path,
                digest,
                canonical_bytes(list(layer.shape)).decode("utf-8"),
            ),
        )
        if tombstone_previous:
            old = previous
            tombstone_id = f"tombstone-{next_generation:04d}-{old['shard_id']}"
            connection.execute(
                "INSERT INTO shards(shard_id, layer_key, layer_idx, generation, path, content_sha256, tombstone, shape) "
                "VALUES(?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    tombstone_id,
                    changed_layer_key,
                    layer.layer_idx,
                    next_generation,
                    old["path"],
                    "",
                    canonical_bytes(list(layer.shape)).decode("utf-8"),
                ),
            )
        connection.execute(
            "INSERT INTO snapshot_records(generation, absolute_ms, time_origin_ms, revision) VALUES(?, ?, ?, ?)",
            (next_generation, base.absolute_ms, base.time_origin_ms, base.revision),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    index["generation"] = next_generation
    atomic_write_bytes(root_path / "index.json", canonical_bytes(index))
    return {
        "generation": next_generation,
        "changed_layer_key": changed_layer_key,
        "tombstone_previous": tombstone_previous,
    }


def estimate_task_snapshot_bytes(task: NormalizedTask) -> int:
    hidden = int(MODEL_REGISTRY[task.base_model_rev]["hidden"])
    count = len(task.layer_specs) * hidden * task.adapter_dim
    return count * 8 * 3


__all__ = [
    "LayerState",
    "Snapshot",
    "append_v2_incremental_revision",
    "detect_format",
    "estimate_task_snapshot_bytes",
    "load_snapshot",
    "migrate_snapshot",
    "milliseconds_from_seconds",
    "seconds_from_milliseconds",
    "write_v1",
    "write_v2",
]

