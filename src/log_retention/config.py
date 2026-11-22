from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ArchiveError, fail


def _require_int(raw: dict[str, Any], key: str, *, minimum: int | None = None) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        fail("E_CONFIG_INVALID", f"config field {key!r} must be an integer", field=key)
    if minimum is not None and value < minimum:
        fail(
            "E_CONFIG_INVALID",
            f"config field {key!r} must be >= {minimum}",
            field=key,
            value=value,
        )
    return value


@dataclass(frozen=True)
class Config:
    path: Path
    root: Path
    primary_stream: Path
    replay_stream: Path
    v1_policy: Path
    v2_meta_db: Path
    v2_shards: tuple[Path, ...]
    ledger_db: Path
    injected_clock_ms: int
    archive_target_watermark_ms: int
    fixed_scale_units_per_ms: int
    clock_tolerance_ms: int
    replay_rates_bytes_per_sec: dict[str, int]
    source_priority: dict[str, int]
    load_version: int

    @classmethod
    def load(cls, path: str | Path) -> Config:
        config_path = Path(path).resolve()
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ArchiveError(
                "E_CONFIG_READ", "cannot read config", {"path": str(config_path)}
            ) from exc
        except json.JSONDecodeError as exc:
            raise ArchiveError(
                "E_CONFIG_JSON",
                "config is not valid JSON",
                {"path": str(config_path), "line": exc.lineno, "column": exc.colno},
            ) from exc
        if not isinstance(raw, dict):
            fail("E_CONFIG_INVALID", "config root must be a JSON object")
        root = config_path.parent

        def path_field(key: str) -> Path:
            value = raw.get(key)
            if not isinstance(value, str) or not value:
                fail(
                    "E_CONFIG_INVALID",
                    f"config field {key!r} must be a non-empty path",
                    field=key,
                )
            candidate = Path(value)
            return (
                candidate if candidate.is_absolute() else (root / candidate).resolve()
            )

        shards_raw = raw.get("v2_shards")
        if not isinstance(shards_raw, list) or not shards_raw:
            fail(
                "E_CONFIG_INVALID", "config field 'v2_shards' must be a non-empty list"
            )
        shards: list[Path] = []
        for item in shards_raw:
            if not isinstance(item, str) or not item:
                fail(
                    "E_CONFIG_INVALID", "each v2 shard path must be a non-empty string"
                )
            p = Path(item)
            shards.append(p if p.is_absolute() else (root / p).resolve())

        rates_raw = raw.get("replay_rates_bytes_per_sec", {})
        if not isinstance(rates_raw, dict):
            fail("E_CONFIG_INVALID", "replay_rates_bytes_per_sec must be an object")
        rates: dict[str, int] = {}
        for instance_id, rate in rates_raw.items():
            if not isinstance(instance_id, str) or not instance_id:
                fail(
                    "E_CONFIG_INVALID",
                    "replay rate instance id must be a non-empty string",
                )
            if not isinstance(rate, int) or isinstance(rate, bool) or rate <= 0:
                fail(
                    "E_CONFIG_INVALID",
                    "replay rate must be a positive integer",
                    instance_id=instance_id,
                )
            rates[instance_id] = rate

        priority_raw = raw.get("source_priority", {"v1": 10, "v2": 20})
        if not isinstance(priority_raw, dict):
            fail("E_CONFIG_INVALID", "source_priority must be an object")
        priority: dict[str, int] = {}
        for source, value in priority_raw.items():
            if source not in {"v1", "v2"}:
                fail(
                    "E_CONFIG_INVALID",
                    "source_priority keys must be v1 or v2",
                    source=source,
                )
            if not isinstance(value, int) or isinstance(value, bool):
                fail(
                    "E_CONFIG_INVALID",
                    "source_priority values must be integers",
                    source=source,
                )
            priority[source] = value
        priority.setdefault("v1", 10)
        priority.setdefault("v2", 20)

        return cls(
            path=config_path,
            root=root,
            primary_stream=path_field("primary_stream"),
            replay_stream=path_field("replay_stream"),
            v1_policy=path_field("v1_policy"),
            v2_meta_db=path_field("v2_meta_db"),
            v2_shards=tuple(sorted(shards, key=lambda p: str(p))),
            ledger_db=path_field("ledger_db"),
            injected_clock_ms=_require_int(raw, "injected_clock_ms", minimum=0),
            archive_target_watermark_ms=_require_int(
                raw, "archive_target_watermark_ms", minimum=0
            ),
            fixed_scale_units_per_ms=_require_int(
                raw, "fixed_scale_units_per_ms", minimum=1
            ),
            clock_tolerance_ms=_require_int(raw, "clock_tolerance_ms", minimum=0),
            replay_rates_bytes_per_sec=rates,
            source_priority=priority,
            load_version=_require_int(raw, "load_version", minimum=1),
        )

    def with_overrides(
        self,
        *,
        ledger_db: str | Path | None = None,
        clock_ms: int | None = None,
        archive_target_ms: int | None = None,
        load_version: int | None = None,
    ) -> Config:
        updates: dict[str, Any] = {}
        if ledger_db is not None:
            updates["ledger_db"] = Path(ledger_db).resolve()
        if clock_ms is not None:
            if clock_ms < 0:
                fail(
                    "E_CLOCK_INVALID",
                    "logical clock must be non-negative",
                    clock_ms=clock_ms,
                )
            updates["injected_clock_ms"] = clock_ms
        if archive_target_ms is not None:
            if archive_target_ms < 0:
                fail("E_WATERMARK_INVALID", "archive target must be non-negative")
            updates["archive_target_watermark_ms"] = archive_target_ms
        if load_version is not None:
            if load_version < 1:
                fail("E_LOAD_VERSION_INVALID", "load version must be >= 1")
            updates["load_version"] = load_version
        if not updates:
            return self
        from dataclasses import replace

        return replace(self, **updates)
