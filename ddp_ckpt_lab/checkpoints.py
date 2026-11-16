"""Atomic multi-shard checkpoints, format migration, and restore validation."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .canonical import (
    canonical_json_bytes,
    digest_array_map,
    digest_array_map_v1,
    generator_from_state,
    jsonable,
    sha256_bytes,
    sha256_file,
    sha256_json,
)
from .compatibility import CompatibilityResult, require_compatibility
from .config import TrainingConfig
from .data import DataCursor
from .errors import BusinessError
from .model import PARAMETER_NAMES
from .state import TrainingState, WorkerState, cursor_payload


FORMAT_VERSION = 2
LEGACY_FORMAT_VERSION = 1
SCHEMA_NAME = "ddp-ckpt-lab"
MANIFEST_FILE = "manifest.json"
COMPLETE_FILE = "COMPLETE"
SHARD_DIGEST_ALGORITHM = "sha256-array-map-v2"
LEGACY_SHARD_DIGEST_ALGORITHM = "sha256-v1-flat"
STATE_DIGEST_ALGORITHM = "sha256-state-v2"


@dataclass(frozen=True)
class CheckpointInfo:
    checkpoint_id: str
    path: str
    global_step: int
    format_version: int
    state_digest: str
    config_digest: str
    manifest_digest: str
    created_at_utc: str
    shard_digests: tuple[dict[str, Any], ...]

    @property
    def is_legacy(self) -> bool:
        return self.format_version < FORMAT_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "path": self.path,
            "global_step": self.global_step,
            "format_version": self.format_version,
            "state_digest": self.state_digest,
            "config_digest": self.config_digest,
            "manifest_digest": self.manifest_digest,
            "created_at_utc": self.created_at_utc,
            "shards": list(self.shard_digests),
        }


@dataclass(frozen=True)
class ScanResult:
    checkpoints: tuple[CheckpointInfo, ...]
    residues: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "checkpoints": [info.as_dict() for info in self.checkpoints],
            "residues": list(self.residues),
            "ok": not self.residues,
        }


@dataclass(frozen=True)
class MigrationReport:
    source_format_version: int
    target_format_version: int
    strategy: str
    steps: tuple[str, ...]
    source_state_digest: str
    restored_state_digest: str
    source_payload_digest: str
    restored_payload_digest: str
    source_config_digest: str
    target_config_digest: str
    source_state_version: str
    target_state_version: str
    learning_rate_changed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_format_version": self.source_format_version,
            "target_format_version": self.target_format_version,
            "strategy": self.strategy,
            "steps": list(self.steps),
            "source_state_digest": self.source_state_digest,
            "restored_state_digest": self.restored_state_digest,
            "source_payload_digest": self.source_payload_digest,
            "restored_payload_digest": self.restored_payload_digest,
            "source_config_digest": self.source_config_digest,
            "target_config_digest": self.target_config_digest,
            "source_state_version": self.source_state_version,
            "target_state_version": self.target_state_version,
            "learning_rate_changed": self.learning_rate_changed,
        }


@dataclass(frozen=True)
class RestoreResult:
    state: TrainingState
    source_config: TrainingConfig
    effective_config: TrainingConfig
    info: CheckpointInfo
    migration: MigrationReport

    def as_report(self) -> dict[str, Any]:
        return {
            "status": "OK",
            "checkpoint": self.info.as_dict(),
            "source_config": self.source_config.as_dict(),
            "effective_config": self.effective_config.as_dict(),
            "effective_config_digest": self.effective_config.config_digest,
            "restored_state_digest": self.state.state_digest(self.effective_config),
            "restored_payload_digest": self.state.payload_digest(),
            "global_step": self.state.global_step,
            "migration": self.migration.as_dict(),
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fsync_stream(stream: Any) -> None:
    stream.flush()
    os.fsync(stream.fileno())


def _write_json_file(path: Path, value: Any) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(
            value,
            stream,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        stream.write("\n")
        _fsync_stream(stream)


def _best_effort_fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _safe_relative_file(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    root_resolved = root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise BusinessError(
            "E_RESTORE",
            "检查点清单包含越界文件路径",
            root=str(root_resolved),
            file=relative,
        ) from exc
    return candidate


def _arrays_for_worker(worker: WorkerState) -> dict[str, np.ndarray]:
    arrays: dict[str, np.ndarray] = {}
    for name in PARAMETER_NAMES:
        arrays[f"param__{name}"] = np.asarray(worker.parameters[name], dtype="<f8")
        arrays[f"momentum__{name}"] = np.asarray(worker.momentum[name], dtype="<f8")
    arrays["cursor_order"] = np.asarray(worker.cursor.order, dtype="<i8")
    return arrays


def _legacy_state_digest(manifest: dict[str, Any]) -> str:
    payload = {
        "global_step": int(manifest["global_step"]),
        "config": manifest["training_config"],
        "workers": [
            {
                "worker_id": int(shard["worker_id"]),
                "shard_digest": shard["digest"],
                "params_digest": shard["params_digest"],
                "momentums_digest": shard["momentums_digest"],
            }
            for shard in manifest["shards"]
        ],
    }
    return sha256_bytes(canonical_json_bytes(payload))


class CheckpointStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def ensure_ready(self) -> dict[str, Any]:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BusinessError(
                "E_CHECKPOINT",
                "检查点目录无法创建",
                path=str(self.root),
                error=str(exc),
            ) from exc
        if not os.access(self.root, os.R_OK | os.W_OK | os.X_OK):
            raise BusinessError(
                "E_CHECKPOINT",
                "检查点目录不可读写",
                path=str(self.root),
            )
        return probe_atomic_storage(self.root)

    def checkpoint_name(self, global_step: int, suffix: str = "") -> str:
        return f"checkpoint_{global_step:08d}{suffix}"

    def save(self, state: TrainingState, config: TrainingConfig) -> CheckpointInfo:
        self.ensure_ready()
        state.validate(config)
        name = self.checkpoint_name(state.global_step)
        final_path = self.root / name
        if final_path.exists():
            raise BusinessError(
                "E_CHECKPOINT",
                "目标检查点已存在，拒绝覆盖完整检查点",
                path=str(final_path),
            )

        created_at = _utc_now()
        shard_payloads: list[tuple[WorkerState, dict[str, np.ndarray], str]] = []
        shard_manifest: list[dict[str, Any]] = []
        for worker in state.workers:
            arrays = _arrays_for_worker(worker)
            shard_digest = digest_array_map(arrays)
            shard_payloads.append((worker, arrays, shard_digest))
            shard_manifest.append(
                {
                    "worker_id": worker.worker_id,
                    "file": f"shards/worker_{worker.worker_id}.npz",
                    "digest": shard_digest,
                    "digest_algorithm": SHARD_DIGEST_ALGORITHM,
                    "params_digest": worker.params_digest,
                    "momentums_digest": worker.momentums_digest,
                    "cursor": {
                        "epoch": worker.cursor.epoch,
                        "position": worker.cursor.position,
                        "order_length": int(worker.cursor.order.size),
                    },
                    "rng_state": jsonable(worker.cursor.rng_state),
                }
            )

        manifest = {
            "schema": SCHEMA_NAME,
            "format_version": FORMAT_VERSION,
            "digest_algorithms": {
                "shard": SHARD_DIGEST_ALGORITHM,
                "state": STATE_DIGEST_ALGORITHM,
            },
            "checkpoint_id": name,
            "created_at_utc": created_at,
            "global_step": state.global_step,
            "training_config": config.as_dict(),
            "config_digest": config.config_digest,
            "state_digest": state.state_digest(config),
            "payload_digest": state.payload_digest(),
            "workers": config.workers,
            "worker_shards": shard_manifest,
        }

        def write_payload(temp_path: Path) -> None:
            shard_dir = temp_path / "shards"
            shard_dir.mkdir(parents=True, exist_ok=False)
            for worker, arrays, _ in shard_payloads:
                shard_path = shard_dir / f"worker_{worker.worker_id}.npz"
                with shard_path.open("wb") as stream:
                    np.savez(stream, **arrays)
                    _fsync_stream(stream)
            _write_json_file(temp_path / MANIFEST_FILE, manifest)
            digest = sha256_file(temp_path / MANIFEST_FILE)
            complete_path = temp_path / COMPLETE_FILE
            with complete_path.open("w", encoding="ascii", newline="\n") as stream:
                stream.write(f"{digest}\n")
                _fsync_stream(stream)
            _best_effort_fsync_directory(temp_path)

        self._commit_directory(name, write_payload)
        return self._read_info(final_path)

    def save_legacy_v1(
        self,
        state: TrainingState,
        config: TrainingConfig,
    ) -> CheckpointInfo:
        """Create a deliberate v1 checkpoint used to test replace-rebuild migration."""

        self.ensure_ready()
        state.validate(config)
        name = self.checkpoint_name(state.global_step, suffix="_v1")
        final_path = self.root / name
        if final_path.exists():
            raise BusinessError(
                "E_CHECKPOINT",
                "目标 v1 检查点已存在",
                path=str(final_path),
            )
        created_at = _utc_now()
        payloads: list[tuple[WorkerState, dict[str, np.ndarray], str]] = []
        shards: list[dict[str, Any]] = []
        for worker in state.workers:
            arrays = _arrays_for_worker(worker)
            shard_digest = digest_array_map_v1(arrays)
            payloads.append((worker, arrays, shard_digest))
            shards.append(
                {
                    "worker_id": worker.worker_id,
                    "file": f"worker_{worker.worker_id}.npz",
                    "digest": shard_digest,
                    "params_digest": worker.params_digest,
                    "momentums_digest": worker.momentums_digest,
                    "cursor": {
                        "epoch": worker.cursor.epoch,
                        "position": worker.cursor.position,
                    },
                    "rng_state": jsonable(worker.cursor.rng_state),
                }
            )
        manifest = {
            "format": "ddp-ckpt-lab-legacy",
            "format_version": LEGACY_FORMAT_VERSION,
            "digest_algorithm": LEGACY_SHARD_DIGEST_ALGORITHM,
            "checkpoint_id": name,
            "created_at_utc": created_at,
            "global_step": state.global_step,
            "training_config": config.as_dict(),
            "payload_digest": state.payload_digest(),
            "shards": shards,
        }
        manifest["state_digest"] = _legacy_state_digest(manifest)

        def write_payload(temp_path: Path) -> None:
            for worker, arrays, _ in payloads:
                shard_path = temp_path / f"worker_{worker.worker_id}.npz"
                with shard_path.open("wb") as stream:
                    np.savez(stream, **arrays)
                    _fsync_stream(stream)
            _write_json_file(temp_path / MANIFEST_FILE, manifest)
            digest = sha256_file(temp_path / MANIFEST_FILE)
            with (temp_path / COMPLETE_FILE).open(
                "w", encoding="ascii", newline="\n"
            ) as stream:
                stream.write(f"{digest}\n")
                _fsync_stream(stream)
            _best_effort_fsync_directory(temp_path)

        self._commit_directory(name, write_payload)
        return self._read_info(final_path)

    def _commit_directory(self, name: str, writer: Callable[[Path], None]) -> None:
        temp_path = self.root / f".tmp-{name}-{uuid.uuid4().hex}"
        final_path = self.root / name
        try:
            temp_path.mkdir(parents=False, exist_ok=False)
            writer(temp_path)
            if final_path.exists():
                raise BusinessError(
                    "E_CHECKPOINT",
                    "检查点提交阶段发现目标已存在",
                    path=str(final_path),
                )
            os.replace(temp_path, final_path)
            _best_effort_fsync_directory(self.root)
        except BusinessError:
            shutil.rmtree(temp_path, ignore_errors=True)
            raise
        except Exception as exc:
            shutil.rmtree(temp_path, ignore_errors=True)
            raise BusinessError(
                "E_CHECKPOINT",
                "检查点原子写入失败",
                path=str(final_path),
                error=f"{type(exc).__name__}: {exc}",
            ) from exc

    def scan(self, *, strict: bool = False) -> ScanResult:
        if not self.root.exists():
            return ScanResult(checkpoints=(), residues=())
        checkpoints: list[CheckpointInfo] = []
        residues: list[dict[str, Any]] = []
        for entry in sorted(self.root.iterdir(), key=lambda item: item.name):
            if entry.name.startswith(".tmp-"):
                residues.append(
                    {
                        "code": "E_CHECKPOINT",
                        "reason": "incomplete_atomic_write_residue",
                        "path": str(entry),
                    }
                )
                continue
            if not entry.is_dir() or not entry.name.startswith("checkpoint_"):
                continue
            try:
                info = self._read_info(entry)
                checkpoints.append(info)
            except BusinessError as exc:
                residues.append(
                    {
                        "code": "E_CHECKPOINT",
                        "reason": "invalid_checkpoint_directory",
                        "path": str(entry),
                        "error": exc.to_dict(),
                    }
                )
            except Exception as exc:
                residues.append(
                    {
                        "code": "E_CHECKPOINT",
                        "reason": "malformed_checkpoint_directory",
                        "path": str(entry),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        checkpoints.sort(
            key=lambda info: (info.global_step, info.format_version, info.checkpoint_id)
        )
        result = ScanResult(
            checkpoints=tuple(checkpoints), residues=tuple(residues)
        )
        if strict and result.residues:
            raise BusinessError(
                "E_CHECKPOINT",
                "检测到不完整或无效的检查点残留",
                residues=list(result.residues),
            )
        return result

    def list_checkpoints(self, *, strict: bool = False) -> list[CheckpointInfo]:
        return list(self.scan(strict=strict).checkpoints)

    def latest(self) -> CheckpointInfo:
        checkpoints = self.list_checkpoints()
        if not checkpoints:
            raise BusinessError(
                "E_RESTORE",
                "没有可恢复的完整检查点",
                root=str(self.root),
            )
        return checkpoints[-1]

    def _read_info(self, path: Path) -> CheckpointInfo:
        manifest_path = path / MANIFEST_FILE
        complete_path = path / COMPLETE_FILE
        if not path.is_dir() or not manifest_path.is_file() or not complete_path.is_file():
            raise BusinessError(
                "E_CHECKPOINT",
                "检查点目录缺少清单或完成标记",
                path=str(path),
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            format_version = int(manifest["format_version"])
            global_step = int(manifest["global_step"])
            state_digest = str(manifest["state_digest"])
        except Exception as exc:
            raise BusinessError(
                "E_CHECKPOINT",
                "检查点清单无法解析",
                path=str(path),
                error=f"{type(exc).__name__}: {exc}",
            ) from exc
        if format_version == FORMAT_VERSION:
            config_payload = manifest.get("training_config", {})
            config_digest = str(
                manifest.get("config_digest")
                or sha256_json(config_payload)
            )
            shards = manifest.get("worker_shards", [])
        elif format_version == LEGACY_FORMAT_VERSION:
            config_payload = manifest.get("training_config", {})
            config_digest = sha256_json(config_payload)
            shards = manifest.get("shards", [])
        else:
            raise BusinessError(
                "E_RESTORE",
                "不支持的检查点格式版本",
                path=str(path),
                format_version=format_version,
            )
        try:
            complete_token = complete_path.read_text(encoding="ascii").strip()
        except Exception as exc:
            raise BusinessError(
                "E_CHECKPOINT",
                "检查点完成标记无法读取",
                path=str(path),
                error=f"{type(exc).__name__}: {exc}",
            ) from exc
        manifest_digest = sha256_file(manifest_path)
        if complete_token != manifest_digest:
            raise BusinessError(
                "E_CHECKPOINT",
                "检查点完成标记与清单摘要不一致",
                path=str(path),
                complete_token=complete_token,
                manifest_digest=manifest_digest,
            )
        return CheckpointInfo(
            checkpoint_id=path.name,
            path=str(path),
            global_step=global_step,
            format_version=format_version,
            state_digest=state_digest,
            config_digest=config_digest,
            manifest_digest=manifest_digest,
            created_at_utc=str(manifest.get("created_at_utc", "")),
            shard_digests=tuple(
                {
                    "worker_id": int(shard["worker_id"]),
                    "digest": str(shard["digest"]),
                    "file": str(shard["file"]),
                }
                for shard in shards
            ),
        )

    def load(
        self,
        checkpoint: CheckpointInfo | str | Path,
        requested_config: TrainingConfig | None = None,
    ) -> RestoreResult:
        initial_info = (
            checkpoint if isinstance(checkpoint, CheckpointInfo) else self._resolve(checkpoint)
        )
        path = Path(initial_info.path)
        info = self._read_info(path)
        try:
            manifest = json.loads((path / MANIFEST_FILE).read_text(encoding="utf-8"))
        except Exception as exc:
            raise BusinessError(
                "E_RESTORE",
                "恢复前无法读取检查点清单",
                path=str(path),
                error=str(exc),
            ) from exc
        try:
            format_version = int(manifest.get("format_version", -1))
            if format_version == FORMAT_VERSION:
                state, source_config = self._load_v2(path, manifest)
            elif format_version == LEGACY_FORMAT_VERSION:
                state, source_config = self._load_v1(path, manifest)
            else:
                raise BusinessError(
                    "E_RESTORE",
                    "不支持的检查点格式版本",
                    format_version=format_version,
                    path=str(path),
                )

            try:
                state.validate(source_config)
            except Exception as exc:
                raise BusinessError(
                    "E_RESTORE",
                    "恢复出的运行状态结构无效",
                    checkpoint=path.name,
                    error=str(exc),
                ) from exc

            effective_config = requested_config or source_config
            compatibility = require_compatibility(
                source_config,
                effective_config,
                source_format_version=format_version,
                target_format_version=FORMAT_VERSION,
            )
            restored_state_digest = state.state_digest(effective_config)
            migration = self._migration_report(
                source_format_version=format_version,
                compatibility=compatibility,
                source_state_digest=str(manifest["state_digest"]),
                restored_state_digest=restored_state_digest,
                source_payload_digest=str(manifest.get("payload_digest", "")),
                restored_payload_digest=state.payload_digest(),
                source_config=source_config,
                target_config=effective_config,
            )
            return RestoreResult(
                state=state,
                source_config=source_config,
                effective_config=effective_config,
                info=info,
                migration=migration,
            )
        except BusinessError:
            raise
        except Exception as exc:
            raise BusinessError(
                "E_RESTORE",
                "检查点恢复过程中发现畸形字段或无法重建的状态",
                checkpoint=path.name,
                error=f"{type(exc).__name__}: {exc}",
            ) from exc

    def _load_v2(
        self, path: Path, manifest: dict[str, Any]
    ) -> tuple[TrainingState, TrainingConfig]:
        if manifest.get("schema") != SCHEMA_NAME:
            raise BusinessError(
                "E_RESTORE",
                "检查点 schema 标识不匹配",
                expected=SCHEMA_NAME,
                actual=manifest.get("schema"),
            )
        algorithms = manifest.get("digest_algorithms", {})
        if algorithms.get("shard") != SHARD_DIGEST_ALGORITHM:
            raise BusinessError(
                "E_RESTORE",
                "v2 检查点分片摘要算法不受支持",
                expected=SHARD_DIGEST_ALGORITHM,
                actual=algorithms.get("shard"),
            )
        if algorithms.get("state") != STATE_DIGEST_ALGORITHM:
            raise BusinessError(
                "E_RESTORE",
                "v2 检查点状态摘要算法不受支持",
                expected=STATE_DIGEST_ALGORITHM,
                actual=algorithms.get("state"),
            )
        config = TrainingConfig.from_dict(manifest.get("training_config", {}))
        if manifest.get("config_digest") != config.config_digest:
            raise BusinessError(
                "E_RESTORE",
                "清单中的训练配置摘要不一致",
                expected=manifest.get("config_digest"),
                actual=config.config_digest,
            )
        workers = self._load_worker_shards(
            path,
            manifest.get("worker_shards", []),
            config,
            digest_function=digest_array_map,
            expected_algorithm=SHARD_DIGEST_ALGORITHM,
            digest_key="digest_algorithm",
        )
        state = TrainingState(
            global_step=int(manifest["global_step"]),
            workers=workers,
        )
        state.validate(config)
        actual_state_digest = state.state_digest(config)
        if actual_state_digest != manifest.get("state_digest"):
            raise BusinessError(
                "E_RESTORE",
                "恢复后的状态摘要与清单不一致",
                expected=manifest.get("state_digest"),
                actual=actual_state_digest,
            )
        if manifest.get("payload_digest") != state.payload_digest():
            raise BusinessError(
                "E_RESTORE",
                "恢复后的 worker 内容摘要与清单不一致",
                expected=manifest.get("payload_digest"),
                actual=state.payload_digest(),
            )
        return state, config

    def _load_v1(
        self, path: Path, manifest: dict[str, Any]
    ) -> tuple[TrainingState, TrainingConfig]:
        if manifest.get("digest_algorithm") != LEGACY_SHARD_DIGEST_ALGORITHM:
            raise BusinessError(
                "E_RESTORE",
                "v1 检查点摘要算法不受支持",
                expected=LEGACY_SHARD_DIGEST_ALGORITHM,
                actual=manifest.get("digest_algorithm"),
            )
        config = TrainingConfig.from_dict(manifest.get("training_config", {}))
        workers = self._load_worker_shards(
            path,
            manifest.get("shards", []),
            config,
            digest_function=digest_array_map_v1,
            expected_algorithm=LEGACY_SHARD_DIGEST_ALGORITHM,
            digest_key=None,
        )
        state = TrainingState(
            global_step=int(manifest["global_step"]),
            workers=workers,
        )
        state.validate(config)
        actual_legacy_digest = _legacy_state_digest(manifest)
        if actual_legacy_digest != manifest.get("state_digest"):
            raise BusinessError(
                "E_RESTORE",
                "v1 状态摘要与清单不一致",
                expected=manifest.get("state_digest"),
                actual=actual_legacy_digest,
            )
        if manifest.get("payload_digest") != state.payload_digest():
            raise BusinessError(
                "E_RESTORE",
                "v1 worker 内容摘要与清单不一致",
                expected=manifest.get("payload_digest"),
                actual=state.payload_digest(),
            )
        return state, config

    def _load_worker_shards(
        self,
        path: Path,
        shard_entries: list[dict[str, Any]],
        config: TrainingConfig,
        *,
        digest_function: Callable[[dict[str, np.ndarray]], str],
        expected_algorithm: str,
        digest_key: str | None,
    ) -> list[WorkerState]:
        if len(shard_entries) != config.workers:
            raise BusinessError(
                "E_RESTORE",
                "检查点 worker 分片数量不匹配",
                expected=config.workers,
                actual=len(shard_entries),
            )
        workers: list[WorkerState] = []
        for expected_worker_id, shard_entry in enumerate(shard_entries):
            worker_id = int(shard_entry["worker_id"])
            if worker_id != expected_worker_id:
                raise BusinessError(
                    "E_RESTORE",
                    "worker 分片顺序或编号不匹配",
                    expected=expected_worker_id,
                    actual=worker_id,
                )
            if digest_key is not None and shard_entry.get(digest_key) != expected_algorithm:
                raise BusinessError(
                    "E_RESTORE",
                    "分片摘要算法与清单声明不一致",
                    worker_id=worker_id,
                    expected=expected_algorithm,
                    actual=shard_entry.get(digest_key),
                )
            shard_path = _safe_relative_file(path, str(shard_entry["file"]))
            try:
                with np.load(shard_path, allow_pickle=False) as archive:
                    arrays = {
                        name: np.asarray(archive[name]).copy()
                        for name in archive.files
                    }
            except Exception as exc:
                raise BusinessError(
                    "E_RESTORE",
                    "worker 分片文件无法读取",
                    worker_id=worker_id,
                    file=str(shard_path),
                    error=f"{type(exc).__name__}: {exc}",
                ) from exc
            actual_digest = digest_function(arrays)
            if actual_digest != shard_entry.get("digest"):
                raise BusinessError(
                    "E_RESTORE",
                    "worker 分片内容摘要与清单不一致，拒绝恢复",
                    worker_id=worker_id,
                    file=str(shard_path),
                    expected=shard_entry.get("digest"),
                    actual=actual_digest,
                )
            expected_array_names = {
                *(f"param__{name}" for name in PARAMETER_NAMES),
                *(f"momentum__{name}" for name in PARAMETER_NAMES),
                "cursor_order",
            }
            if set(arrays) != expected_array_names:
                raise BusinessError(
                    "E_RESTORE",
                    "worker 分片字段集合不匹配",
                    worker_id=worker_id,
                    expected=sorted(expected_array_names),
                    actual=sorted(arrays),
                )
            parameters = {
                name: np.asarray(arrays[f"param__{name}"], dtype=np.float64)
                for name in PARAMETER_NAMES
            }
            momentum = {
                name: np.asarray(arrays[f"momentum__{name}"], dtype=np.float64)
                for name in PARAMETER_NAMES
            }
            cursor_meta = shard_entry.get("cursor", {})
            order = np.asarray(arrays["cursor_order"], dtype=np.int64)
            order_length = int(cursor_meta.get("order_length", order.size))
            position = int(cursor_meta.get("position", 0))
            epoch = int(cursor_meta.get("epoch", 0))
            if order_length != order.size:
                raise BusinessError(
                    "E_RESTORE",
                    "worker 数据游标置换长度与清单不一致",
                    worker_id=worker_id,
                    expected=order_length,
                    actual=int(order.size),
                )
            if position < 0 or position > order.size or epoch < 0:
                raise BusinessError(
                    "E_RESTORE",
                    "worker 数据游标位置或 epoch 非法",
                    worker_id=worker_id,
                    epoch=epoch,
                    position=position,
                    order_length=int(order.size),
                )
            rng_state = jsonable(shard_entry.get("rng_state", {}))
            try:
                generator_from_state(rng_state)
            except Exception as exc:
                raise BusinessError(
                    "E_RESTORE",
                    "worker 随机数状态无法重建",
                    worker_id=worker_id,
                    error=f"{type(exc).__name__}: {exc}",
                ) from exc
            cursor = DataCursor(
                epoch=epoch,
                position=position,
                order=order,
                rng_state=rng_state,
            )
            workers.append(
                WorkerState(
                    worker_id=worker_id,
                    parameters=parameters,
                    momentum=momentum,
                    cursor=cursor,
                )
            )
        return workers

    def _migration_report(
        self,
        *,
        source_format_version: int,
        compatibility: CompatibilityResult,
        source_state_digest: str,
        restored_state_digest: str,
        source_payload_digest: str,
        restored_payload_digest: str,
        source_config: TrainingConfig,
        target_config: TrainingConfig,
    ) -> MigrationReport:
        return MigrationReport(
            source_format_version=source_format_version,
            target_format_version=FORMAT_VERSION,
            strategy="replace_rebuild",
            steps=compatibility.migration_steps,
            source_state_digest=source_state_digest,
            restored_state_digest=restored_state_digest,
            source_payload_digest=source_payload_digest,
            restored_payload_digest=restored_payload_digest,
            source_config_digest=source_config.config_digest,
            target_config_digest=target_config.config_digest,
            source_state_version=compatibility.source_state_version,
            target_state_version=compatibility.target_state_version,
            learning_rate_changed=compatibility.learning_rate_changed,
        )

    def _resolve(self, checkpoint: str | Path) -> CheckpointInfo:
        candidate = Path(checkpoint)
        if candidate.is_absolute():
            path = candidate
        elif candidate.exists():
            path = candidate
        else:
            path = self.root / candidate
        if not path.exists():
            raise BusinessError(
                "E_RESTORE",
                "检查点路径不存在",
                path=str(path),
            )
        return self._read_info(path)

    def fingerprint(self, checkpoint: CheckpointInfo | str | Path) -> str:
        info = checkpoint if isinstance(checkpoint, CheckpointInfo) else self._resolve(checkpoint)
        path = Path(info.path)
        files: dict[str, str] = {}
        for entry in sorted(path.rglob("*"), key=lambda item: str(item.relative_to(path))):
            if entry.is_file():
                files[str(entry.relative_to(path)).replace(os.sep, "/")] = sha256_file(entry)
        return sha256_json(files)


def probe_atomic_storage(root: str | Path) -> dict[str, Any]:
    root_path = Path(root).resolve()
    root_path.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    source = root_path / f".tmp-atomic-probe-{token}"
    target = root_path / f".tmp-atomic-probe-{token}-renamed"
    try:
        source.mkdir()
        probe_file = source / "probe.txt"
        with probe_file.open("w", encoding="ascii", newline="\n") as stream:
            stream.write("ddp-ckpt-lab\n")
            _fsync_stream(stream)
        os.replace(source, target)
        observed = (target / "probe.txt").read_text(encoding="ascii")
        if observed != "ddp-ckpt-lab\n":
            raise OSError("atomic rename probe content mismatch")
        return {
            "status": "OK",
            "path": str(root_path),
            "writable": True,
            "atomic_directory_rename": True,
        }
    except Exception as exc:
        raise BusinessError(
            "E_CHECKPOINT",
            "检查点目录不满足原子重命名要求",
            path=str(root_path),
            error=f"{type(exc).__name__}: {exc}",
        ) from exc
    finally:
        shutil.rmtree(source, ignore_errors=True)
        shutil.rmtree(target, ignore_errors=True)
