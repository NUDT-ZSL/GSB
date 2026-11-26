"""Version-dispatched predictor snapshots with v1/v2 migration."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .canonical import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_bytes,
    canonical_json,
    read_json,
    sha256_bytes,
    sha256_file,
    sha256_value,
    stable_float,
)
from .errors import (
    DriftError,
    E_SNAPSHOT_INVALID,
    E_VERSION_UNSUPPORTED,
)
from .locking import FileCommitLock, resolve_commit_lock


@dataclass(frozen=True)
class PredictorModel:
    predictor_id: str
    model_type: str
    feature_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    intercept: float
    config_generation: int
    retrain_generation: int
    created_ms: int
    trained_until_ms: int
    origin: str = "snapshot"

    @classmethod
    def from_dict(
        cls, value: dict[str, Any], *, default_origin: str = "snapshot"
    ) -> "PredictorModel":
        try:
            coefficients = tuple(float(item) for item in value["coefficients"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DriftError(E_SNAPSHOT_INVALID, "invalid predictor coefficients") from exc
        feature_names = tuple(str(item) for item in value.get("feature_names", ()))
        if not feature_names:
            feature_names = tuple(
                f"feature_{index}" for index in range(len(coefficients))
            )
        if len(feature_names) != len(coefficients):
            raise DriftError(
                E_SNAPSHOT_INVALID, "feature_names and coefficients differ in length"
            )
        return cls(
            predictor_id=str(value.get("predictor_id", value.get("id", "predictor-main"))),
            model_type=str(value.get("model_type", "linear")),
            feature_names=feature_names,
            coefficients=coefficients,
            intercept=float(value.get("intercept", 0.0)),
            config_generation=int(value.get("config_generation", 1)),
            retrain_generation=int(value.get("retrain_generation", 0)),
            created_ms=int(value.get("created_ms", 0)),
            trained_until_ms=int(value.get("trained_until_ms", 0)),
            origin=str(value.get("origin", default_origin)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "predictor_id": self.predictor_id,
            "model_type": self.model_type,
            "feature_names": list(self.feature_names),
            "coefficients": list(self.coefficients),
            "intercept": self.intercept,
            "config_generation": self.config_generation,
            "retrain_generation": self.retrain_generation,
            "created_ms": self.created_ms,
            "trained_until_ms": self.trained_until_ms,
            "origin": self.origin,
        }

    def predict(self, features: np.ndarray) -> np.ndarray:
        matrix = np.asarray(features, dtype=np.float64)
        if matrix.ndim == 1:
            matrix = matrix.reshape(1, -1)
        if matrix.shape[1] != len(self.coefficients):
            raise DriftError(
                E_SNAPSHOT_INVALID,
                "predictor feature count does not match snapshot",
                expected=len(self.coefficients),
                received=matrix.shape[1],
            )
        return matrix @ np.asarray(self.coefficients, dtype=np.float64) + self.intercept

    def with_update(
        self,
        *,
        coefficients: Iterable[float],
        intercept: float,
        retrain_generation: int,
        trained_until_ms: int,
        origin: str,
    ) -> "PredictorModel":
        return PredictorModel(
            predictor_id=self.predictor_id,
            model_type=self.model_type,
            feature_names=self.feature_names,
            coefficients=tuple(stable_float(value) for value in coefficients),
            intercept=stable_float(intercept),
            config_generation=self.config_generation,
            retrain_generation=retrain_generation,
            created_ms=self.created_ms,
            trained_until_ms=trained_until_ms,
            origin=origin,
        )

    def version_id(self) -> str:
        body = self.to_dict()
        body.pop("origin", None)
        return sha256_value(body)


@dataclass(frozen=True)
class Tombstone:
    predictor_id: str
    target_retrain_generation: int
    reason: str
    created_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "predictor_id": self.predictor_id,
            "target_retrain_generation": self.target_retrain_generation,
            "reason": self.reason,
            "created_ms": self.created_ms,
        }


@dataclass(frozen=True)
class MetadataCompletion:
    field: str
    source: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "source": self.source, "value": self.value}


@dataclass(frozen=True)
class SnapshotBundle:
    version: str
    current: PredictorModel
    models: tuple[PredictorModel, ...]
    tombstones: tuple[Tombstone, ...]
    completions: tuple[MetadataCompletion, ...]
    created_ms: int
    source_path: str | None = None

    @property
    def current_model_id(self) -> str:
        return model_identifier(self.current)


@dataclass(frozen=True)
class DropRecord:
    predictor_id: str
    config_generation: int
    retrain_generation: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "predictor_id": self.predictor_id,
            "config_generation": self.config_generation,
            "retrain_generation": self.retrain_generation,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MigrationResult:
    target_version: str
    output_path: str
    completions: tuple[MetadataCompletion, ...]
    drops: tuple[DropRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_version": self.target_version,
            "output_path": self.output_path,
            "completions": [item.to_dict() for item in self.completions],
            "drops": [item.to_dict() for item in self.drops],
        }


def model_identifier(model: PredictorModel) -> str:
    return (
        f"{model.predictor_id}:cfg{model.config_generation}:"
        f"retrain{model.retrain_generation}:{model.version_id()[:12]}"
    )


def _dispatch_version(payload: dict[str, Any]) -> str:
    raw = payload.get("version")
    if raw is None and isinstance(payload.get("header"), dict):
        raw = payload["header"].get("schema_version")
    if raw in (1, "1", "v1", "V1"):
        return "v1"
    if raw in (2, "2", "v2", "V2"):
        return "v2"
    raise DriftError(
        E_VERSION_UNSUPPORTED,
        "snapshot version is not supported",
        version=raw,
    )


def read_snapshot(
    path: str | Path, *, epoch_ms: int = 0
) -> SnapshotBundle:
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise DriftError(E_SNAPSHOT_INVALID, "snapshot root must be an object")
    version = _dispatch_version(payload)
    if version == "v1":
        return _read_v1(path, payload, epoch_ms=epoch_ms)
    return _read_v2(path, payload)


def _read_v1(
    path: str | Path, payload: dict[str, Any], *, epoch_ms: int
) -> SnapshotBundle:
    completions: list[MetadataCompletion] = []
    predictor = payload.get("predictor", {})
    parameters = payload.get("parameters", {})
    if not isinstance(predictor, dict) or not isinstance(parameters, dict):
        raise DriftError(E_SNAPSHOT_INVALID, "invalid v1 snapshot structure")

    feature_names = predictor.get("feature_names")
    if not feature_names:
        coefficient_keys = sorted(
            (
                key,
                value,
            )
            for key, value in parameters.items()
            if key.startswith("coefficient.") or key.endswith(".coefficient")
        )
        coefficients = [float(value) for _, value in coefficient_keys]
        feature_names = [f"feature_{index}" for index in range(len(coefficients))]
        completions.append(
            MetadataCompletion(
                "feature_names",
                "v1_flat_parameter_order",
                list(feature_names),
            )
        )
    else:
        feature_names = [str(item) for item in feature_names]
        coefficients = []
        for index, name in enumerate(feature_names):
            for key in (
                f"coefficient.{index}",
                f"feature.{name}.coefficient",
                f"{name}.coefficient",
            ):
                if key in parameters:
                    coefficients.append(float(parameters[key]))
                    break
            else:
                if "coefficients" in predictor:
                    coefficients.append(float(predictor["coefficients"][index]))
                else:
                    raise DriftError(
                        E_SNAPSHOT_INVALID,
                        f"missing coefficient for feature {name}",
                    )

    intercept = parameters.get(
        "intercept",
        parameters.get(
            "bias",
            predictor.get("intercept", 0.0),
        ),
    )
    if "intercept" not in parameters and "bias" not in parameters:
        completions.append(
            MetadataCompletion("intercept", "v1_predictor_object", intercept)
        )

    relative_s = payload.get(
        "created_relative_s",
        payload.get("trained_until_relative_s", predictor.get("created_relative_s", 0.0)),
    )
    created_ms = epoch_ms + int(round(float(relative_s) * 1000))
    completions.append(
        MetadataCompletion(
            "created_ms",
            "v1_relative_seconds_plus_epoch",
            created_ms,
        )
    )
    trained_relative_s = payload.get(
        "trained_until_relative_s",
        predictor.get("trained_until_relative_s", relative_s),
    )
    trained_until_ms = epoch_ms + int(round(float(trained_relative_s) * 1000))

    config_generation = payload.get(
        "config_generation",
        predictor.get("config_generation", 1),
    )
    if "config_generation" not in payload and "config_generation" not in predictor:
        completions.append(
            MetadataCompletion("config_generation", "default_v1_generation", 1)
        )
    predictor_id = predictor.get(
        "id",
        predictor.get("predictor_id", payload.get("predictor_id", "predictor-main")),
    )
    if "id" not in predictor and "predictor_id" not in predictor:
        completions.append(
            MetadataCompletion("predictor_id", "default_predictor_id", predictor_id)
        )
    model_type = predictor.get("model_type", "linear")
    if "model_type" not in predictor:
        completions.append(
            MetadataCompletion("model_type", "default_linear_model", model_type)
        )

    model = PredictorModel(
        predictor_id=str(predictor_id),
        model_type=str(model_type),
        feature_names=tuple(str(item) for item in feature_names),
        coefficients=tuple(float(item) for item in coefficients),
        intercept=float(intercept),
        config_generation=int(config_generation),
        retrain_generation=int(predictor.get("retrain_generation", 0)),
        created_ms=created_ms,
        trained_until_ms=trained_until_ms,
        origin="snapshot",
    )
    return SnapshotBundle(
        version="v1",
        current=model,
        models=(model,),
        tombstones=(),
        completions=tuple(completions),
        created_ms=created_ms,
        source_path=str(path),
    )


def _read_v2(path: str | Path, manifest: dict[str, Any]) -> SnapshotBundle:
    header = manifest.get("header", {})
    if not isinstance(header, dict):
        raise DriftError(E_SNAPSHOT_INVALID, "invalid v2 header")
    base = Path(path).parent
    shards = manifest.get("shards")
    if not isinstance(shards, list) or not shards:
        raise DriftError(E_SNAPSHOT_INVALID, "v2 manifest has no shards")

    models: list[PredictorModel] = []
    for shard in shards:
        if not isinstance(shard, dict) or "path" not in shard:
            raise DriftError(E_SNAPSHOT_INVALID, "invalid v2 shard declaration")
        shard_path = base / str(shard["path"])
        expected_hash = shard.get("sha256")
        if expected_hash and sha256_file(shard_path) != expected_hash:
            raise DriftError(
                E_SNAPSHOT_INVALID,
                "v2 shard checksum mismatch",
                shard=str(shard_path),
            )
        shard_payload = read_json(shard_path)
        if not isinstance(shard_payload, dict) or _dispatch_version(shard_payload) != "v2":
            raise DriftError(E_SNAPSHOT_INVALID, "v2 shard version mismatch")
        for item in shard_payload.get("models", []):
            if not isinstance(item, dict):
                raise DriftError(E_SNAPSHOT_INVALID, "invalid v2 model entry")
            if "created_ms" not in item or "trained_until_ms" not in item:
                raise DriftError(
                    E_SNAPSHOT_INVALID,
                    "v2 models must carry absolute millisecond timestamps",
                )
            models.append(PredictorModel.from_dict(item, default_origin="v2_shard"))
    if not models:
        raise DriftError(E_SNAPSHOT_INVALID, "v2 snapshot contains no models")

    db_path = base / str(manifest.get("metadata_db", "predictor.v2.sqlite"))
    expected_db_hash = manifest.get("metadata_db_sha256")
    if expected_db_hash and sha256_file(db_path) != expected_db_hash:
        raise DriftError(
            E_SNAPSHOT_INVALID,
            "v2 metadata database checksum mismatch",
            database=str(db_path),
        )
    completions, tombstones, metadata = _read_v2_metadata(db_path)
    current_id = (
        manifest.get("current_model")
        or metadata.get("current_model")
        or model_identifier(models[-1])
    )
    by_id = {model_identifier(model): model for model in models}
    if str(current_id) not in by_id:
        raise DriftError(
            E_SNAPSHOT_INVALID,
            "v2 current model is absent from shards",
            current_model=current_id,
        )
    current = by_id[str(current_id)]
    current = PredictorModel(
        predictor_id=current.predictor_id,
        model_type=current.model_type,
        feature_names=current.feature_names,
        coefficients=current.coefficients,
        intercept=current.intercept,
        config_generation=current.config_generation,
        retrain_generation=current.retrain_generation,
        created_ms=current.created_ms,
        trained_until_ms=current.trained_until_ms,
        origin="snapshot",
    )
    normalized_models = tuple(
        model
        if model_identifier(model) == model_identifier(current)
        else PredictorModel(
            predictor_id=model.predictor_id,
            model_type=model.model_type,
            feature_names=model.feature_names,
            coefficients=model.coefficients,
            intercept=model.intercept,
            config_generation=model.config_generation,
            retrain_generation=model.retrain_generation,
            created_ms=model.created_ms,
            trained_until_ms=model.trained_until_ms,
            origin="snapshot",
        )
        for model in models
    )
    created_ms = int(
        header.get(
            "absolute_time_ms",
            metadata.get("created_ms", current.created_ms),
        )
    )
    return SnapshotBundle(
        version="v2",
        current=current,
        models=normalized_models,
        tombstones=tuple(tombstones),
        completions=tuple(completions),
        created_ms=created_ms,
        source_path=str(path),
    )


def _read_v2_metadata(
    path: Path,
) -> tuple[list[MetadataCompletion], list[Tombstone], dict[str, Any]]:
    if not path.exists():
        raise DriftError(
            E_SNAPSHOT_INVALID, "v2 metadata database is missing", path=str(path)
        )
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise DriftError(E_SNAPSHOT_INVALID, "cannot open v2 metadata database") from exc
    try:
        metadata_rows = connection.execute(
            "SELECT key, value FROM metadata ORDER BY key"
        ).fetchall()
        completion_rows = connection.execute(
            "SELECT field, source, filled_value FROM completion_log ORDER BY id"
        ).fetchall()
        tombstone_rows = connection.execute(
            "SELECT predictor_id, target_retrain_generation, reason, created_ms "
            "FROM tombstones ORDER BY id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise DriftError(
            E_SNAPSHOT_INVALID, "invalid v2 metadata schema"
        ) from exc
    finally:
        connection.close()

    metadata: dict[str, Any] = {}
    for key, raw_value in metadata_rows:
        try:
            metadata[str(key)] = json.loads(raw_value)
        except (TypeError, json.JSONDecodeError):
            metadata[str(key)] = raw_value
    completions = [
        MetadataCompletion(
            field=str(field),
            source=str(source),
            value=json.loads(value),
        )
        for field, source, value in completion_rows
    ]
    tombstones = [
        Tombstone(
            predictor_id=str(predictor_id),
            target_retrain_generation=int(target),
            reason=str(reason),
            created_ms=int(created_ms),
        )
        for predictor_id, target, reason, created_ms in tombstone_rows
    ]
    return completions, tombstones, metadata


def write_v1(
    bundle: SnapshotBundle,
    path: str | Path,
    *,
    drops: Iterable[DropRecord] = (),
    lock_path: str | Path | None = None,
) -> None:
    target = Path(path)
    model = bundle.current
    parameters: dict[str, Any] = {"intercept": stable_float(model.intercept)}
    for index, coefficient in enumerate(model.coefficients):
        parameters[f"coefficient.{index}"] = stable_float(coefficient)
    payload = {
        "version": "v1",
        "header": {"format": "gsb-predictor", "schema_version": 1},
        "created_relative_s": stable_float(model.created_ms / 1000.0),
        "config_generation": model.config_generation,
        "predictor": {
            "id": model.predictor_id,
            "predictor_id": model.predictor_id,
            "model_type": model.model_type,
            "feature_names": list(model.feature_names),
            "coefficients": [stable_float(item) for item in model.coefficients],
            "intercept": stable_float(model.intercept),
            "retrain_generation": model.retrain_generation,
            "trained_until_relative_s": stable_float(
                model.trained_until_ms / 1000.0
            ),
        },
        "parameters": parameters,
        "dropped_incremental_generations": [
            item.to_dict() for item in drops
        ],
    }
    with FileCommitLock(resolve_commit_lock(lock_path)):
        atomic_write_json(target, payload)


def write_v2(
    bundle: SnapshotBundle,
    manifest_path: str | Path,
    *,
    lock_path: str | Path | None = None,
) -> None:
    target = Path(manifest_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    models = list(bundle.models)
    if not any(model_identifier(item) == model_identifier(bundle.current) for item in models):
        models.append(bundle.current)
    models.sort(
        key=lambda item: (
            item.config_generation,
            item.retrain_generation,
            item.created_ms,
            model_identifier(item),
        )
    )
    shard_payload = {
        "version": "v2",
        "header": {"format": "gsb-predictor-shard", "schema_version": 2},
        "models": [model.to_dict() for model in models],
    }
    shard_bytes = canonical_bytes(shard_payload, newline=True)
    shard_hash = sha256_bytes(shard_bytes)
    shard_name = f"predictor.v2.{shard_hash[:16]}.models.json"
    db_name = f"predictor.v2.{shard_hash[:16]}.sqlite"
    shard_path = target.parent / shard_name
    db_path = target.parent / db_name

    with FileCommitLock(resolve_commit_lock(lock_path)):
        atomic_write_bytes(shard_path, shard_bytes)
        _write_v2_database(
            db_path,
            models=models,
            tombstones=list(bundle.tombstones),
            completions=list(bundle.completions),
            current=bundle.current,
            created_ms=bundle.created_ms,
        )
        db_hash = sha256_file(db_path)
        manifest = {
            "version": "v2",
            "header": {
                "format": "gsb-predictor",
                "schema_version": 2,
                "absolute_time_ms": bundle.created_ms,
            },
            "current_model": model_identifier(bundle.current),
            "metadata_db": db_name,
            "metadata_db_sha256": db_hash,
            "shards": [
                {
                    "kind": "models",
                    "path": shard_name,
                    "sha256": shard_hash,
                }
            ],
        }
        atomic_write_json(target, manifest)


def _write_v2_database(
    path: Path,
    *,
    models: list[PredictorModel],
    tombstones: list[Tombstone],
    completions: list[MetadataCompletion],
    current: PredictorModel,
    created_ms: int,
) -> None:
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        connection = sqlite3.connect(temporary)
        try:
            connection.executescript(
                """
                PRAGMA journal_mode=DELETE;
                CREATE TABLE metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE completion_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    field TEXT NOT NULL,
                    source TEXT NOT NULL,
                    filled_value TEXT NOT NULL
                );
                CREATE TABLE tombstones (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    predictor_id TEXT NOT NULL,
                    target_retrain_generation INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_ms INTEGER NOT NULL
                );
                """
            )
            metadata = {
                "format": "gsb-predictor",
                "schema_version": 2,
                "created_ms": created_ms,
                "current_model": model_identifier(current),
                "config_generation": current.config_generation,
                "model_count": len(models),
            }
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                [(key, json.dumps(value, sort_keys=True)) for key, value in metadata.items()],
            )
            connection.executemany(
                "INSERT INTO completion_log(field, source, filled_value) "
                "VALUES (?, ?, ?)",
                [
                    (
                        item.field,
                        item.source,
                        json.dumps(item.value, sort_keys=True),
                    )
                    for item in completions
                ],
            )
            connection.executemany(
                "INSERT INTO tombstones("
                "predictor_id, target_retrain_generation, reason, created_ms"
                ") VALUES (?, ?, ?, ?)",
                [
                    (
                        item.predictor_id,
                        item.target_retrain_generation,
                        item.reason,
                        item.created_ms,
                    )
                    for item in tombstones
                ],
            )
            connection.commit()
        finally:
            connection.close()
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def migrate_v1_to_v2(
    source: str | Path,
    target: str | Path,
    *,
    epoch_ms: int = 0,
    lock_path: str | Path | None = None,
) -> MigrationResult:
    bundle = read_snapshot(source, epoch_ms=epoch_ms)
    if bundle.version != "v1":
        raise DriftError(E_SNAPSHOT_INVALID, "upgrade source is not v1")
    upgraded = SnapshotBundle(
        version="v2",
        current=bundle.current,
        models=bundle.models,
        tombstones=(),
        completions=bundle.completions,
        created_ms=bundle.created_ms,
        source_path=str(target),
    )
    write_v2(upgraded, target, lock_path=lock_path)
    return MigrationResult(
        target_version="v2",
        output_path=str(target),
        completions=bundle.completions,
        drops=(),
    )


def migrate_v2_to_v1(
    source: str | Path,
    target: str | Path,
    *,
    lock_path: str | Path | None = None,
) -> MigrationResult:
    bundle = read_snapshot(source)
    if bundle.version != "v2":
        raise DriftError(E_SNAPSHOT_INVALID, "downgrade source is not v2")
    base_models = [
        model for model in bundle.models if model.retrain_generation == 0
    ]
    if not base_models:
        base_models = sorted(bundle.models, key=lambda item: item.created_ms)[:1]
    selected = (
        bundle.current
        if bundle.current.retrain_generation == 0
        else sorted(base_models, key=lambda item: (item.retrain_generation, item.created_ms))[0]
    )
    drops = [
        DropRecord(
            predictor_id=model.predictor_id,
            config_generation=model.config_generation,
            retrain_generation=model.retrain_generation,
            reason="E_INCREMENTAL_RETRAIN_DROPPED",
        )
        for model in sorted(
            bundle.models,
            key=lambda item: (
                item.predictor_id,
                item.config_generation,
                item.retrain_generation,
            ),
        )
        if model.retrain_generation > 0
    ]
    if selected.retrain_generation > 0:
        selected = PredictorModel(
            predictor_id=selected.predictor_id,
            model_type=selected.model_type,
            feature_names=selected.feature_names,
            coefficients=selected.coefficients,
            intercept=selected.intercept,
            config_generation=selected.config_generation,
            retrain_generation=0,
            created_ms=selected.created_ms,
            trained_until_ms=selected.trained_until_ms,
            origin="snapshot",
        )
    downgraded = SnapshotBundle(
        version="v1",
        current=selected,
        models=(selected,),
        tombstones=bundle.tombstones,
        completions=bundle.completions,
        created_ms=bundle.created_ms,
        source_path=str(target),
    )
    write_v1(downgraded, target, drops=drops, lock_path=lock_path)
    return MigrationResult(
        target_version="v1",
        output_path=str(target),
        completions=bundle.completions,
        drops=tuple(drops),
    )


def snapshot_error_replay(path: str | Path) -> dict[str, Any]:
    """Return a stable error-code record instead of raising."""
    try:
        bundle = read_snapshot(path)
        return {
            "ok": True,
            "version": bundle.version,
            "current_model": bundle.current_model_id,
        }
    except DriftError as exc:
        return {"ok": False, **exc.to_dict()}

