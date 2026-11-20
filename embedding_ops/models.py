"""Manifest task model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import E_CONFIG_INVALID, E_MANIFEST_INVALID, business_error


def _require_int(value: Any, field: str, job_id: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise business_error(
            E_MANIFEST_INVALID,
            "manifest",
            job_id,
            f"{field} must be an integer",
            field=field,
            value=value,
        )
    return value


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    vocab_size: int
    embed_dim: int
    table_dtype: str
    index_set: Any
    gather_axis: int
    reduction_mode: str
    pad_index: int
    scale: float
    quant_scale: float
    quant_zero_point: int
    seed: int
    pad_policy: str | None = None

    @property
    def effective_pad_index(self) -> int | None:
        return None if self.pad_index < 0 else self.pad_index

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "JobSpec":
        if not isinstance(value, dict):
            raise business_error(
                E_MANIFEST_INVALID,
                "manifest",
                "unknown",
                "each JSONL record must be an object",
            )
        job_id_value = value.get("job_id")
        if not isinstance(job_id_value, str) or not job_id_value.strip():
            raise business_error(
                E_MANIFEST_INVALID,
                "manifest",
                "unknown",
                "job_id must be a non-empty string",
            )
        job_id = job_id_value
        required = (
            "vocab_size",
            "embed_dim",
            "table_dtype",
            "index_set",
            "gather_axis",
            "reduction_mode",
            "pad_index",
            "scale",
            "quant_scale",
            "quant_zero_point",
            "seed",
        )
        missing = [name for name in required if name not in value]
        if missing:
            raise business_error(
                E_MANIFEST_INVALID,
                "manifest",
                job_id,
                "manifest record is missing required fields",
                missing=missing,
            )
        vocab_size = _require_int(value["vocab_size"], "vocab_size", job_id)
        embed_dim = _require_int(value["embed_dim"], "embed_dim", job_id)
        gather_axis = _require_int(value["gather_axis"], "gather_axis", job_id)
        pad_index = _require_int(value["pad_index"], "pad_index", job_id)
        quant_zero_point = _require_int(
            value["quant_zero_point"], "quant_zero_point", job_id
        )
        seed = _require_int(value["seed"], "seed", job_id)
        if vocab_size <= 0 or embed_dim <= 0:
            raise business_error(
                E_CONFIG_INVALID,
                "manifest",
                job_id,
                "vocab_size and embed_dim must be positive",
                vocab_size=vocab_size,
                embed_dim=embed_dim,
            )
        table_dtype = value["table_dtype"]
        reduction_mode = value["reduction_mode"]
        if not isinstance(table_dtype, str):
            raise business_error(
                E_MANIFEST_INVALID,
                "manifest",
                job_id,
                "table_dtype must be a string",
            )
        if not isinstance(reduction_mode, str):
            raise business_error(
                E_MANIFEST_INVALID,
                "manifest",
                job_id,
                "reduction_mode must be a string",
            )
        try:
            scale = float(value["scale"])
            quant_scale = float(value["quant_scale"])
        except (TypeError, ValueError) as exc:
            raise business_error(
                E_MANIFEST_INVALID,
                "manifest",
                job_id,
                "scale and quant_scale must be numeric",
            ) from exc
        if not np.isfinite(scale):
            raise business_error(
                E_CONFIG_INVALID,
                "manifest",
                job_id,
                "scale must be finite",
                scale=scale,
            )
        if not np.isfinite(quant_scale):
            raise business_error(
                E_MANIFEST_INVALID,
                "manifest",
                job_id,
                "quant_scale must be finite",
                quant_scale=quant_scale,
            )
        pad_policy = value.get("pad_policy")
        if pad_policy is not None and not isinstance(pad_policy, str):
            raise business_error(
                E_MANIFEST_INVALID,
                "manifest",
                job_id,
                "pad_policy must be a string when present",
            )
        index_set = value["index_set"]
        if not isinstance(index_set, (dict, list, tuple)):
            raise business_error(
                E_MANIFEST_INVALID,
                "manifest",
                job_id,
                "index_set must be an object or array",
            )
        return cls(
            job_id=job_id,
            vocab_size=vocab_size,
            embed_dim=embed_dim,
            table_dtype=table_dtype,
            index_set=index_set,
            gather_axis=gather_axis,
            reduction_mode=reduction_mode,
            pad_index=pad_index,
            scale=scale,
            quant_scale=quant_scale,
            quant_zero_point=quant_zero_point,
            seed=seed,
            pad_policy=pad_policy,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "vocab_size": self.vocab_size,
            "embed_dim": self.embed_dim,
            "table_dtype": self.table_dtype,
            "index_set": self.index_set,
            "gather_axis": self.gather_axis,
            "reduction_mode": self.reduction_mode,
            "pad_index": self.pad_index,
            "scale": self.scale,
            "quant_scale": self.quant_scale,
            "quant_zero_point": self.quant_zero_point,
            "seed": self.seed,
            "pad_policy": self.pad_policy,
        }
