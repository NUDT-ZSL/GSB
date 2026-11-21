"""End-to-end orchestration: recovery, scheduling, commits and run provenance."""

from __future__ import annotations

import json
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .config import STAGES, BudgetConfig, PipelineConfig, SchedulingDecision
from .errors import PipeError
from .manifest import PairRecord, canonical_order, validate_manifest
from .resources import current_process_metrics
from .scheduler import ResourceObservation, arbitrate, chunk_pairs
from .stages import FUSION_STAGE, SCORE_STAGE
from .store import CommittedSnapshot, SnapshotStore
from .workers import WORKER_REGISTRY


class PipelineExecutionError(PipeError):
    code = "E_PAIR_FAILED"

    def __init__(self, failures: List[dict]) -> None:
        super().__init__(
            f"{len(failures)} pair(s) failed; first: {failures[0]['code']} "
            f"on {failures[0]['pair_id']} ({failures[0]['stage']})"
        )
        self.failures = failures

    def to_dict(self) -> dict:
        out = super().to_dict()
        out["failures"] = self.failures
        return out


@dataclass
class RunResult:
    report: dict
    snapshots: Dict[str, CommittedSnapshot]
    scores: Dict[str, float] = field(default_factory=dict)


class Pipeline:
    """Deterministic four-stage pipeline with snapshot-based recovery."""

    def __init__(
        self,
        work_dir: Path,
        config: Optional[PipelineConfig] = None,
        budget: Optional[BudgetConfig] = None,
        *,
        requested_workers: int = 2,
        requested_batch: int = 4,
        force_rebuild: bool = False,
    ) -> None:
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.config = config or PipelineConfig()
        self.budget = budget or BudgetConfig()
        self.budget.validate()
        self.requested_workers = requested_workers
        self.requested_batch = requested_batch
        self.force_rebuild = force_rebuild
        self.store = SnapshotStore(self.work_dir, self.config)

    def run(self, records: Sequence[PairRecord]) -> RunResult:
        validated = validate_manifest(records, self.config)
        ordered = canonical_order(validated)
        pair_ids = [record.pair_id for record in ordered]
        pair_id_set = set(pair_ids)
        record_dicts = tuple(record.to_dict() for record in ordered)
        images_dir = str(self.work_dir / "images")

        report: dict = {
            "config": json.loads(self.config.canonical_json()),
            "pair_ids_canonical": pair_ids,
            "stages": {},
            "provenance": {},
        }
        snapshots: Dict[str, CommittedSnapshot] = {}
        committed_dirs: Dict[str, str] = {}

        ctx = mp.get_context("spawn")
        for stage in STAGES:
            stage_report, snapshot = self._run_stage(
                stage=stage,
                pair_ids=pair_ids,
                pair_id_set=pair_id_set,
                record_dicts=record_dicts,
                images_dir=images_dir,
                committed_dirs=committed_dirs,
                ctx=ctx,
            )
            report["stages"][stage] = stage_report
            snapshots[stage] = snapshot
            committed_dirs[stage] = str(snapshot.path)

        self._write_provenance(report, snapshots, pair_id_set)
        scores = self._write_scores(snapshots[SCORE_STAGE])
        self._write_report(report)
        return RunResult(report=report, snapshots=snapshots, scores=scores)

    # ----------------------------------------------------------------- stages
    def _run_stage(
        self,
        *,
        stage: str,
        pair_ids: List[str],
        pair_id_set: set,
        record_dicts: Tuple[dict, ...],
        images_dir: str,
        committed_dirs: Dict[str, str],
        ctx,
    ) -> Tuple[dict, CommittedSnapshot]:
        committed = self.store.load_committed(stage, force_rebuild=self.force_rebuild)
        reusable = committed is not None and set(committed.pair_ids) == pair_id_set

        if reusable:
            # Recovery rule: committed pairs are never recomputed or double counted.
            assert committed is not None
            decision = self._decision(stage, len(pair_ids))
            stage_report = {
                "status": "reused",
                "version": committed.version,
                "embeddings_checksum": committed.embeddings_checksum,
                "scheduling": decision.to_report(),
                "resource_observation": current_process_metrics(),
            }
            return stage_report, committed

        if committed is not None:
            # Pair-set/version mismatch: full replacement, never incremental patching.
            reason = (
                "pair set differs from current manifest"
                if set(committed.pair_ids) != pair_id_set
                else "snapshot version superseded"
            )
            replacement_reason = reason
        else:
            replacement_reason = "no committed snapshot"

        decision = self._decision(stage, len(pair_ids))
        next_version = self.store.next_version(stage)
        staging = self.store.begin_stage(stage, next_version)
        chunks = chunk_pairs(pair_ids, decision.chosen_batch)

        worker = WORKER_REGISTRY[stage]
        kwargs = dict(
            records=record_dicts,
            images_dir=images_dir,
            config_kwargs=dict(self.config.__dict__),
        )
        if stage in (FUSION_STAGE, SCORE_STAGE):
            kwargs["input_dirs"] = dict(committed_dirs)
        task = partial(worker, **kwargs)

        outputs: Dict[str, np.ndarray] = {}
        failures: List[dict] = []
        # executor.map preserves chunk submission order => deterministic merge.
        with ProcessPoolExecutor(
            max_workers=decision.chosen_workers, mp_context=ctx
        ) as executor:
            for chunk_result in executor.map(task, chunks):
                for pair_id, array in chunk_result["outputs"].items():
                    outputs[pair_id] = np.asarray(array, dtype=np.float32)
                failures.extend(chunk_result["failures"])

        if failures:
            # No commit on failure; staging is discarded on the next attempt.
            raise PipelineExecutionError(failures)

        produced_ids = sorted(outputs)
        if produced_ids != sorted(pair_ids):
            from .errors import InconsistentError

            raise InconsistentError(
                f"stage {stage!r} produced {len(produced_ids)} of {len(pair_ids)} pairs",
                stage=stage,
                version=next_version,
                category="stage_pair_set",
            )

        for pair_id in pair_ids:  # canonical write order
            self.store.write_embedding(staging, pair_id, outputs[pair_id])

        snapshot = self.store.commit_stage(
            stage,
            staging,
            pair_ids,
            next_version,
            extra={
                "replacement_reason": replacement_reason,
                "degraded": decision.degraded,
                "degradation_sources": list(decision.degradation_sources),
            },
        )
        stage_report = {
            "status": "recomputed",
            "version": snapshot.version,
            "embeddings_checksum": snapshot.embeddings_checksum,
            "replacement_reason": replacement_reason,
            "scheduling": decision.to_report(),
            "resource_observation": current_process_metrics(),
        }
        return stage_report, snapshot

    def _decision(self, stage: str, pair_count: int) -> SchedulingDecision:
        return arbitrate(
            ResourceObservation(
                stage=stage,
                pair_count=pair_count,
                requested_workers=self.requested_workers,
                requested_batch=self.requested_batch,
            ),
            self.budget,
        )

    # --------------------------------------------------------------- outputs
    def _write_provenance(
        self, report: dict, snapshots: Dict[str, CommittedSnapshot], pair_id_set: set
    ) -> None:
        for stage, snapshot in snapshots.items():
            status = report["stages"][stage]["status"]
            if status == "reused":
                reused, recomputed = sorted(snapshot.pair_ids), []
            else:
                reused, recomputed = [], sorted(snapshot.pair_ids)
            union = set(reused) | set(recomputed)
            assert not (set(reused) & set(recomputed))
            assert union == pair_id_set
            report["provenance"][stage] = {
                "reused_pair_ids": reused,
                "recomputed_pair_ids": recomputed,
                "disjoint": True,
                "union_covers_all": union == pair_id_set,
                "version": snapshot.version,
                "embeddings_checksum": snapshot.embeddings_checksum,
            }

    def _write_scores(self, score_snapshot: CommittedSnapshot) -> Dict[str, float]:
        scores = {}
        for pair_id in score_snapshot.pair_ids:
            value = float(score_snapshot.load(pair_id).reshape(()).astype(np.float32))
            scores[pair_id] = value
        payload = {
            pair_id: {"float32_hex": np.float32(value).tobytes().hex(), "value": value}
            for pair_id, value in sorted(scores.items())
        }
        (self.work_dir / "results.json").write_text(
            json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8"
        )
        return scores

    def _write_report(self, report: dict) -> None:
        (self.work_dir / "run_report.json").write_text(
            json.dumps(report, sort_keys=True, indent=2), encoding="utf-8"
        )

