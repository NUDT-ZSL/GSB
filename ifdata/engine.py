"""Six-stage orchestration, run fingerprinting and crash-consistent resume."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable

from .config import load_data_inputs, resolve_config
from .errors import (
    BusinessError,
    E_INVALID_INPUT,
    E_SNAPSHOT_CORRUPT,
    ForcedTermination,
)
from .stages import (
    run_dedup,
    run_export,
    run_ingest,
    run_quality,
    run_sampling,
    run_template,
)
from .state import StageSnapshotStore
from .strategy import load_strategy_file, locate_strategy
from .types import STAGE_NAMES
from .utils import atomic_write_json, now_ms, read_json, sha256_file


class PipelineEngine:
    def __init__(
        self,
        *,
        data_dir: str | Path,
        state_dir: str | Path,
        out_dir: str | Path,
        strategy_generation: int = 2,
        strategy_path: str | Path | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.state_dir = Path(state_dir)
        self.out_dir = Path(out_dir)
        self.strategy_generation = strategy_generation
        if strategy_path is None:
            strategy_path = locate_strategy(
                self.data_dir, strategy_generation
            )
        self.strategy_path = Path(strategy_path)
        self.strategy = load_strategy_file(
            self.strategy_path, strategy_generation
        )
        templates, cleaning_rules, quotas = load_data_inputs(self.data_dir)
        self.templates = templates
        self.cleaning_rules = cleaning_rules
        self.quotas = quotas
        self.config = resolve_config(
            self.strategy,
            cleaning_rules,
            quotas,
        )
        self.store = StageSnapshotStore(self.state_dir)
        self.run_manifest_path = self.state_dir / "run.json"
        self._input_paths = {
            "raw_samples": self.data_dir / "raw_samples.jsonl",
            "templates": self.data_dir / "templates.json",
            "cleaning_rules": self.data_dir / "cleaning_rules.json",
            "quota_targets": self.data_dir / "quota_targets.json",
        }
        missing = [
            str(path) for path in self._input_paths.values() if not path.exists()
        ]
        if missing:
            raise BusinessError(
                E_INVALID_INPUT,
                "pipeline input files are missing",
                details={"missing": missing},
            )

    @property
    def raw_path(self) -> Path:
        return self._input_paths["raw_samples"]

    def _fingerprint(self) -> dict[str, Any]:
        return {
            "strategy_generation": self.strategy_generation,
            "strategy_checksum": self.strategy.get("checksum"),
            "strategy_snapshot_version": self.strategy.get("snapshot_version"),
            "inputs": {
                name: sha256_file(path)
                for name, path in sorted(self._input_paths.items())
            },
        }

    def _ensure_run_manifest(self, *, reset: bool) -> dict[str, Any]:
        if reset and self.state_dir.exists():
            shutil.rmtree(self.state_dir)
            self.store = StageSnapshotStore(self.state_dir)
        fingerprint = self._fingerprint()
        if self.run_manifest_path.exists():
            existing = read_json(self.run_manifest_path)
            if existing.get("fingerprint") != fingerprint:
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    (
                        "run fingerprint changed; committed stages cannot be "
                        "mixed with a different strategy or input set"
                    ),
                    details={
                        "existing": existing.get("fingerprint"),
                        "current": fingerprint,
                        "hint": "start a fresh state directory",
                    },
                )
            return existing
        manifest = {
            "schema_version": 1,
            "created_at_ms": now_ms(),
            "fingerprint": fingerprint,
        }
        atomic_write_json(self.run_manifest_path, manifest)
        return manifest

    def _apply_overrides(
        self,
        config: dict[str, Any],
        *,
        requested_workers: int | None = None,
        compare_budget: int | None = None,
        memory_limit_bytes: int | None = None,
    ) -> dict[str, Any]:
        import copy

        updated = copy.deepcopy(config)
        if requested_workers is not None:
            updated["runtime"]["requested_workers"] = int(requested_workers)
        if compare_budget is not None:
            updated["runtime"]["compare_budget"] = int(compare_budget)
        if memory_limit_bytes is not None:
            updated["runtime"]["memory_limit_bytes"] = int(memory_limit_bytes)
        return updated

    def _stage_fn(
        self,
        stage: str,
        *,
        config: dict[str, Any],
        failpoints: dict[str, str] | None,
    ) -> Callable[[dict[str, Any]], dict[str, Any]]:
        if stage == "ingest":
            return lambda previous: run_ingest(self.raw_path, config)
        if stage == "template":
            return lambda previous: run_template(
                previous["samples"],
                self.templates,
                config,
                failpoints=failpoints,
            )
        if stage == "quality":
            return lambda previous: run_quality(
                previous["samples"],
                self.cleaning_rules,
                config,
                failpoints=failpoints,
            )
        if stage == "dedup":
            return lambda previous: run_dedup(
                previous["samples"],
                config,
                failpoints=failpoints,
            )
        if stage == "sampling":
            return lambda previous: run_sampling(
                previous["samples"],
                config,
            )
        if stage == "export":
            return lambda previous: run_export(
                previous,
                self.out_dir,
                strategy_checksum=self.strategy.get("checksum"),
            )
        raise BusinessError(E_INVALID_INPUT, f"unknown stage {stage}")

    def run(
        self,
        *,
        resume: bool = True,
        reset: bool = False,
        requested_workers: int | None = None,
        compare_budget: int | None = None,
        memory_limit_bytes: int | None = None,
        stop_after_stage: str | None = None,
        fail_stage: str | None = None,
        failpoints: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not resume and not reset and self.store.load_snapshots():
            raise BusinessError(
                E_INVALID_INPUT,
                "state already contains committed stages; use reset=True",
            )
        manifest = self._ensure_run_manifest(reset=reset)
        config = self._apply_overrides(
            self.config,
            requested_workers=requested_workers,
            compare_budget=compare_budget,
            memory_limit_bytes=memory_limit_bytes,
        )
        committed = self.store.load_snapshots()
        previous = committed[-1]["payload"] if committed else None
        start_index = (
            int(committed[-1]["stage_index"]) + 1 if committed else 1
        )
        newly_committed: list[dict[str, Any]] = []
        resumed_from = committed[-1]["stage"] if committed else None
        for index in range(start_index, len(STAGE_NAMES) + 1):
            stage = STAGE_NAMES[index - 1]
            self.store.record_event(
                "stage_started",
                stage=stage,
                stage_index=index,
                resumed=bool(committed),
            )
            try:
                result = self._stage_fn(
                    stage,
                    config=config,
                    failpoints=failpoints,
                )(previous or {})
                if fail_stage == stage:
                    raise ForcedTermination(
                        stage,
                        "injected after computation and before stage commit",
                    )
                expected_ids = result["expected_sample_ids"]
                completed_ids = result["completed_sample_ids"]
                snapshot = self.store.commit(
                    stage=stage,
                    payload=result,
                    completed_sample_ids=completed_ids,
                    expected_sample_ids=expected_ids,
                )
            except ForcedTermination:
                self.store.record_event(
                    "stage_terminated_before_commit",
                    stage=stage,
                    stage_index=index,
                    committed_stage=resumed_from,
                )
                raise
            except BusinessError as exc:
                self.store.record_event(
                    "stage_failed",
                    stage=stage,
                    stage_index=index,
                    code=exc.code,
                    message=exc.message,
                    batch_id=exc.batch_id,
                )
                raise
            previous = result
            newly_committed.append(
                {
                    "stage": stage,
                    "stage_index": index,
                    "snapshot_version": snapshot["snapshot_version"],
                    "checksum": snapshot["checksum"],
                    "completed_count": len(snapshot["completed_sample_ids"]),
                }
            )
            if stop_after_stage == stage:
                break
        final_snapshots = self.store.load_snapshots()
        final_stage = final_snapshots[-1]["stage"] if final_snapshots else None
        return {
            "status": (
                "complete"
                if final_stage == STAGE_NAMES[-1]
                else "interrupted"
            ),
            "strategy": {
                "generation": self.strategy_generation,
                "strategy_id": self.strategy.get("strategy_id"),
                "snapshot_version": self.strategy.get("snapshot_version"),
                "checksum": self.strategy.get("checksum"),
                "path": str(self.strategy_path),
            },
            "run_fingerprint": manifest["fingerprint"],
            "resumed_from_stage": resumed_from,
            "started_at_stage": (
                STAGE_NAMES[start_index - 1]
                if start_index <= len(STAGE_NAMES)
                else None
            ),
            "newly_committed": newly_committed,
            "state": self.store.summary(),
            "export": (
                final_snapshots[-1]["payload"].get("manifest")
                if final_stage == "export"
                else None
            ),
        }

    def status(self) -> dict[str, Any]:
        run_manifest = (
            read_json(self.run_manifest_path)
            if self.run_manifest_path.exists()
            else None
        )
        return {
            "strategy": {
                "generation": self.strategy_generation,
                "strategy_id": self.strategy.get("strategy_id"),
                "snapshot_version": self.strategy.get("snapshot_version"),
                "checksum": self.strategy.get("checksum"),
                "path": str(self.strategy_path),
            },
            "run_manifest": run_manifest,
            "state": self.store.summary(),
        }

    def show_stage(self, stage: str) -> dict[str, Any]:
        snapshot = self.store.get_stage(stage)
        if snapshot is None:
            raise BusinessError(
                E_INVALID_INPUT,
                f"stage {stage!r} has no committed snapshot",
            )
        return snapshot

