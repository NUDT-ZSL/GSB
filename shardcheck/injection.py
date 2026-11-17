from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError


SUPPORTED_INJECTION_TYPES = {
    "budget_occupancy_out_of_range",
    "illegal_retention",
    "oversized_shard",
    "fingerprint_conflict",
    "generate_exception",
    "write_exception",
    "merge_exception",
}


@dataclass(frozen=True)
class Injection:
    injection_id: str
    injection_type: str
    round: int
    target_shard_index: int | None = None
    target_physical_index: int | None = None
    value: float | int | None = None
    raw: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "injection_id": self.injection_id,
            "type": self.injection_type,
            "round": self.round,
            "target_shard_index": self.target_shard_index,
            "target_physical_index": self.target_physical_index,
            "value": self.value,
        }


class InjectionTable:
    def __init__(self, injections: list[Injection], source_hash: str):
        self.injections = injections
        self.source_hash = source_hash

    @classmethod
    def load(cls, path: Path | None) -> "InjectionTable":
        if path is None:
            return cls([], "none")
        try:
            raw_bytes = path.read_bytes()
            if path.suffix.lower() == ".json":
                raw = json.loads(raw_bytes.decode("utf-8"))
            else:
                raw = yaml.safe_load(raw_bytes.decode("utf-8"))
        except Exception as exc:
            raise ConfigError(f"cannot read injection config {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("injection config must be a mapping")
        if raw.get("schema_version", 1) != 1:
            raise ConfigError("unsupported injection schema version")
        rows = raw.get("injections", [])
        if not isinstance(rows, list):
            raise ConfigError("injection config .injections must be a list")
        injections: list[Injection] = []
        seen: set[str] = set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ConfigError(f"injection {index + 1} must be a mapping")
            injection_id = row.get("id", row.get("injection_id"))
            injection_type = row.get("type")
            round_number = row.get("round")
            if not isinstance(injection_id, str) or not injection_id:
                raise ConfigError(f"injection {index + 1} has no id")
            if injection_id in seen:
                raise ConfigError(f"duplicate injection id {injection_id!r}")
            seen.add(injection_id)
            if injection_type not in SUPPORTED_INJECTION_TYPES:
                raise ConfigError(f"injection {injection_id} has unsupported type {injection_type!r}")
            if not isinstance(round_number, int) or isinstance(round_number, bool) or round_number < 1:
                raise ConfigError(f"injection {injection_id} has invalid round")
            target_shard_index = row.get("target_shard_index")
            target_physical_index = row.get("target_physical_index")
            for field_name, field_value in (
                ("target_shard_index", target_shard_index),
                ("target_physical_index", target_physical_index),
            ):
                if field_value is not None and (
                    not isinstance(field_value, int) or isinstance(field_value, bool) or field_value < 0
                ):
                    raise ConfigError(f"injection {injection_id} has invalid {field_name}")
            injections.append(
                Injection(
                    injection_id=injection_id,
                    injection_type=injection_type,
                    round=round_number,
                    target_shard_index=target_shard_index,
                    target_physical_index=target_physical_index,
                    value=row.get("value"),
                    raw=row,
                )
            )
        from .io import sha256_bytes

        return cls(injections, sha256_bytes(raw_bytes))

    def for_round(self, round_number: int, injection_type: str | None = None) -> list[Injection]:
        return [
            item
            for item in self.injections
            if item.round == round_number
            and (injection_type is None or item.injection_type == injection_type)
        ]

    def get_one(self, round_number: int, injection_type: str) -> Injection | None:
        matches = self.for_round(round_number, injection_type)
        return matches[0] if matches else None

    def all_ids(self) -> list[str]:
        return [item.injection_id for item in self.injections]
