"""Deterministic fault-injection engine.

Injections are pure data from ``injections.json``.  Each call site asks the
engine whether a fault is armed for its ``(stage, target)``; the engine tracks
per-injection attempt counts so ``fire_after`` makes a fault disappear after a
resume replay, proving that recovery only replays missing work.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .errors import (
    E_BATCH_DIGEST_CONFLICT,
    E_CACHE_ALGEBRA_ROLLBACK,
    E_CACHE_WRITE_FAILED,
    E_CLOCK_ROLLBACK,
    E_GEN_DIGEST_MISMATCH,
    E_PREFETCH_FAILED,
    E_PREFETCH_TIMEOUT,
    fail,
)
from .model import Injection

STAGE_KIND_CODE = {
    ("generate", "gen_digest_mismatch"): E_GEN_DIGEST_MISMATCH,
    ("cache", "cache_algebra_rollback"): E_CACHE_ALGEBRA_ROLLBACK,
    ("cache", "cache_write_failure"): E_CACHE_WRITE_FAILED,
    ("prefetch", "prefetch_failure"): E_PREFETCH_FAILED,
    ("prefetch", "prefetch_timeout"): E_PREFETCH_TIMEOUT,
    ("prefetch", "clock_rollback"): E_CLOCK_ROLLBACK,
    ("assemble", "batch_digest_conflict"): E_BATCH_DIGEST_CONFLICT,
}

# crash injections raise this internal sentinel instead of a business error so
# the orchestrator can distinguish forced termination from ordinary faults.
class ForcedCrash(RuntimeError):
    def __init__(self, stage: str, target: str):
        super().__init__(f"forced crash at stage={stage} target={target}")
        self.stage = stage
        self.target = target


@dataclass
class InjectionEngine:
    injections: Tuple[Injection, ...]
    marker_path: str = ""
    attempts: Dict[int, int] = field(default_factory=dict)
    fired: List[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Crash injections simulate a process being killed.  Persist a marker
        # so that after a real resume the same one-shot crash does not re-fire,
        # while ordinary (non-crash) faults remain replayable.
        self._crashed: set = set()
        if self.marker_path and os.path.exists(self.marker_path):
            with open(self.marker_path, "r", encoding="utf-8") as handle:
                self._crashed = set(json.load(handle))

    def _matches(self, injection: Injection, stage: str, target: str) -> bool:
        if injection.stage != stage:
            return False
        if injection.target == "*":
            return True
        return injection.target == target

    def armed(self, stage: str, target: str) -> Optional[Injection]:
        """Return the first armed injection for this call site, if any."""
        for index, injection in enumerate(self.injections):
            if not self._matches(injection, stage, target):
                continue
            attempt = self.attempts.get(index, 0)
            self.attempts[index] = attempt + 1
            if attempt < injection.fire_after:
                continue
            self.fired.append(
                {
                    "stage": stage,
                    "target": target,
                    "kind": injection.kind,
                    "attempt": attempt,
                }
            )
            return injection
        return None

    def check(self, stage: str, target: str) -> None:
        """Raise the mapped business error / forced crash if a fault is armed."""
        injection = self.armed(stage, target)
        if injection is None:
            return
        if injection.kind == "crash":
            marker = f"{stage}:{target}"
            if marker in self._crashed:
                return
            self._crashed.add(marker)
            if self.marker_path:
                with open(self.marker_path, "w", encoding="utf-8") as handle:
                    json.dump(sorted(self._crashed), handle)
            raise ForcedCrash(stage, target)
        code = STAGE_KIND_CODE.get((stage, injection.kind))
        if code is None:
            code = E_PREFETCH_FAILED
        raise fail(
            code,
            f"injected {injection.kind} at {stage}",
            stage=stage,
            target=target,
            kind=injection.kind,
            options=injection.options,
        )
