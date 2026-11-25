"""End-to-end deterministic detection, snapshot recovery and recalibration."""

from __future__ import annotations

import signal
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .autoencoder import AEConfig, DeterministicAutoencoder
from .calibration import CalibrationRequest, CalibrationService
from .codec import to_native
from .execution_control import BudgetConfig
from .features import ScoringConfig, extract_features
from .io_utils import append_jsonl, atomic_write_bytes, payload_checksum, read_jsonl
from .preprocessing import NormalizationStats, PreprocessConfig, WindowPreprocessor
from .snapshots import SnapshotStore
from .storage import MetadataStore
from .thresholds import ThresholdModel, adjudicate


class DetectionPipeline:
    def __init__(
        self,
        state_dir: str | Path,
        *,
        seed: int = 406,
        batch_size: int = 8,
        input_shape: tuple[int, int] = (32, 4),
        strategy: str = "strict",
        preprocess_config: PreprocessConfig | None = None,
        ae_config: AEConfig | None = None,
        scoring_config: ScoringConfig | None = None,
        budget_config: BudgetConfig | None = None,
    ):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.seed = int(seed)
        self.batch_size = int(batch_size)
        self.input_shape = tuple(input_shape)
        self.input_dim = int(np.prod(input_shape))
        self.strategy = strategy
        self.preprocess_config = preprocess_config or PreprocessConfig(seed=seed)
        self.ae_config = ae_config or AEConfig(seed=seed)
        self.scoring_config = scoring_config or ScoringConfig()

        self.snapshots = SnapshotStore(self.state_dir)
        self.metadata = MetadataStore(self.state_dir)
        self.calibration = CalibrationService(
            self.state_dir,
            metadata=self.metadata,
            snapshots=self.snapshots,
            budget_config=budget_config,
        )

        self.stats_path = self.state_dir / "normalization_stats.npz"
        self.ae_path = self.state_dir / "ae_weights.npz"
        self.diagnostics_jsonl = self.state_dir / "diagnostics.jsonl"
        self.diagnostics_dir = self.state_dir / "diagnostics_batches"
        self.diagnostics_dir.mkdir(exist_ok=True)

        self.stats = self._load_stats()
        self.preprocessor = WindowPreprocessor(
            self.preprocess_config,
            stats=self.stats,
            input_shape=self.input_shape,
        )
        self.ae = self._load_ae()
        self.model = self._load_threshold_model()
        self.plan: dict[str, Any] | None = self.snapshots.load_payload("plan")
        self.result: dict[str, Any] = self.snapshots.load_payload("result", default=self._empty_result())
        self.reconstructed_this_run: list[str] = []
        self.reused_this_run: list[str] = []

    def set_stats(self, stats: NormalizationStats) -> None:
        self.stats = stats
        np.savez_compressed(self.stats_path, mean=stats.mean, scale=stats.scale)
        self.preprocessor = WindowPreprocessor(
            self.preprocess_config,
            stats=stats,
            input_shape=self.input_shape,
        )

    def set_ae(self, ae: DeterministicAutoencoder, persist: bool = True) -> None:
        self.ae = ae
        if persist:
            ae.save_npz(self.ae_path)

    def set_threshold_model(self, model: ThresholdModel, persist: bool = True) -> dict:
        self.model = model
        if persist:
            return self.snapshots.commit("threshold", model.to_dict())
        return {}

    def initialize_plan(self, window_ids: Sequence[str]) -> dict[str, Any]:
        ids = [str(item) for item in window_ids]
        if self.plan is not None:
            existing = list(self.plan.get("window_ids", []))
            if existing != ids:
                raise ValueError("existing plan window ids do not match requested ids")
            return self.plan
        batches = []
        for index in range(0, len(ids), self.batch_size):
            batch_index = index // self.batch_size
            batches.append(
                {
                    "batch_index": batch_index,
                    "window_ids": ids[index : index + self.batch_size],
                    "status": "pending",
                }
            )
        self.plan = {
            "seed": self.seed,
            "batch_size": self.batch_size,
            "window_ids": ids,
            "next_batch_index": 0,
            "batches": batches,
        }
        self.snapshots.commit("plan", self.plan)
        return self.plan

    def append_plan_windows(self, window_ids: Sequence[str]) -> dict[str, Any]:
        """Append a new batch without rewriting or removing old plan batches."""

        ids = [str(item) for item in window_ids]
        if self.plan is None:
            return self.initialize_plan(ids)

        existing = list(self.plan.get("window_ids", []))
        existing_set = set(existing)
        new_ids = [window_id for window_id in ids if window_id not in existing_set]
        if not new_ids:
            return self.plan

        batches = list(self.plan.get("batches", []))
        start_index = len(batches)
        for offset, start in enumerate(range(0, len(new_ids), self.batch_size)):
            batch_ids = new_ids[start : start + self.batch_size]
            batches.append(
                {
                    "batch_index": start_index + offset,
                    "window_ids": batch_ids,
                    "status": "pending",
                }
            )

        self.plan["window_ids"] = existing + new_ids
        self.plan["batches"] = batches
        self.plan["next_batch_index"] = len(batches)
        self.snapshots.commit("plan", self.plan)
        return self.plan

    def run_pending(
        self,
        windows: Sequence[Any],
        *,
        crash_after_batches: int | None = None,
        append: bool = False,
    ) -> dict[str, Any]:
        window_map = {self._window_id(window): window for window in windows}
        if append:
            self.append_plan_windows(list(window_map))
        else:
            self.initialize_plan(list(window_map))
        assert self.plan is not None
        if self.ae is None:
            raise RuntimeError("autoencoder is not initialized; call set_ae or fit_baseline_ae first")
        if self.model is None:
            raise RuntimeError("threshold model is not initialized; call set_threshold_model first")

        committed = set(self.result.get("processed_window_ids", []))
        run_processed: list[str] = []
        run_reused: list[str] = []
        committed_batch_count = 0

        for batch in self.plan["batches"]:
            batch_index = int(batch["batch_index"])
            batch_ids = list(batch["window_ids"])
            if all(window_id in committed for window_id in batch_ids):
                run_reused.extend(batch_ids)
                self.reused_this_run.extend(batch_ids)
                committed_batch_count += 1
                continue

            new_records = []
            for window_id in batch_ids:
                if window_id in committed:
                    run_reused.append(window_id)
                    self.reused_this_run.append(window_id)
                    continue
                record = self.process_window(window_map[window_id], batch_index)
                new_records.append(record)
                run_processed.append(window_id)
                self.reconstructed_this_run.append(window_id)

            self._commit_window_batch(batch_index, batch_ids, new_records)
            committed.update(record["window_id"] for record in new_records)
            committed_batch_count += 1

            if crash_after_batches is not None and committed_batch_count >= int(crash_after_batches):
                # Snapshot and fsync have completed. SIGKILL proves recovery does
                # not depend on Python/finally cleanup.
                signal.raise_signal(signal.SIGKILL)

        return {
            "ok": True,
            "processed": run_processed,
            "reused": run_reused,
            "reconstructed_this_run": list(self.reconstructed_this_run),
            "reused_committed_this_run": list(self.reused_this_run),
            "processed_total": len(self.result.get("processed_window_ids", [])),
            "diagnostics_path": str(self.diagnostics_jsonl),
        }

    def process_window(self, window: Any, batch_index: int | None = None) -> dict[str, Any]:
        if self.ae is None or self.model is None:
            raise RuntimeError("pipeline requires AE and threshold model")
        window_id = self._window_id(window)
        values = self._window_values(window)
        segment_id = self._window_segment(window)
        window_index = self._window_index(window)

        preprocessed = self.preprocessor.process(window_id, values)
        forward = self.ae.forward(preprocessed.completed)
        reconstruction = np.asarray(forward.reconstruction, dtype=np.float64).reshape(-1)
        features = extract_features(
            preprocessed,
            reconstruction,
            smooth_kernel=self.preprocess_config.smooth_kernel,
            scoring_config=self.scoring_config,
        )
        decision = adjudicate(
            features.score,
            segment_id,
            self.model,
            strategy=self.strategy,
        )
        row = self.model.effective_row(decision["segment_id"])
        record = {
            "record_id": f"rec-{window_id}-v{self.model.model_version}-{features.checksum[:12]}",
            "record_type": "detection",
            "window_id": window_id,
            "window_index": window_index,
            "segment_id": segment_id,
            "batch_id": batch_index,
            "labels": {
                "true_label": self._get(window, "true_label"),
                "polluted_label": self._get(window, "polluted_label"),
            },
            "preprocess_config": asdict(self.preprocess_config),
            "scoring_config": asdict(self.scoring_config),
            "input_shape": list(self.input_shape),
            "raw_window": preprocessed.raw.tolist(),
            "normalized_window": preprocessed.normalized.tolist(),
            "mask": preprocessed.mask.astype(int).tolist(),
            "completed_window": preprocessed.completed.tolist(),
            "reconstruction": reconstruction.tolist(),
            "ae": {
                "config": asdict(self.ae_config),
                "checksum": self.ae.checksum,
                "latent_fingerprint": self._fingerprint(forward.latent),
                "reconstruction_fingerprint": self._fingerprint(reconstruction),
            },
            "mask_boundary_traces": preprocessed.trace_dicts(),
            "features": features.to_record(),
            "decision": decision,
            "threshold": {
                "model_version": self.model.model_version,
                "model_checksum": self.model.checksum or self.model.recompute_checksum(),
                "strategy": self.strategy,
                "tolerance_ratio": self.model.tolerance_ratio,
                "row": row.to_dict(),
            },
            "calibration_path": list(self.model.calibration_path),
            "recalibration_path": list(self.model.calibration_path) or ["initial_threshold_model"],
        }
        record["record_checksum"] = payload_checksum({k: v for k, v in record.items() if k != "record_checksum"})
        return record

    def apply_calibration(self, request: CalibrationRequest | dict[str, Any]) -> dict[str, Any]:
        if self.model is None:
            raise RuntimeError("threshold model is not initialized")
        if isinstance(request, CalibrationRequest):
            request_obj = request
        else:
            request_data = dict(request)
            request_data.setdefault("feature_index", self.result.get("feature_index", {}))
            request_data.setdefault("input_dim", self.input_dim)
            request_obj = CalibrationRequest(**request_data)

        old_version = self.model.model_version
        outcome = self.calibration.submit(self.model, request_obj)
        if not outcome.get("ok"):
            return outcome

        self.model = outcome["model"]
        propagation = outcome["propagation"]
        affected_ids = propagation["recompute"]["decisions"]
        readjudicated = []
        new_records = []
        for window_id in affected_ids:
            source_record = self.latest_record(window_id)
            if source_record is None:
                continue
            new_record = self._readjudicate_record(source_record, propagation)
            new_records.append(new_record)
            readjudicated.append(window_id)

        self.result.setdefault("records", []).extend(new_records)
        for record in new_records:
            self.result.setdefault("decisions", {})[record["window_id"]] = record["decision"]
        self.result["last_calibration"] = {
            "old_version": old_version,
            "new_version": self.model.model_version,
            "propagation": propagation,
            "budget": outcome["budget"],
            "readjudicated": readjudicated,
            "reused_decisions": propagation["reuse"]["decisions"],
        }
        self.result.setdefault("calibration_history", []).append(
            {
                "old_version": old_version,
                "new_version": self.model.model_version,
                "reason": request_obj.reason,
                "propagation": propagation,
                "budget": outcome["budget"],
                "readjudicated": readjudicated,
                "reused_decisions": propagation["reuse"]["decisions"],
            }
        )
        self._write_diagnostics_file()
        self.snapshots.commit("result", self.result)
        outcome["readjudicated_window_ids"] = readjudicated
        outcome["reused_decision_window_ids"] = propagation["reuse"]["decisions"]
        return outcome

    def latest_record(self, window_id: str) -> dict[str, Any] | None:
        records = self.result.get("records", [])
        for record in reversed(records):
            if record.get("window_id") == window_id:
                return record
        return None

    def replay_diagnostics(self) -> dict[str, Any]:
        from .replay import replay_record

        results = []
        all_match = True
        for record in self.result.get("records", []):
            replay_result = replay_record(record)
            results.append(replay_result)
            all_match = all_match and replay_result["matches"]
        return {"ok": all_match, "results": results}

    def _commit_window_batch(
        self,
        batch_index: int,
        batch_ids: Sequence[str],
        records: Sequence[dict[str, Any]],
    ) -> None:
        result = self.result
        result.setdefault("processed_window_ids", [])
        result.setdefault("committed_batches", [])
        result.setdefault("records", [])
        result.setdefault("decisions", {})
        result.setdefault("feature_index", {})

        for record in records:
            result["records"].append(record)
            if record["window_id"] not in result["processed_window_ids"]:
                result["processed_window_ids"].append(record["window_id"])
            result["decisions"][record["window_id"]] = record["decision"]
            result["feature_index"][record["window_id"]] = {
                "window_id": record["window_id"],
                "segment_id": record["segment_id"],
                "window_index": record["window_index"],
                "batch_id": record["batch_id"],
                "score": record["features"]["score"],
                "metrics": {
                    key: record["features"][key]
                    for key in (
                        "score",
                        "mean_residual",
                        "max_residual",
                        "smooth_residual",
                        "boundary_instability",
                        "mask_consistency",
                    )
                },
                "feature_checksum": record["features"]["checksum"],
            }
            self.metadata.upsert_window(
                {
                    "window_id": record["window_id"],
                    "window_index": record["window_index"],
                    "segment_id": record["segment_id"],
                    "true_label": (record.get("labels") or {}).get("true_label"),
                    "polluted_label": (record.get("labels") or {}).get("polluted_label", 0),
                    "score": record["features"]["score"],
                    "severity": record["decision"]["severity"],
                    "is_anomaly": record["decision"]["is_anomaly"],
                    "threshold_version": record["threshold"]["model_version"],
                    "feature_checksum": record["features"]["checksum"],
                    "batch_id": batch_index,
                }
            )

        batch_payload = {"batch_index": batch_index, "window_ids": list(batch_ids), "records": list(records)}
        batch_path = self.diagnostics_dir / f"batch_{batch_index:04d}.json"
        from .io_utils import atomic_write_json

        atomic_write_json(batch_path, batch_payload)
        self._write_diagnostics_file()

        result["committed_batches"].append(
            {
                "batch_index": batch_index,
                "window_ids": list(batch_ids),
                "record_count": len(records),
                "diagnostic_file": str(batch_path),
            }
        )
        self.snapshots.commit("result", result)

        if self.plan is not None:
            for batch in self.plan["batches"]:
                if int(batch["batch_index"]) == batch_index:
                    batch["status"] = "committed"
            self.plan["next_batch_index"] = batch_index + 1
            self.snapshots.commit("plan", self.plan)

        for record in records:
            self.metadata.record_diagnostic(
                record["record_id"],
                record["window_id"],
                batch_index,
                self.diagnostics_jsonl,
                record["record_checksum"],
            )

    def _readjudicate_record(self, source: dict[str, Any], propagation: dict[str, Any]) -> dict[str, Any]:
        window_id = source["window_id"]
        segment_id = source["segment_id"]
        score = float(source["features"]["score"])
        decision = adjudicate(score, segment_id, self.model, strategy=self.strategy)
        row = self.model.effective_row(decision["segment_id"])
        record = dict(source)
        record.update(
            {
                "record_id": f"readj-{window_id}-v{self.model.model_version}",
                "record_type": "threshold_readjudication",
                "source_record_id": source["record_id"],
                "decision": decision,
                "threshold": {
                    "model_version": self.model.model_version,
                    "model_checksum": self.model.checksum or self.model.recompute_checksum(),
                    "strategy": self.strategy,
                    "tolerance_ratio": self.model.tolerance_ratio,
                    "row": row.to_dict(),
                },
                "calibration_path": list(self.model.calibration_path),
                "recalibration_path": {
                    "path": list(self.model.calibration_path),
                    "propagation": propagation,
                },
            }
        )
        record.pop("record_checksum", None)
        record["record_checksum"] = payload_checksum({k: v for k, v in record.items() if k != "record_checksum"})
        return record

    def _write_diagnostics_file(self) -> None:
        records = self.result.get("records", [])
        lines = []
        for record in records:
            lines.append(__import__("json").dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        atomic_write_bytes(self.diagnostics_jsonl, ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"))

    def _load_stats(self) -> NormalizationStats | None:
        if not self.stats_path.exists():
            return None
        with np.load(self.stats_path, allow_pickle=False) as data:
            return NormalizationStats(mean=data["mean"].copy(), scale=data["scale"].copy())

    def _load_ae(self) -> DeterministicAutoencoder | None:
        if not self.ae_path.exists():
            return None
        return DeterministicAutoencoder.load_npz(self.ae_path, self.ae_config)

    def _load_threshold_model(self) -> ThresholdModel | None:
        envelope = self.snapshots.load("threshold")
        if envelope is None:
            return None
        return ThresholdModel.from_dict(envelope["payload"])

    def _empty_result(self) -> dict[str, Any]:
        return {
            "processed_window_ids": [],
            "committed_batches": [],
            "records": [],
            "decisions": {},
            "feature_index": {},
            "compute_ledger": [],
        }

    @staticmethod
    def _window_id(window: Any) -> str:
        if isinstance(window, dict):
            return str(window["window_id"])
        return str(window.window_id)

    @staticmethod
    def _window_values(window: Any) -> Any:
        if isinstance(window, dict):
            return window["values"]
        return window.values

    @staticmethod
    def _window_segment(window: Any) -> str:
        return DetectionPipeline._get(window, "segment_id") or DetectionPipeline._get(window, "segment")

    @staticmethod
    def _window_index(window: Any) -> int | None:
        value = DetectionPipeline._get(window, "window_index")
        return None if value is None else int(value)

    @staticmethod
    def _get(window: Any, key: str) -> Any:
        if isinstance(window, dict):
            return window.get(key)
        return getattr(window, key, None)

    @staticmethod
    def _fingerprint(array: np.ndarray) -> str:
        from .io_utils import array_fingerprint

        return array_fingerprint(np.asarray(array, dtype=np.float64))


def load_diagnostic_records(path: str | Path) -> list[dict[str, Any]]:
    return read_jsonl(path)
