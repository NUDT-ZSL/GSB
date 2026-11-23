from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .errors import BusinessError
from .models import JobSpec
from .util import (
    array_content_hash,
    atomic_write_json,
    npy_bytes,
    read_json,
    sanitize_component,
    sha256_bytes,
)
from .workspace import Workspace


class InputStore:
    """Owns the deterministic input tensor files and their detection metadata."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.paths = workspace.paths
        self.paths.ensure()
        self._meta = self._load_meta()

    def _load_meta(self) -> dict[str, Any]:
        raw = read_json(self.paths.input_meta, default={"jobs": {}})
        if not isinstance(raw, dict) or not isinstance(raw.get("jobs", {}), dict):
            raise BusinessError(
                "E_CACHE_CORRUPT", "input metadata is malformed", field=str(self.paths.input_meta)
            )
        raw.setdefault("jobs", {})
        return raw

    def save(self) -> None:
        atomic_write_json(self.paths.input_meta, self._meta, pretty=True)

    def path_for(self, job_id: str) -> Path:
        return self.paths.inputs / f"{sanitize_component(job_id)}.npy"

    def materialize(self, job: JobSpec) -> tuple[np.ndarray, str]:
        path = self.path_for(job.job_id)
        signature = job.input_signature()
        record = self._meta["jobs"].get(job.job_id)
        should_generate = not path.exists() or record is None or record.get("signature") != signature

        if should_generate:
            array = self._generate(job)
            raw = npy_bytes(array)
            from .util import atomic_write_bytes

            atomic_write_bytes(path, raw)
            self._meta["jobs"][job.job_id] = {
                "signature": signature,
                "shape": list(job.input_shape),
                "dtype": job.dtype,
                "seed": job.seed,
                "source": "seed",
                "content_hash": array_content_hash(array),
                "file_sha256": sha256_bytes(raw),
            }
        else:
            try:
                array = np.load(path, allow_pickle=False)
            except (OSError, ValueError) as exc:
                raise BusinessError(
                    "E_INPUT_INVALID",
                    f"cannot load input tensor for {job.job_id}: {exc}",
                    job_id=job.job_id,
                    field="input",
                    stage="validate_plan",
                ) from exc
            if tuple(array.shape) != job.input_shape:
                raise BusinessError(
                    "E_INPUT_INVALID",
                    f"input tensor shape {array.shape} does not match input_shape {job.input_shape}",
                    job_id=job.job_id,
                    field="input_shape",
                    stage="validate_plan",
                )
            requested_dtype = job.dtype_np()
            if array.dtype != requested_dtype:
                array = array.astype(requested_dtype, copy=False)

        array = np.ascontiguousarray(array)
        if not np.all(np.isfinite(array)):
            raise BusinessError(
                "E_INPUT_NONFINITE",
                "input tensor contains NaN or infinity",
                job_id=job.job_id,
                field="input",
                stage="validate_plan",
            )
        content_hash = array_content_hash(array)
        self._meta["jobs"][job.job_id]["content_hash"] = content_hash
        self._meta["jobs"][job.job_id]["file_sha256"] = sha256_bytes(npy_bytes(array))
        return array, content_hash

    def _generate(self, job: JobSpec) -> np.ndarray:
        try:
            generator = np.random.Generator(np.random.PCG64(job.seed))
            values = generator.standard_normal(size=job.input_shape, dtype=np.float64)
            values = values.astype(job.dtype_np(), copy=False)
            with np.errstate(over="raise", invalid="raise"):
                np.add(values, 0, out=values)
            return np.ascontiguousarray(values)
        except (FloatingPointError, OverflowError, ValueError) as exc:
            raise BusinessError(
                "E_INPUT_INVALID",
                f"failed to deterministically generate input for {job.job_id}: {exc}",
                job_id=job.job_id,
                field="seed",
                stage="validate_plan",
            ) from exc

    def mutate_flat(self, job: JobSpec, flat_index: int, value: float) -> dict[str, Any]:
        array, before = self.materialize(job)
        if not isinstance(flat_index, int) or flat_index < 0 or flat_index >= array.size:
            raise BusinessError(
                "E_INPUT_INVALID",
                f"flat index {flat_index} is outside tensor of size {array.size}",
                job_id=job.job_id,
                field="flat_index",
                stage="validate_plan",
            )
        if not np.isfinite(value):
            raise BusinessError(
                "E_INPUT_INVALID",
                "replacement value must be finite",
                job_id=job.job_id,
                field="value",
                stage="validate_plan",
            )
        mutated = array.copy()
        mutated.reshape(-1)[flat_index] = np.asarray(value, dtype=array.dtype)
        if not np.all(np.isfinite(mutated)):
            raise BusinessError(
                "E_INPUT_INVALID",
                "replacement value overflows the job dtype",
                job_id=job.job_id,
                field="value",
                stage="validate_plan",
            )
        from .util import atomic_write_bytes

        atomic_write_bytes(self.path_for(job.job_id), npy_bytes(mutated))
        after = array_content_hash(mutated)
        self._meta["jobs"][job.job_id].update(
            {
                "source": "manual",
                "content_hash": after,
                "file_sha256": sha256_bytes(npy_bytes(mutated)),
            }
        )
        self.save()
        return {
            "job_id": job.job_id,
            "flat_index": flat_index,
            "value": float(value),
            "before_hash": before,
            "after_hash": after,
            "input_path": str(self.path_for(job.job_id)),
        }

    def mutate_scale(self, job: JobSpec, scale: float) -> dict[str, Any]:
        array, before = self.materialize(job)
        if not np.isfinite(scale):
            raise BusinessError(
                "E_INPUT_INVALID",
                "scale must be finite",
                job_id=job.job_id,
                field="scale",
                stage="validate_plan",
            )
        mutated = np.ascontiguousarray(array.astype(np.float64) * float(scale))
        if not np.all(np.isfinite(mutated)):
            raise BusinessError(
                "E_INPUT_INVALID",
                "scaled input contains NaN or infinity",
                job_id=job.job_id,
                field="scale",
                stage="validate_plan",
            )
        mutated = mutated.astype(array.dtype, copy=False)
        if not np.all(np.isfinite(mutated)):
            raise BusinessError(
                "E_INPUT_INVALID",
                "scaled input overflows the job dtype",
                job_id=job.job_id,
                field="scale",
                stage="validate_plan",
            )
        from .util import atomic_write_bytes

        atomic_write_bytes(self.path_for(job.job_id), npy_bytes(mutated))
        after = array_content_hash(mutated)
        self._meta["jobs"][job.job_id].update(
            {
                "source": "manual",
                "content_hash": after,
                "file_sha256": sha256_bytes(npy_bytes(mutated)),
            }
        )
        self.save()
        return {
            "job_id": job.job_id,
            "scale": float(scale),
            "before_hash": before,
            "after_hash": after,
            "input_path": str(self.path_for(job.job_id)),
        }

