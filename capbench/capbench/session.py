"""Load-test session orchestration: run, rerun (stale replacement), recovery."""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .clock import MonotonicClock
from .driver import StageDriver
from .metrics import aggregate, judge
from .models import (Scene, StageResult, VERDICT_UNKNOWN)
from .snapshot import Snapshot, SnapshotStore
from .versioning import VersionInfo, diff_scenes, fingerprint


@dataclass
class ReplacementReport:
    replaced_stage_indexes: list[int]
    old_version: int | None
    new_version: int
    fingerprint_old: str | None
    fingerprint_new: str
    diffs: list[str]
    fully_replaced: bool


@dataclass
class RunReport:
    snapshot_name: str
    params_version: int
    completed: list[StageResult] = field(default_factory=list)
    unknown_stage_indexes: list[int] = field(default_factory=list)
    interrupted: bool = False
    replacement: ReplacementReport | None = None


class SessionRunner:
    def __init__(self, store: SnapshotStore, version_store: VersionStore,
                 server_config: dict[str, Any],
                 clock_factory: Callable[[], MonotonicClock] = MonotonicClock,
                 sample_interval_ms_override: int | None = None) -> None:
        self.store = store
        self.version_store = version_store
        self.server_config = dict(server_config)
        self.clock_factory = clock_factory
        self.sample_interval_ms_override = sample_interval_ms_override

    # ------------------------------------------------------------------ run/rerun
    def run(self, snapshot_name: str, scene: Scene,
            stop_event: threading.Event | None = None) -> RunReport:
        if stop_event is None:
            stop_event = threading.Event()
        fp = fingerprint(scene)
        version_info: VersionInfo = self.version_store.resolve(fp)
        snapshot = self.store.new(
            snapshot_name, copy.deepcopy(scene.raw),
            version_info.version, fp, dict(self.server_config))

        replacement: ReplacementReport | None = None
        # Rerun case: a prior snapshot of the same name exists. Its observations
        # are stale whenever the fingerprint/version differs and must be wholly
        # replaced - never merged stage-by-stage.
        try:
            prior = self.store.load(snapshot_name)
        except FileNotFoundError:
            prior = None
        if prior is not None:
            diffs = diff_scenes(prior.scene, scene)
            stale = prior.params_fingerprint != fp
            replaced_indexes = prior.result_indexes() if stale else []
            replacement = ReplacementReport(
                replaced_stage_indexes=replaced_indexes,
                old_version=prior.params_version if stale else None,
                new_version=version_info.version,
                fingerprint_old=prior.params_fingerprint if stale else None,
                fingerprint_new=fp,
                diffs=diffs if stale else [],
                fully_replaced=stale,
            )

        report = RunReport(snapshot_name=snapshot_name,
                           params_version=version_info.version,
                           replacement=replacement)
        self.store.save(snapshot)
        interval = (self.sample_interval_ms_override
                    or scene.sample_interval_ms)

        for spec in scene.stages:
            if stop_event is not None and stop_event.is_set():
                self.store.mark_unknown(
                    snapshot, spec.index,
                    "forced termination before stage start; rerun required")
                report.unknown_stage_indexes.append(spec.index)
                report.interrupted = True
                continue
            clock = self.clock_factory()
            driver = StageDriver(
                scene.target.host, scene.target.port, spec,
                sample_interval_ms=interval, clock=clock,
                stop_event=stop_event)
            observation = driver.run()
            if observation is None or driver.interrupted:
                self.store.mark_unknown(
                    snapshot, spec.index,
                    "forced termination mid-stage; no exit report produced; "
                    "rerun required")
                report.unknown_stage_indexes.append(spec.index)
                report.interrupted = True
                continue
            metrics = aggregate(observation)
            verdict = judge(metrics, scene.slo)
            result = StageResult(
                stage_index=spec.index,
                spec={
                    "index": spec.index,
                    "duration_s": spec.duration_s,
                    "concurrency": spec.concurrency,
                    "throughput_limit_rps": spec.throughput_limit_rps,
                },
                target={"host": scene.target.host, "port": scene.target.port},
                params_version=version_info.version,
                metrics=metrics,
                verdict=verdict.verdict,
                saturation_reason=verdict.saturation_reason,
                triggered_metrics=verdict.triggered_metrics,
                observation=observation,
            )
            self.store.append_result(snapshot, result)
            report.completed.append(result)

        if not report.interrupted:
            snapshot.status = "completed"
            self.store.save(snapshot)
        return report

    # ------------------------------------------------------------------ recovery
    def recover(self, snapshot_name: str) -> Snapshot:
        """Reload a snapshot after forced termination.

        Only persisted finished stages are trusted. Stages marked unknown have
        no report and are explicitly listed for re-execution.
        """
        return self.store.load(snapshot_name)

    @staticmethod
    def recovery_plan(snapshot: Snapshot) -> dict[str, Any]:
        finished = snapshot.result_indexes()
        unknown = snapshot.unknown_indexes()
        all_indexes = [s["index"] for s in snapshot.scene.get("stages", [])]
        pending = [i for i in all_indexes if i not in finished]
        return {
            "snapshot": snapshot.name,
            "status": snapshot.status,
            "params_version": snapshot.params_version,
            "finished_stage_indexes": finished,
            "unknown_stage_indexes": unknown,
            "must_rerun_stage_indexes": pending,
            "note": ("unknown stages have no exit report by construction and "
                     "must be re-executed; no report is synthesized"),
        }
