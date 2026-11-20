"""Explicit, deterministic fault-injection hooks.

Faults are configured per ``scene_id`` and stage in a JSON file rather than
environment randomness, so injected failures are reproducible and auditable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .errors import (
    FaceBudgetError,
    MemoryBudgetError,
    SnapshotCorruptError,
    StageBusyError,
    TextureConsistencyError,
    WorkerFault,
)


@dataclass(frozen=True)
class FaultSpec:
    scene_id: str
    stage: str
    code: str
    message: str


class FaultPlan:
    def __init__(self, specs: Optional[Dict[str, Dict[str, FaultSpec]]] = None):
        self._specs = specs or {}

    @classmethod
    def load(cls, path: Optional[str]) -> "FaultPlan":
        if not path:
            return cls()
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        specs: Dict[str, Dict[str, FaultSpec]] = {}
        for scene_id, stages in data.items():
            specs[scene_id] = {
                stage: FaultSpec(
                    scene_id=scene_id,
                    stage=stage,
                    code=item["code"],
                    message=item.get("message", "injected fault"),
                )
                for stage, item in stages.items()
            }
        return cls(specs)

    def trigger(self, scene_id: str, stage: str) -> None:
        spec = self._specs.get(scene_id, {}).get(stage)
        if spec is None:
            return
        kwargs: Dict[str, Any] = {
            "scene_id": scene_id,
            "stage": stage,
            "details": {"injected": True, "code": spec.code},
        }
        if spec.code == "E_FACE_BUDGET":
            raise FaceBudgetError(spec.message, **kwargs)
        if spec.code == "E_TEX_CONSISTENCY":
            raise TextureConsistencyError(spec.message, **kwargs)
        if spec.code == "E_MEMORY_BUDGET":
            raise MemoryBudgetError(spec.message, **kwargs)
        if spec.code == "E_SNAPSHOT_CORRUPT":
            raise SnapshotCorruptError(spec.message, **kwargs)
        if spec.code == "E_STAGE_BUSY":
            raise StageBusyError(spec.message, **kwargs)
        raise WorkerFault(spec.message, **kwargs)
