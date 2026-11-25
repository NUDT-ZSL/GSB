"""Shared numeric semantics for float64, float32 and scaled fixed point."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .errors import precision_invalid
from .hashing import sha256_json


_ROUNDING_MODES = {"nearest_even", "half_up", "floor", "ceil", "trunc"}


@dataclass(frozen=True)
class Precision:
    name: str
    kind: str
    rounding: str = "nearest_even"
    scale: int | float | None = None

    @classmethod
    def from_config(cls, config: dict[str, Any], profile_name: str | None = None) -> "Precision":
        precision_cfg = config.get("precision", {})
        name = profile_name or precision_cfg.get("default", "float64")
        profiles = precision_cfg.get("profiles", {})
        if name not in profiles and name not in {"float64", "float32", "fixed"}:
            raise precision_invalid(f"unknown precision profile: {name}", profile=name)
        profile = dict(profiles.get(name, {}))
        kind = profile.get("kind", name if name in {"float64", "float32", "fixed"} else "")
        if kind not in {"float64", "float32", "fixed"}:
            raise precision_invalid(f"precision profile has invalid kind: {kind}", profile=name)
        rounding = profile.get("rounding", precision_cfg.get("rounding", "nearest_even"))
        if rounding not in _ROUNDING_MODES:
            raise precision_invalid(f"unknown rounding mode: {rounding}", rounding=rounding)
        scale = profile.get("scale", precision_cfg.get("fixed_scale"))
        if kind == "fixed":
            if scale is None or not isinstance(scale, (int, float)) or scale <= 0:
                raise precision_invalid("fixed precision requires a positive scale", scale=scale)
            if isinstance(scale, float) and not float(scale).is_integer():
                # A non-integer factor is accepted as a scaling factor but the
                # raw representation remains an integer grid count.
                scale_value: int | float = float(scale)
            else:
                scale_value = int(scale)
        else:
            scale_value = None
        return cls(name=name, kind=kind, rounding=rounding, scale=scale_value)

    @property
    def numpy_dtype(self) -> type[np.generic]:
        if self.kind == "float32":
            return np.float32
        return np.float64

    def round_scalar(self, value: float) -> float:
        arr = self.round_array(np.asarray(value, dtype=np.float64))
        return float(arr)

    def _rounded_scaled(self, scaled: np.ndarray) -> np.ndarray:
        mode = self.rounding
        if mode == "nearest_even":
            return np.rint(scaled)
        if mode == "half_up":
            return np.floor(scaled + 0.5)
        if mode == "floor":
            return np.floor(scaled)
        if mode == "ceil":
            return np.ceil(scaled)
        if mode == "trunc":
            return np.trunc(scaled)
        raise precision_invalid(f"unsupported rounding mode: {mode}")

    def quantize_raw(self, value: Any) -> np.ndarray:
        arr = np.asarray(value, dtype=np.float64)
        if self.kind == "float64":
            return arr.astype(np.float64, copy=True)
        if self.kind == "float32":
            return arr.astype(np.float32)
        assert self.scale is not None
        scaled = arr * np.float64(self.scale)
        return self._rounded_scaled(scaled).astype(np.int64)

    def quantize(self, value: Any) -> np.ndarray:
        """Return the value after one complete representation/operator boundary."""
        raw = self.quantize_raw(value)
        if self.kind == "fixed":
            return raw.astype(np.float64) / np.float64(self.scale)
        return raw.astype(np.float64)

    q = quantize

    def digest(self) -> str:
        return sha256_json(
            {"name": self.name, "kind": self.kind, "rounding": self.rounding, "scale": self.scale}
        )

    # Shared operator semantics. Every primitive exits through ``q`` so that
    # profile differences are caused by representation loss rather than a
    # different algorithmic path.
    def add(self, a: Any, b: Any) -> np.ndarray:
        return self.q(np.add(self.q(a), self.q(b)))

    def sub(self, a: Any, b: Any) -> np.ndarray:
        return self.q(np.subtract(self.q(a), self.q(b)))

    def mul(self, a: Any, b: Any) -> np.ndarray:
        return self.q(np.multiply(self.q(a), self.q(b)))

    def div(self, a: Any, b: Any) -> np.ndarray:
        return self.q(np.divide(self.q(a), self.q(b)))

    def square(self, a: Any) -> np.ndarray:
        qa = self.q(a)
        return self.q(np.multiply(qa, qa))

    def sqrt(self, a: Any) -> np.ndarray:
        return self.q(np.sqrt(np.maximum(self.q(a), 0.0)))

    def matmul(self, a: Any, b: Any) -> np.ndarray:
        return self.q(np.matmul(self.q(a), self.q(b)))

    def sum(self, a: Any, axis: int | tuple[int, ...] | None = None) -> np.ndarray:
        return self.q(np.sum(self.q(a), axis=axis))

    def mean(self, a: Any, axis: int | tuple[int, ...] | None = None) -> np.ndarray:
        return self.q(np.mean(self.q(a), axis=axis))

    def var(self, a: Any, axis: int | tuple[int, ...] | None = None) -> np.ndarray:
        qa = self.q(a)
        return self.q(np.var(qa, axis=axis))

    def config_view(self) -> dict[str, Any]:
        return {
            "profile": self.name,
            "kind": self.kind,
            "rounding": self.rounding,
            "scale": self.scale,
            "digest": self.digest(),
        }


def all_profiles(config: dict[str, Any]) -> Iterable[Precision]:
    names = ["float64", "float32", "fixed"]
    return [Precision.from_config(config, name) for name in names]
