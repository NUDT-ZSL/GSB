"""Four-stage state machine orchestrating framing, pooling, training, summary.

Stage transitions are committed through :class:`SnapshotStore` so that an
interrupted process resumes from the last *committed* epoch: all intermediate
gradient and partial-epoch state lives only in memory and is discarded on
restart by construction.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

from ..config.config import EngineConfig
from ..data.dataset import read_manifest
from ..engine import pipeline as pl
from ..engine.training import (LinearRegressor, chunk_plan,
                               enforce_resource_limits)
from .snapshot import SnapshotStore, STAGES


class ForceTermination(Exception):
    """Raised by the injected kill hook to simulate a hard process stop."""


@dataclass
class TrainingRun:
    label: str
    precision: str
    chunk_size: int
    plan: list
    weights: np.ndarray
    history: list
    converged_epoch: int
    config_snapshot: dict


def config_fingerprint(config: EngineConfig) -> str:
    return hashlib.sha256(config.canonical_json()).hexdigest()


class EngineRunner:
    def __init__(self, config: EngineConfig, data_dir: str, snapshot_dir: str):
        config.validate()
        self.config = config
        self.data_dir = data_dir
        self.manifest = read_manifest(f"{data_dir}/manifest.jsonl")
        self.store = SnapshotStore(
            snapshot_dir, config_fingerprint(config),
            config.snapshot_schema_version)
        self.kill_after_epoch: int | None = None
        self.events: list[dict] = []
        self.degradations: list[dict] = []
        self.last_error: dict | None = None

    def _record(self, event: str, **details) -> None:
        self.events.append({"event": event, **details})

    # ------------------------------------------------------------------ #
    # Stage 1: framing
    # ------------------------------------------------------------------ #
    def run_framing(self) -> list[dict]:
        if self.store.is_complete("framing"):
            envelope, snapshot = self.store.load_latest("framing")
            self._record("framing.reused", version=snapshot["version"])
            return self._decode_framing(snapshot)
        outputs: list[dict] = []
        for entry in self.manifest:
            dialogue = pl.load_dialogue(self.data_dir, entry,
                                        self.config.frame_length,
                                        self.config.hop_length)
            outputs.append(pl.process_dialogue(dialogue, self.config))
        payload = {
            "dialog_ids": [item["dialog_id"] for item in outputs],
            "frame_length": self.config.frame_length,
            "hop_length": self.config.hop_length,
            "frame_counts": [int(item["frame_features"].shape[0])
                             for item in outputs],
            "boundaries": [[list(span) for span in item["boundaries"]]
                           for item in outputs],
        }
        arrays = {}
        for item in outputs:
            key = item["dialog_id"]
            arrays[f"{key}__frame_features"] = item["frame_features"]
            arrays[f"{key}__pooled"] = item["pooled"]
            arrays[f"{key}__targets"] = item["targets"]
            arrays[f"{key}__is_speech"] = item["is_speech"].astype(np.int8)
            arrays[f"{key}__silence"] = item["silence_mask"].astype(np.int8)
        with self.store.commit_lock():
            envelope = self.store.save("framing", payload, arrays)
            self.store.mark_complete("framing", envelope["version"])
        self._record("framing.committed", version=envelope["version"])
        return outputs

    def _decode_framing(self, snapshot: dict) -> list[dict]:
        payload, arrays = snapshot["payload"], snapshot["arrays"]
        outputs: list[dict] = []
        for index, dialog_id in enumerate(payload["dialog_ids"]):
            outputs.append({
                "dialog_id": dialog_id,
                "frame_features": arrays[f"{dialog_id}__frame_features"],
                "pooled": arrays[f"{dialog_id}__pooled"],
                "targets": arrays[f"{dialog_id}__targets"],
                "is_speech": arrays[f"{dialog_id}__is_speech"].astype(bool),
                "silence_mask": arrays[f"{dialog_id}__silence"].astype(bool),
                "boundaries": [tuple(span) for span
                               in payload["boundaries"][index]],
                "segment_meta": self._segment_meta(dialog_id, payload, index),
            })
        return outputs

    @staticmethod
    def _segment_meta(dialog_id: str, payload: dict, index: int) -> list[dict]:
        return [{"dialog_id": dialog_id, "segment_index": seg_idx,
                 "frame_start": span[0], "frame_end": span[1],
                 "speaker_ids": []}
                for seg_idx, span in enumerate(payload["boundaries"][index])]

    # ------------------------------------------------------------------ #
    # Stage 2: pooling / standardisation
    # ------------------------------------------------------------------ #
    def run_pooling(self, framing_outputs: list[dict]) -> dict:
        if self.store.is_complete("pooling"):
            envelope, snapshot = self.store.load_latest("pooling")
            self._record("pooling.reused", version=snapshot["version"])
            return self._decode_pooling(snapshot, framing_outputs)
        dataset = pl.pool_from_framing(framing_outputs, self.config)
        payload = {
            "dialog_ids": dataset["dialog_ids"],
            "segment_meta": dataset["segment_meta"],
            "n_segments": int(dataset["x"].shape[0]),
            "n_features": int(dataset["x"].shape[1]),
            "mean": dataset["mean"].tolist(),
            "std": dataset["std"].tolist(),
            "feature_scale": self.config.feature_scale,
        }
        with self.store.commit_lock():
            envelope = self.store.save(
                "pooling", payload,
                {"raw_x": dataset["raw_x"], "x": dataset["x"],
                 "x_aug": dataset["x_aug"], "y": dataset["y"]})
            self.store.mark_complete("pooling", envelope["version"])
        self._record("pooling.committed", version=envelope["version"])
        dataset["version"] = envelope["version"]
        return dataset

    def _decode_pooling(self, snapshot: dict,
                        framing_outputs: list[dict]) -> dict:
        payload, arrays = snapshot["payload"], snapshot["arrays"]
        return {
            "dialog_ids": payload["dialog_ids"],
            "per_dialog": framing_outputs,
            "raw_x": arrays["raw_x"],
            "x": arrays["x"],
            "x_aug": arrays["x_aug"],
            "y": arrays["y"],
            "mean": np.array(payload["mean"], dtype=np.float64),
            "std": np.array(payload["std"], dtype=np.float64),
            "segment_meta": payload["segment_meta"],
            "version": snapshot["version"],
        }

    # ------------------------------------------------------------------ #
    # Stage 3: resumable chunked training
    # ------------------------------------------------------------------ #
    def run_training(self, dataset: dict) -> dict:
        n_segments, n_features = dataset["x"].shape
        chunk_size, degradations = enforce_resource_limits(
            len(self.manifest), n_segments, n_features, self.config)
        self.degradations = degradations
        plan = chunk_plan(n_segments, chunk_size)

        start_epoch = 1
        weights = np.zeros((n_features + 1, 2), dtype=np.float64)
        previous_loss = None
        history: list[dict] = []
        epoch_weights: list[np.ndarray] = []
        skipped_epochs: list[int] = []
        protected_epochs: list[int] = []
        if self.store.latest_version("training") > 0:
            envelope, snapshot = self.store.load_latest("training")
            progress = snapshot["payload"]
            start_epoch = progress["next_epoch"]
            weights = snapshot["arrays"]["weights"]
            previous_loss = progress["previous_loss"]
            history = progress["history"]
            epoch_weights = [snapshot["arrays"][f"epoch_{ep}"]
                             for ep in
                             range(1, progress["last_epoch"] + 1)
                             if f"epoch_{ep}" in snapshot["arrays"]]
            skipped_epochs = list(progress.get("skipped_epochs", []))
            protected_epochs = list(progress.get("protected_epochs", []))
            skipped_epochs.extend(
                ep for ep in range(1, start_epoch) if ep in protected_epochs
                and ep not in skipped_epochs)
            self._record("training.resumed",
                         last_committed_epoch=progress["last_epoch"],
                         next_epoch=start_epoch)
        else:
            zero_model = LinearRegressor(n_features, self.config)
            previous_loss = zero_model.loss_mse(dataset["x_aug"],
                                                dataset["y"])

        model = LinearRegressor(n_features, self.config)
        model.weights = weights.copy()
        converged_epoch = 0
        for epoch in range(start_epoch, self.config.epochs + 1):
            grad = model.gradient(dataset["x_aug"], dataset["y"], plan)
            model.apply_update(grad)
            loss = model.loss_mse(dataset["x_aug"], dataset["y"])
            converged, improvement, reason = model.check_convergence(
                previous_loss, loss)
            record = {"epoch": epoch, "loss": loss,
                      "improvement": improvement, "converged": converged,
                      "convergence_reason": reason}
            history.append(record)
            previous_loss = loss
            epoch_weights.append(model.weights.copy())
            # Every committed progress snapshot is protected: on resume the
            # epoch starts from its committed boundary rather than replaying.
            protected_epochs.append(epoch)
            payload = {
                "last_epoch": epoch,
                "next_epoch": epoch + 1,
                "previous_loss": loss,
                "history": history,
                "chunk_size": chunk_size,
                "plan": [list(span) for span in plan],
                "degradations": degradations,
                "skipped_epochs": skipped_epochs,
                "protected_epochs": protected_epochs,
                "completed": False,
            }
            weight_arrays = {"weights": model.weights}
            for ep, epoch_weight in enumerate(epoch_weights, start=1):
                weight_arrays[f"epoch_{ep}"] = epoch_weight
            with self.store.commit_lock():
                envelope = self.store.save(
                    "training", payload, weight_arrays)
            self._record("training.epoch_committed", epoch=epoch,
                         version=envelope["version"], loss=loss)
            if self.kill_after_epoch is not None and \
                    epoch == self.kill_after_epoch and \
                    not self.store.is_complete("training"):
                self._record("training.force_terminated", epoch=epoch)
                raise ForceTermination(
                    f"forced termination after committed epoch {epoch}")
            if converged:
                converged_epoch = epoch
                break

        final_payload = {
            "last_epoch": history[-1]["epoch"],
            "next_epoch": history[-1]["epoch"] + 1,
            "previous_loss": previous_loss,
            "history": history,
            "chunk_size": chunk_size,
            "plan": [list(span) for span in plan],
            "degradations": degradations,
            "skipped_epochs": skipped_epochs,
            "protected_epochs": protected_epochs,
            "converged_epoch": converged_epoch,
            "completed": True,
        }
        weight_arrays = {"weights": model.weights}
        for ep, epoch_weight in enumerate(epoch_weights, start=1):
            weight_arrays[f"epoch_{ep}"] = epoch_weight
        with self.store.commit_lock():
            envelope = self.store.save(
                "training", final_payload, weight_arrays)
            self.store.mark_complete("training", envelope["version"])
        self._record("training.completed", version=envelope["version"],
                     converged_epoch=converged_epoch)
        return {"weights": model.weights, "history": history,
                "converged_epoch": converged_epoch, "plan": plan,
                "chunk_size": chunk_size,
                "degradations": degradations,
                "epoch_weights": epoch_weights,
                "skipped_epochs": skipped_epochs,
                "protected_epochs": protected_epochs,
                "version": envelope["version"]}

    # ------------------------------------------------------------------ #
    # Stage 4: summary
    # ------------------------------------------------------------------ #
    def run_summary(self, dataset: dict, training: dict) -> dict:
        if self.store.is_complete("summary"):
            envelope, snapshot = self.store.load_latest("summary")
            self._record("summary.reused", version=snapshot["version"])
            return snapshot["payload"]
        scores = pl.dialogue_scores(training["weights"], dataset)
        payload = {
            "dialog_ids": dataset["dialog_ids"],
            "dialogue_scores": scores,
            "final_weights": training["weights"].tolist(),
            "loss_history": training["history"],
            "converged_epoch": training["converged_epoch"],
            "chunk_size": training["chunk_size"],
            "degradations": training["degradations"],
        }
        with self.store.commit_lock():
            envelope = self.store.save("summary", payload)
            self.store.mark_complete("summary", envelope["version"])
        self._record("summary.committed", version=envelope["version"])
        payload["version"] = envelope["version"]
        return payload

    def run_all(self) -> dict:
        framing = self.run_framing()
        dataset = self.run_pooling(framing)
        training = self.run_training(dataset)
        summary = self.run_summary(dataset, training)
        return {"framing": framing, "dataset": dataset,
                "training": training, "summary": summary,
                "events": self.events}


def stage_status(store: SnapshotStore) -> dict:
    status = {}
    for stage in STAGES:
        status[stage] = {
            "latest_version": store.latest_version(stage),
            "complete": store.is_complete(stage),
        }
    return status
