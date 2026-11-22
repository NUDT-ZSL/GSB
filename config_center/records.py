from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .models import ConfigDecl


@dataclass(frozen=True)
class StoredConfig:
    key: str
    value_type: str
    value_text: str
    value_decimal: Decimal | None
    scale: int
    role: str | None
    fixed_int: int | None
    carried_in_units: Decimal
    discarded_units: Decimal
    residual_units: Decimal
    residual_value: Decimal
    conversion_id: int | None
    conversion_version: int | None

    def declaration(self) -> ConfigDecl:
        return ConfigDecl(
            key=self.key,
            value_type=self.value_type,
            value_text=self.value_text,
            value_decimal=self.value_decimal,
            scale=self.scale,
            role=self.role,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "type": self.value_type,
            "value": self.value_text,
            "scale": self.scale,
            "role": self.role,
            "fixed_int": self.fixed_int,
            "fixed_value": (
                None
                if self.fixed_int is None
                else _fixed_text(self.fixed_int, self.scale)
            ),
            "carried_in_units": _decimal_text(self.carried_in_units),
            "discarded_units": _decimal_text(self.discarded_units),
            "residual_units": _decimal_text(self.residual_units),
            "residual": _decimal_text(self.residual_value),
            "conversion_id": self.conversion_id,
            "conversion_version": self.conversion_version,
        }


@dataclass(frozen=True)
class ConfigBundle:
    namespace: str
    version: int
    digest: str
    content_digest: str
    logical_clock_ms: int
    configs: dict[str, StoredConfig]

    def by_role(self, role: str) -> StoredConfig:
        matches = [cfg for cfg in self.configs.values() if cfg.role == role]
        if not matches:
            raise KeyError(role)
        if len(matches) > 1:
            raise ValueError(f"role {role} is not unique")
        return matches[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "version": self.version,
            "digest": self.digest,
            "content_digest": self.content_digest,
            "logical_clock_ms": self.logical_clock_ms,
            "configs": {
                key: cfg.to_dict() for key, cfg in sorted(self.configs.items())
            },
        }


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _fixed_text(fixed_int: int, scale: int) -> str:
    return _decimal_text(Decimal(fixed_int) / Decimal(scale))

