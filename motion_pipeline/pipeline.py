"""Four-stage orchestration, recovery reuse, and byte-stable final artifacts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import PipelineConfig
from .dataset import load_dataset
from .deterministic import (
    atomic_write_bytes,
    canonical_bytes,
    checksum_without,
    sha256_bytes,
)
from .errors import BusinessError
from .snapshots import STAGES, SnapshotStore
from .stages import (
    aggregate,
    encode_motion,
    extrapolate,
    sample_frames,
)
from .verification import verify_checkpoint, verify_final_result


StageCallback = Callable[[int, dict[str, Any]], None]
StopCallback = Callable[[], bool]


@dataclass
class RunOutcome:
    status: str
    completed_stages: int
    checkpoint: dict[str, Any] | None
    final_result: dict[str, Any] | None
    final_result_bytes: bytes | None
    reused: list[dict[str, Any]]
    recomputed: list[dict[str, Any]]
    degradations: list[dict[str, Any]]
    verification: dict[str, Any] | None
    report: dict[str, Any] = field(default_factory=dict)


class Pipeline:
    def __init__(self, workdir: str | Path, config: PipelineConfig | None = None) -> None:
        self.workdir = Path(workdir)
        self.config = config or PipelineConfig()
        self.store = SnapshotStore(self.workdir)
        self.final_path = self.workdir / "final" / "result.json"

    def _stage_payload(
        self,
        stage_index: int,
        dataset: dict[str, Any],
        completed_payloads: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if stage_index == 0:
            return sample_frames(dataset, self.config)
        if stage_index == 1:
            return encode_motion(dataset, completed_payloads[0], self.config)
        if stage_index == 2:
            return extrapolate(
                dataset, completed_payloads[0], completed_payloads[1], self.config
            )
        if stage_index == 3:
            return aggregate(
                dataset,
                completed_payloads[0],
                completed_payloads[1],
                completed_payloads[2],
                self.config,
            )
        raise BusinessError(
            "E_PARTIAL_COMMIT",
            "stage index is outside the four-stage state machine",
            stage="unknown",
        )

    def _check_stored_config_compatibility(
        self, checkpoint: dict[str, Any], dataset: dict[str, Any]
    ) -> None:
        stages = checkpoint["stages"]
        if checkpoint["task_clip_ids"] != dataset["clip_ids"]:
            raise BusinessError(
                "E_RECOVERY_MISMATCH",
                "committed checkpoint clip set differs from the input manifest",
                stage=checkpoint["committed_stage"],
                version=checkpoint["version"],
                field="clip_ids",
            )
        if len(stages) >= 1:
            stored_ratio = stages[0]["payload"].get("config", {}).get(
                "observation_ratio"
            )
            if stored_ratio != self.config.observation_ratio:
                raise BusinessError(
                    "E_RECOVERY_MISMATCH",
                    "observation ratio differs from the committed frame-sample stage",
                    stage="frame_sample",
                    version=1,
                    field="observation_ratio",
                )
        if len(stages) >= 3:
            stored_config = stages[2]["payload"].get("config", {})
            if (
                stored_config.get("window_length")
                != self.config.history_window_length
                or stored_config.get("decay") != float(self.config.decay)
            ):
                raise BusinessError(
                    "E_RECOVERY_MISMATCH",
                    "temporal extrapolation configuration differs from the checkpoint",
                    stage="temporal_extrapolation",
                    version=3,
                    field="temporal_config",
                )
        if len(stages) >= 4:
            stored_hit_distance = stages[3]["payload"].get("config", {}).get(
                "hit_distance_px"
            )
            if stored_hit_distance != float(self.config.hit_distance_px):
                raise BusinessError(
                    "E_RECOVERY_MISMATCH",
                    "hit distance differs from the committed aggregation stage",
                    stage="result_aggregation",
                    version=4,
                    field="hit_distance_px",
                )

    def _build_final_result(
        self, checkpoint: dict[str, Any]
    ) -> dict[str, Any]:
        aggregated = checkpoint["stages"][3]["payload"]
        per_clip = {
            clip_id: {
                "prediction_frames": aggregated["clips"][clip_id][
                    "prediction_frames"
                ],
                "trajectory_stats": aggregated["clips"][clip_id][
                    "trajectory_stats"
                ],
            }
            for clip_id in checkpoint["task_clip_ids"]
        }
        body = {
            "schema_version": 1,
            "kind": "motion-pipeline-final-result",
            "clip_ids": checkpoint["task_clip_ids"],
            "config": {
                "history_window_length": self.config.history_window_length,
                "decay": float(self.config.decay),
                "observation_ratio": float(self.config.observation_ratio),
                "hit_distance_px": float(self.config.hit_distance_px),
            },
            "per_clip": per_clip,
            "overall_metrics": aggregated["overall_metrics"],
            "checkpoint_versions": checkpoint["version_history"],
        }
        final_result = dict(body, result_checksum=checksum_without(body, "__none__"))
        final_result["result_checksum"] = checksum_without(
            final_result, "result_checksum"
        )
        return final_result

    def _write_final(self, final_result: dict[str, Any]) -> bytes:
        data = canonical_bytes(final_result) + b"\n"
        atomic_write_bytes(self.final_path, data)
        return data

    def _build_report(
        self,
        *,
        status: str,
        checkpoint: dict[str, Any] | None,
        final_result: dict[str, Any] | None,
        final_bytes: bytes | None,
        reused: list[dict[str, Any]],
        recomputed: list[dict[str, Any]],
        degradations: list[dict[str, Any]],
        verification: dict[str, Any] | None,
    ) -> dict[str, Any]:
        checkpoint_bytes = (
            self.store.snapshot_path.read_bytes()
            if self.store.snapshot_path.exists()
            else b""
        )
        report: dict[str, Any] = {
            "schema_version": 1,
            "status": status,
            "completed_stages": checkpoint["version"] if checkpoint else 0,
            "committed_stage": (
                checkpoint["committed_stage"] if checkpoint else None
            ),
            "stage_versions": (
                checkpoint["version_history"] if checkpoint else []
            ),
            "cursor": checkpoint["cursor"] if checkpoint else None,
            "reused": reused,
            "recomputed": recomputed,
            "degradations": degradations,
            "degradations_applied": bool(degradations),
            "checkpoint_path": "snapshots/checkpoint.json",
            "checkpoint_sha256": sha256_bytes(checkpoint_bytes) if checkpoint_bytes else None,
            "final_result_path": (
                "final/result.json" if final_bytes is not None else None
            ),
            "final_result_sha256": (
                sha256_bytes(final_bytes) if final_bytes is not None else None
            ),
            "cross_stage_verification": verification,
            "overall_metrics": (
                final_result["overall_metrics"] if final_result else None
            ),
            "per_clip_summary": (
                {
                    clip_id: record["trajectory_stats"]
                    for clip_id, record in final_result["per_clip"].items()
                }
                if final_result
                else None
            ),
        }
        report["report_checksum"] = checksum_without(report, "report_checksum")
        return report

    def run(
        self,
        manifest_path: str | Path,
        frames_path: str | Path,
        *,
        reset: bool = False,
        on_stage_committed: StageCallback | None = None,
        stop_requested: StopCallback | None = None,
    ) -> RunOutcome:
        self.config.validate()
        dataset = load_dataset(manifest_path, frames_path, self.config)
        dataset["degradations"] = list(dataset.get("degradations", []))
        if reset:
            self.store.reset()
            self.final_path.unlink(missing_ok=True)
        checkpoint = self.store.load()
        reused: list[dict[str, Any]] = []
        recomputed: list[dict[str, Any]] = []
        if checkpoint is not None:
            self._check_stored_config_compatibility(checkpoint, dataset)
            if checkpoint["version"] < 4:
                self.final_path.unlink(missing_ok=True)
            for record in checkpoint["stages"]:
                reused.append(
                    {
                        "stage": record["stage"],
                        "version": record["version"],
                        "clip_ids": record["completed_clip_ids"],
                    }
                )
        stage_outputs = (
            [record["payload"] for record in checkpoint["stages"]]
            if checkpoint is not None
            else []
        )
        status = "completed"
        for stage_index, stage in enumerate(STAGES):
            if stage_index < len(stage_outputs):
                continue
            if stop_requested is not None and stop_requested():
                status = "stopped"
                break
            payload = self._stage_payload(stage_index, dataset, stage_outputs)
            if payload.get("clip_ids") != dataset["clip_ids"]:
                raise BusinessError(
                    "E_PARTIAL_COMMIT",
                    "stage output does not include every clip",
                    stage=stage["name"],
                    field="clip_ids",
                )
            committed = self.store.commit_stage(
                stage_index,
                payload,
                required_clip_ids=dataset["clip_ids"],
                expected_version=checkpoint["version"] if checkpoint else None,
            )
            checkpoint = committed
            stage_outputs.append(payload)
            recomputed.append(
                {
                    "stage": stage["name"],
                    "version": committed["version"],
                    "clip_ids": list(dataset["clip_ids"]),
                }
            )
            if on_stage_committed is not None:
                on_stage_committed(stage_index, committed)
            if stop_requested is not None and stop_requested():
                status = "stopped"
                break
        final_result: dict[str, Any] | None = None
        final_bytes: bytes | None = None
        verification: dict[str, Any] | None = None
        if status == "completed" and checkpoint is not None and checkpoint["version"] == 4:
            verification = verify_checkpoint(checkpoint)
            final_result = self._build_final_result(checkpoint)
            verify_final_result(final_result, checkpoint)
            final_bytes = self._write_final(final_result)
        report = self._build_report(
            status=status,
            checkpoint=checkpoint,
            final_result=final_result,
            final_bytes=final_bytes,
            reused=reused,
            recomputed=recomputed,
            degradations=dataset["degradations"],
            verification=verification,
        )
        return RunOutcome(
            status=status,
            completed_stages=checkpoint["version"] if checkpoint else 0,
            checkpoint=checkpoint,
            final_result=final_result,
            final_result_bytes=final_bytes,
            reused=reused,
            recomputed=recomputed,
            degradations=dataset["degradations"],
            verification=verification,
            report=report,
        )
