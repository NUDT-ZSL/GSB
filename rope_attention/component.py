"""Public facade for embedding the component in a local application."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .attention import (
    NumericTolerance,
    blocked_attention,
    compare_blocked_to_full,
    full_attention,
)
from .cache import CacheStore
from .config import parse_config
from .data import AttentionTask, AttentionTensors
from .engine import (
    AttentionEngine,
    EngineResult,
    assert_results_byte_identical,
    estimate_block_memory,
    plan_invalidation,
    retarget_tasks,
)
from .migration import MigrationResult, migrate
from .models import ParsedConfig, ResourceLimits
from .rope import compute_position_angles, rotate_qk, rotation_summary
from .snapshot import ConfigSnapshot, SnapshotStore


class RopeAttentionComponent:
    """One local workspace with cache, active snapshot and compute facade."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        tolerance: NumericTolerance | None = None,
        limits: ResourceLimits | None = None,
        use_cache: bool = True,
        validate_reference: bool = True,
        allow_semantic_alias: bool = True,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.cache = CacheStore(self.workspace / "cache")
        self.snapshots = SnapshotStore(self.workspace)
        self.engine = AttentionEngine(
            self.cache,
            tolerance=tolerance,
            limits=limits,
            snapshot_store=self.snapshots,
            use_cache=use_cache,
            validate_reference=validate_reference,
            allow_semantic_alias=allow_semantic_alias,
        )

    def load_config(self, text: str | bytes) -> ParsedConfig:
        return parse_config(text)

    def migrate(
        self,
        source: ParsedConfig | str | bytes,
        target_version: str,
        *,
        allow_semantic_drift: bool = True,
    ) -> MigrationResult:
        return migrate(
            source,
            target_version,
            allow_semantic_drift=allow_semantic_drift,
        )

    def active_snapshot(self) -> ConfigSnapshot | None:
        return self.snapshots.active()

    def commit_config(
        self,
        text: str | bytes,
        *,
        source_name: str,
        version: int | None = None,
    ) -> tuple[ConfigSnapshot, bool]:
        return self.snapshots.replace_from_text(
            text,
            source_name=source_name,
            version=version,
        )

    def compute(
        self,
        tasks: Iterable[AttentionTask],
        tensors_by_case: dict[str, AttentionTensors],
        *,
        snapshot: ConfigSnapshot | None = None,
        parallel_nodes: int = 1,
    ) -> EngineResult:
        active = snapshot if snapshot is not None else self.snapshots.active()
        if active is None:
            raise ValueError("no active config snapshot")
        return self.engine.compute_all(
            tasks,
            tensors_by_case,
            active,
            parallel_nodes=parallel_nodes,
        )

    def rotation(
        self,
        task: AttentionTask,
        tensors: AttentionTensors,
        config: ParsedConfig,
    ) -> dict[str, Any]:
        angles = compute_position_angles(task, config)
        q_rot, k_rot = rotate_qk(tensors.q, tensors.k, angles)
        return {
            "angles": angles,
            "k_rot": k_rot,
            "q_rot": q_rot,
            "summary": rotation_summary(angles),
        }

    def attention(
        self,
        task: AttentionTask,
        tensors: AttentionTensors,
        config: ParsedConfig,
    ) -> dict[str, Any]:
        rotation = self.rotation(task, tensors, config)
        blocked = blocked_attention(
            rotation["q_rot"],
            rotation["k_rot"],
            tensors.v,
            block_size=config.block_size,
            causal=task.causal,
            head_dim=task.head_dim,
        )
        full = full_attention(
            rotation["q_rot"],
            rotation["k_rot"],
            tensors.v,
            causal=task.causal,
            head_dim=task.head_dim,
        )
        comparison = compare_blocked_to_full(
            blocked,
            full,
            tolerance=self.engine.tolerance,
        )
        return {
            "blocked": blocked,
            "comparison": comparison,
            "full": full,
            "rotation": rotation,
        }

    def invalidation_plan(
        self,
        tasks: Iterable[AttentionTask],
        old_snapshot: ConfigSnapshot,
        new_snapshot: ConfigSnapshot,
    ) -> dict[str, Any]:
        return plan_invalidation(tasks, old_snapshot, new_snapshot)

    def assert_byte_identical(
        self,
        incremental: EngineResult,
        full: EngineResult,
    ) -> dict[str, Any]:
        return assert_results_byte_identical(incremental, full)

    def resource_estimate(
        self,
        tasks: Iterable[AttentionTask],
        block_size: int,
    ) -> dict[str, int]:
        values = {
            task.case_id: estimate_block_memory(task, block_size) for task in tasks
        }
        return {
            "max_bytes": max(values.values()) if values else 0,
            "per_case": values,
        }

    def cache_diagnostics(self) -> dict[str, Any]:
        return self.cache.diagnostics()

    def verify_cache(self) -> dict[str, Any]:
        return self.cache.verify_all_entries()

    def retarget(
        self,
        tasks: Iterable[AttentionTask],
        config: ParsedConfig,
    ) -> list[AttentionTask]:
        return retarget_tasks(tasks, config)

