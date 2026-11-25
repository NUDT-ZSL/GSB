"""One-shot crash injection used to validate SIGKILL recovery paths."""

import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .errors import PlanValidationError
from .jsonio import read_json


def _crash_error(message: str, **details: Any) -> PlanValidationError:
    error = PlanValidationError(message, **details)
    error.code = "E_CRASH_MANIFEST_INVALID"
    return error


@dataclass(frozen=True)
class CrashInjection:
    stage: str
    after_committed_shards: Optional[int] = None


class CrashInjector:
    def __init__(self, injection: Optional[CrashInjection] = None) -> None:
        self.injection = injection
        self.triggered = False

    @classmethod
    def load(
        cls,
        crash_manifest: Optional[Union[str, Path]] = None,
        stage_override: Optional[str] = None,
        after_override: Optional[int] = None,
    ) -> "CrashInjector":
        injection = None
        if crash_manifest:
            raw = read_json(crash_manifest)
            injection = _parse_injection(raw)
        if stage_override:
            normalized = _normalize_stage(stage_override)
            injection = CrashInjection(
                stage=normalized,
                after_committed_shards=after_override,
            )
        elif after_override is not None:
            if injection is None:
                injection = CrashInjection(
                    stage="write", after_committed_shards=after_override
                )
            elif injection.stage != "write":
                raise _crash_error(
                    "--crash-after can only be used with a write-stage injection"
                )
            else:
                injection = CrashInjection(
                    stage="write", after_committed_shards=after_override
                )
        return cls(injection)

    def trigger(self, stage: str, committed_shards: int = 0) -> None:
        if self.triggered or self.injection is None:
            return
        if self.injection.stage != stage:
            return
        if stage == "write":
            threshold = self.injection.after_committed_shards
            if threshold is None or committed_shards < threshold:
                return
        self.triggered = True
        os.kill(os.getpid(), signal.SIGKILL)

    def describes(self, stage: str) -> bool:
        return bool(self.injection and self.injection.stage == stage)

    def threshold(self) -> Optional[int]:
        if not self.injection or self.injection.stage != "write":
            return None
        return self.injection.after_committed_shards


def _normalize_stage(value: Any) -> str:
    if not isinstance(value, str):
        raise _crash_error("crash stage must be a string", value=value)
    normalized = value.strip().lower().replace("-", "_")
    aliases: Dict[str, str] = {
        "write": "write",
        "after_write": "write",
        "during_write": "write",
        "shard_write": "write",
        "writes": "write",
        "persist": "write",
        "commit": "commit",
        "finalize": "commit",
        "ledger_commit": "commit",
        "checkpoint_commit": "commit",
        "manifest": "commit",
        "manifest_commit": "commit",
        "before_commit": "commit",
    }
    if normalized not in aliases:
        raise _crash_error("unsupported crash stage", stage=value)
    return aliases[normalized]


def _parse_injection(raw: Any) -> Optional[CrashInjection]:
    if isinstance(raw, list):
        candidates = raw
    elif isinstance(raw, dict) and isinstance(raw.get("injections"), list):
        schema = raw.get("schema")
        if schema is not None and schema != "ckpt-crash/v1":
            raise _crash_error("unsupported crash manifest schema", schema=schema)
        candidates = raw["injections"]
    elif isinstance(raw, dict):
        candidates = [raw]
    else:
        raise _crash_error("crash manifest must be an object or a list")
    if not candidates:
        return None
    entry = candidates[0]
    if not isinstance(entry, dict):
        raise _crash_error("crash injection must be an object")
    stage = _normalize_stage(
        entry.get(
            "stage",
            entry.get("crash_stage", entry.get("mode", entry.get("phase"))),
        )
    )
    after = entry.get(
        "after_committed_shards",
        entry.get(
            "after_shards",
            entry.get(
                "after",
                entry.get("kill_after", entry.get("after_write", entry.get("after_committed"))),
            ),
        ),
    )
    if stage == "write":
        if after is None:
            after = 0
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise _crash_error(
                "crash write threshold must be a non-negative integer",
                after_committed_shards=after,
            )
        return CrashInjection(stage=stage, after_committed_shards=after)
    return CrashInjection(stage=stage)
