"""Deterministic fault-injection configuration and kernel stage hooks."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .errors import KernelStageError


@dataclass
class InjectionConfig:
    """Each injection item carries a stable id reported when it fires.

    Supported kinds:
      nonfinite_input   - insert NaN/Inf into an input tensor.
      length_mismatch   - hand the kernel a K/V block with wrong width.
      zero_block_size   - construct a config with block_size == 0.
      zero_budget       - allow a run with recompute_budget == 0.
      snapshot_tamper   - rewrite the snapshot version / payload on disk.
      stage_raise       - raise from score / normalize / weighted stage.
    """

    items: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: str) -> "InjectionConfig":
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return cls(items=list(raw.get("items", [])))

    def enabled(self, kind: str) -> list[dict]:
        return [item for item in self.items if item["kind"] == kind]

    def first(self, kind: str) -> dict | None:
        matches = self.enabled(kind)
        return matches[0] if matches else None


class KernelInjector:
    """Per-run stage hook; records every fired injection id for the report."""

    def __init__(self, config: InjectionConfig | None):
        self.config = config
        self.fired: list[str] = []
        self.fired_by_block: dict[int, str] = {}

    def on_stage(self, stage: str, block_index: int) -> None:
        if self.config is None:
            return
        for item in self.config.enabled("stage_raise"):
            if item.get("stage") != stage:
                continue
            target = item.get("block_index")
            if target is not None and target != block_index:
                continue
            injection_id = item["id"]
            self.fired.append(injection_id)
            self.fired_by_block[block_index] = injection_id
            raise KernelStageError(
                stage,
                f"injection {injection_id} forced failure at stage {stage} "
                f"of block {block_index}",
            )
