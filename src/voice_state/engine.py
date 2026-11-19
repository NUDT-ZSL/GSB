"""Recoverable four-stage analysis orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .canonical import canonical_json_bytes, checksum_value, atomic_write_bytes
from .config import AnalysisConfig
from .dataset import Dataset, load_dataset
from .errors import BusinessError, E_INCONSISTENT
from .pipeline import (
    assert_dialog_sets,
    run_aggregation_stage,
    run_framing_stage,
    run_regression_stage,
    run_segmentation_stage,
)
from .regression import MODEL_CHECKSUM
from .snapshot import STAGE_ORDER, SnapshotStore


ENGINE_SCHEMA = "voice-state-engine-v1"
EventCallback = Callable[[str, str, int, int], None]


def analysis_key(dataset: Dataset, config: AnalysisConfig) -> str:
    return checksum_value(
        {
            "engine_schema": ENGINE_SCHEMA,
            "config": config.as_dict(),
            "dataset_fingerprint": dataset.fingerprint,
            "registry_version": dataset.registry_version,
            "registry_checksum": dataset.registry_checksum,
            "model_checksum": MODEL_CHECKSUM,
        }
    )


@dataclass(slots=True)
class ExecutionResult:
    report: dict[str, Any]
    report_bytes: bytes
    status: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    computed_stages: list[str] = field(default_factory=list)
    reused_stages: list[str] = field(default_factory=list)
    resumed_from_stage: str | None = None

    @property
    def report_hash(self) -> str:
        return str(self.report["report_hash"])

    def summary(self) -> dict[str, Any]:
        return {
            "ok": True,
            "report_hash": self.report_hash,
            "report_checksum": checksum_value(self.report),
            "computed_stages": self.computed_stages,
            "reused_stages": self.reused_stages,
            "resumed_from_stage": self.resumed_from_stage,
            "dialog_count": len(self.report["dialogs"]),
            "ranking": self.report["ranking"],
            "status": self.status,
        }


class AnalysisEngine:
    def __init__(
        self,
        dataset_dir: str | Path,
        state_dir: str | Path,
        config: AnalysisConfig | None = None,
    ) -> None:
        self.config = config or AnalysisConfig()
        self.config.validate()
        self.dataset_dir = Path(dataset_dir)
        self.state_dir = Path(state_dir)
        self.dataset = load_dataset(self.dataset_dir)
        self.store = SnapshotStore(self.state_dir)
        self.key = analysis_key(self.dataset, self.config)
        self.store.initialize_or_reconcile(
            analysis_key=self.key,
            registry_version=self.dataset.registry_version,
            registry_checksum=self.dataset.registry_checksum,
        )
        self._events: list[dict[str, Any]] = []
        self._on_event: EventCallback | None = None

    def status(self) -> dict[str, Any]:
        status = self.store.status()
        status["analysis_key"] = self.key
        status["config_checksum"] = self.config.checksum()
        status["dataset_fingerprint"] = self.dataset.fingerprint
        status["registry_version"] = self.dataset.registry_version
        status["registry_checksum"] = self.dataset.registry_checksum
        return status

    def run(self, on_event: EventCallback | None = None) -> ExecutionResult:
        self._on_event = on_event
        before = self.store.status()
        committed_before = [
            stage for stage in STAGE_ORDER if before["stages"].get(stage, {}).get("status") == "committed"
        ]
        self._events = []
        initial_resume = before["resume_from_stage"]
        computed: list[str] = []
        reused: list[str] = []
        payloads: dict[str, dict[str, Any]] = {}
        expected_ids = [item.dialog_id for item in self.dataset.dialogs]

        for stage in STAGE_ORDER:
            if stage in committed_before:
                payload = self.store.load_stage(stage)
                assert_dialog_sets(expected_ids, payload, stage)
                payloads[stage] = payload
                reused.append(stage)
                self._emit("stage_reused", stage, len(expected_ids), len(expected_ids))
                continue

            if stage == "framing":
                payload = run_framing_stage(self.dataset, self.config, self._progress)
            elif stage == "segmentation":
                payload = run_segmentation_stage(
                    self.dataset,
                    self.config,
                    payloads["framing"],
                    self.store.cache_dir,
                    self._progress,
                )
            elif stage == "regression":
                payload = run_regression_stage(
                    self.dataset,
                    self.config,
                    payloads["framing"],
                    payloads["segmentation"],
                    self._progress,
                )
            else:
                payload = run_aggregation_stage(
                    self.dataset,
                    self.config,
                    payloads["regression"],
                )
            assert_dialog_sets(expected_ids, payload, stage)
            self.store.commit_stage(
                stage,
                payload,
                analysis_key=self.key,
                registry_version=self.dataset.registry_version,
                registry_checksum=self.dataset.registry_checksum,
            )
            payloads[stage] = payload
            computed.append(stage)
            self._emit("stage_committed", stage, len(expected_ids), len(expected_ids))

        aggregation = payloads["aggregation"]
        report = aggregation["report"]
        if report.get("report_hash") != aggregation.get("report_hash"):
            raise BusinessError(
                E_INCONSISTENT,
                "Aggregation report hash is inconsistent with the committed payload",
                {"stage": "aggregation"},
            )
        report_bytes = canonical_json_bytes(report)
        status = self.status()
        result = ExecutionResult(
            report=report,
            report_bytes=report_bytes,
            status=status,
            events=list(self._events),
            computed_stages=computed,
            reused_stages=reused,
            resumed_from_stage=initial_resume if initial_resume else None,
        )
        self._emit("run_complete", "aggregation", len(expected_ids), len(expected_ids))
        return result

    def run_stage(self, stage: str, on_event: EventCallback | None = None) -> ExecutionResult:
        if stage not in STAGE_ORDER:
            raise BusinessError(E_INCONSISTENT, "Unknown stage", {"stage": stage})
        target_index = STAGE_ORDER.index(stage)
        current = self.store.status()
        self._on_event = on_event
        self._events = []
        expected_ids = [item.dialog_id for item in self.dataset.dialogs]
        payloads: dict[str, dict[str, Any]] = {}
        computed: list[str] = []
        reused: list[str] = []
        for index, name in enumerate(STAGE_ORDER):
            committed = current["stages"].get(name, {}).get("status") == "committed"
            if committed:
                payload = self.store.load_stage(name)
                assert_dialog_sets(expected_ids, payload, name)
                payloads[name] = payload
                reused.append(name)
            elif index <= target_index:
                if name == "framing":
                    payload = run_framing_stage(self.dataset, self.config, self._progress)
                elif name == "segmentation":
                    payload = run_segmentation_stage(
                        self.dataset, self.config, payloads["framing"], self.store.cache_dir, self._progress
                    )
                elif name == "regression":
                    payload = run_regression_stage(
                        self.dataset, self.config, payloads["framing"], payloads["segmentation"], self._progress
                    )
                else:
                    payload = run_aggregation_stage(self.dataset, self.config, payloads["regression"])
                assert_dialog_sets(expected_ids, payload, name)
                self.store.commit_stage(
                    name,
                    payload,
                    analysis_key=self.key,
                    registry_version=self.dataset.registry_version,
                    registry_checksum=self.dataset.registry_checksum,
                )
                payloads[name] = payload
                computed.append(name)
                self._emit("stage_committed", name, len(expected_ids), len(expected_ids))
                if index == target_index:
                    break
            else:
                break
        if "aggregation" not in payloads:
            status = self.status()
            return ExecutionResult(
                report={},
                report_bytes=b"",
                status=status,
                events=list(self._events),
                computed_stages=computed,
                reused_stages=reused,
                resumed_from_stage=None,
            )
        report = payloads["aggregation"]["report"]
        result = ExecutionResult(
            report=report,
            report_bytes=canonical_json_bytes(report),
            status=self.status(),
            events=list(self._events),
            computed_stages=computed,
            reused_stages=reused,
            resumed_from_stage=None,
        )
        return result

    def export_report(self, path: str | Path) -> Path:
        result = self.run()
        target = Path(path)
        atomic_write_bytes(target, result.report_bytes)
        return target

    def _progress(self, event: str, stage: str, completed: int, total: int) -> None:
        if self.config.batch_size > 0 and event == "dialog_completed":
            if completed % self.config.batch_size != 0 and completed != total:
                return
        self._emit(event, stage, completed, total)

    def _emit(self, event: str, stage: str, completed: int, total: int) -> None:
        record = {
            "event": event,
            "stage": stage,
            "completed": completed,
            "total": total,
        }
        self._events.append(record)
        if self._on_event is not None:
            self._on_event(event, stage, completed, total)

