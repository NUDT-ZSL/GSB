"""Audit policy and budget configuration."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass

from .constants import ALGORITHM_WHITELIST_VERSION
from .errors import E_PARAM, KlpConfigError


@dataclass(frozen=True)
class AuditConfig:
    """Deterministic policy knobs plus real-resource budgets.

    ``concurrency`` is operational only and deliberately excluded from the
    checkpoint policy fingerprint so changing it cannot invalidate reusable
    session conclusions.
    """

    concurrency: int = 4
    session_timeout_seconds: float | None = 30.0
    max_derivations_per_session: int = 50_000
    scale_bytes_threshold: int = 1_048_576
    scale_derived_threshold: int = 10_000
    sampling_ratio: float = 0.25
    sample_chunk_size: int = 8
    checkpoint_interval: int = 1
    algorithm_policy_version: str = ALGORITHM_WHITELIST_VERSION

    def validate(self) -> None:
        if not isinstance(self.concurrency, int) or isinstance(self.concurrency, bool):
            raise KlpConfigError(E_PARAM, "concurrency must be an integer")
        if self.concurrency < 1:
            raise KlpConfigError(E_PARAM, "concurrency must be at least 1")
        if self.session_timeout_seconds is not None:
            timeout = self.session_timeout_seconds
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise KlpConfigError(E_PARAM, "session timeout must be numeric or null")
            if not math.isfinite(float(timeout)) or float(timeout) < 0:
                raise KlpConfigError(E_PARAM, "session timeout must be finite and non-negative")
        integer_fields = {
            "max_derivations_per_session": self.max_derivations_per_session,
            "scale_bytes_threshold": self.scale_bytes_threshold,
            "scale_derived_threshold": self.scale_derived_threshold,
            "sample_chunk_size": self.sample_chunk_size,
            "checkpoint_interval": self.checkpoint_interval,
        }
        for name, value in integer_fields.items():
            if not isinstance(value, int) or isinstance(value, bool):
                raise KlpConfigError(E_PARAM, f"{name} must be an integer")
            if value < 0:
                raise KlpConfigError(E_PARAM, f"{name} must be non-negative")
        if self.sample_chunk_size < 1:
            raise KlpConfigError(E_PARAM, "sample_chunk_size must be at least 1")
        if self.checkpoint_interval < 1:
            raise KlpConfigError(E_PARAM, "checkpoint_interval must be at least 1")
        if isinstance(self.sampling_ratio, bool) or not isinstance(
            self.sampling_ratio, (int, float)
        ):
            raise KlpConfigError(E_PARAM, "sampling_ratio must be numeric")
        ratio = float(self.sampling_ratio)
        if not math.isfinite(ratio) or ratio <= 0 or ratio > 1:
            raise KlpConfigError(E_PARAM, "sampling_ratio must be in (0, 1]")
        if not isinstance(self.algorithm_policy_version, str) or not self.algorithm_policy_version:
            raise KlpConfigError(E_PARAM, "algorithm_policy_version must be a non-empty string")

    def policy_fingerprint(self) -> str:
        payload = {
            "session_timeout_seconds": self.session_timeout_seconds,
            "max_derivations_per_session": self.max_derivations_per_session,
            "scale_bytes_threshold": self.scale_bytes_threshold,
            "scale_derived_threshold": self.scale_derived_threshold,
            "sampling_ratio": self.sampling_ratio,
            "sample_chunk_size": self.sample_chunk_size,
            "algorithm_policy_version": self.algorithm_policy_version,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def public_dict(self) -> dict[str, object]:
        data = asdict(self)
        data.pop("checkpoint_interval", None)
        return data

