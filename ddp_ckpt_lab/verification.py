"""Resume equivalence and replay reproducibility verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .canonical import array_bytes, arrays_equal, sha256_bytes, sha256_json
from .checkpoints import CheckpointInfo, CheckpointStore
from .config import TrainingConfig
from .errors import BusinessError
from .model import PARAMETER_NAMES
from .state import TrainingState
from .training import Trainer, initialize_training


@dataclass(frozen=True)
class Divergence:
    step: int
    worker_id: int | None
    parameter_name: str
    state_kind: str
    expected_digest: str
    actual_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "worker_id": self.worker_id,
            "parameter_name": self.parameter_name,
            "state_kind": self.state_kind,
            "expected_digest": self.expected_digest,
            "actual_digest": self.actual_digest,
        }


def first_divergence(
    expected: TrainingState,
    actual: TrainingState,
    *,
    step: int | None = None,
) -> Divergence | None:
    check_step = int(expected.global_step if step is None else step)
    if expected.global_step != actual.global_step:
        return Divergence(
            step=check_step,
            worker_id=None,
            parameter_name="global_step",
            state_kind="global_step",
            expected_digest=sha256_json(expected.global_step),
            actual_digest=sha256_json(actual.global_step),
        )
    if len(expected.workers) != len(actual.workers):
        return Divergence(
            step=check_step,
            worker_id=None,
            parameter_name="worker_count",
            state_kind="topology",
            expected_digest=sha256_json(len(expected.workers)),
            actual_digest=sha256_json(len(actual.workers)),
        )
    for expected_worker, actual_worker in zip(expected.workers, actual.workers):
        if expected_worker.worker_id != actual_worker.worker_id:
            return Divergence(
                step=check_step,
                worker_id=expected_worker.worker_id,
                parameter_name="worker_id",
                state_kind="topology",
                expected_digest=sha256_json(expected_worker.worker_id),
                actual_digest=sha256_json(actual_worker.worker_id),
            )
        worker_id = expected_worker.worker_id
        for name in PARAMETER_NAMES:
            left = np.asarray(expected_worker.parameters[name])
            right = np.asarray(actual_worker.parameters[name])
            if not arrays_equal(left, right):
                return Divergence(
                    step=check_step,
                    worker_id=worker_id,
                    parameter_name=name,
                    state_kind="parameter",
                    expected_digest=sha256_bytes(array_bytes(left)),
                    actual_digest=sha256_bytes(array_bytes(right)),
                )
        for name in PARAMETER_NAMES:
            left = np.asarray(expected_worker.momentum[name])
            right = np.asarray(actual_worker.momentum[name])
            if not arrays_equal(left, right):
                return Divergence(
                    step=check_step,
                    worker_id=worker_id,
                    parameter_name=name,
                    state_kind="momentum",
                    expected_digest=sha256_bytes(array_bytes(left)),
                    actual_digest=sha256_bytes(array_bytes(right)),
                )
        expected_cursor = expected_worker.cursor
        actual_cursor = actual_worker.cursor
        if expected_cursor.epoch != actual_cursor.epoch:
            return Divergence(
                step=check_step,
                worker_id=worker_id,
                parameter_name="cursor.epoch",
                state_kind="data_cursor",
                expected_digest=sha256_json(expected_cursor.epoch),
                actual_digest=sha256_json(actual_cursor.epoch),
            )
        if expected_cursor.position != actual_cursor.position:
            return Divergence(
                step=check_step,
                worker_id=worker_id,
                parameter_name="cursor.position",
                state_kind="data_cursor",
                expected_digest=sha256_json(expected_cursor.position),
                actual_digest=sha256_json(actual_cursor.position),
            )
        if not arrays_equal(expected_cursor.order, actual_cursor.order):
            return Divergence(
                step=check_step,
                worker_id=worker_id,
                parameter_name="cursor.order",
                state_kind="data_cursor",
                expected_digest=sha256_bytes(array_bytes(expected_cursor.order)),
                actual_digest=sha256_bytes(array_bytes(actual_cursor.order)),
            )
        expected_rng_digest = sha256_json(expected_cursor.rng_state)
        actual_rng_digest = sha256_json(actual_cursor.rng_state)
        if expected_rng_digest != actual_rng_digest:
            return Divergence(
                step=check_step,
                worker_id=worker_id,
                parameter_name="cursor.rng_state",
                state_kind="rng_state",
                expected_digest=expected_rng_digest,
                actual_digest=actual_rng_digest,
            )
    return None


def assert_bitwise_equal(
    expected: TrainingState,
    actual: TrainingState,
    *,
    step: int | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    divergence = first_divergence(expected, actual, step=step)
    if divergence is None:
        return
    details: dict[str, Any] = {"first_divergence": divergence.as_dict()}
    if context:
        details["context"] = context
    raise BusinessError(
        "E_DIVERGE",
        "逐位状态比较发现不一致",
        **details,
    )


class VerificationService:
    def __init__(self, store: CheckpointStore) -> None:
        self.store = store

    def verify_resume_consistency(
        self,
        config: TrainingConfig,
        checkpoint: CheckpointInfo | str,
        continuation_steps: int,
    ) -> dict[str, Any]:
        if continuation_steps <= 0:
            raise ValueError("continuation_steps must be positive")
        loaded = self.store.load(checkpoint, config)
        if loaded.source_config.learning_rate != config.learning_rate:
            raise BusinessError(
                "E_CONFIG",
                "逐位续训等价校验要求学习率保持不变",
                field="learning_rate",
                checkpoint_value=loaded.source_config.learning_rate,
                requested_value=config.learning_rate,
                suggestion="learning_rate 变化属于兼容迁移，但不能与该检查点的未中断基线逐位比较",
            )

        init_bundle = initialize_training(config)
        baseline = Trainer(config, init_bundle.dataset, init_bundle.state)
        baseline.train_until(loaded.info.global_step)
        assert_bitwise_equal(
            baseline.state,
            loaded.state,
            step=loaded.info.global_step,
            context={"phase": "checkpoint_boundary", "checkpoint": loaded.info.checkpoint_id},
        )
        checkpoint_boundary_digest = loaded.state.state_digest(config)

        resumed = Trainer(config, init_bundle.dataset, loaded.state)
        comparisons: list[dict[str, Any]] = []
        for _ in range(continuation_steps):
            baseline_record = baseline.step()
            resumed_record = resumed.step()
            assert_bitwise_equal(
                baseline.state,
                resumed.state,
                step=baseline.state.global_step,
                context={
                    "phase": "continuation",
                    "checkpoint": loaded.info.checkpoint_id,
                    "baseline_progress_digest": baseline_record.progress_digest,
                    "resumed_progress_digest": resumed_record.progress_digest,
                },
            )
            comparisons.append(
                {
                    "step": baseline.state.global_step,
                    "baseline_state_digest": baseline_record.state_digest,
                    "resumed_state_digest": resumed_record.state_digest,
                    "baseline_progress_digest": baseline_record.progress_digest,
                    "resumed_progress_digest": resumed_record.progress_digest,
                    "equal": True,
                }
            )

        return {
            "status": "OK",
            "resume_consistency": True,
            "business_code": "OK",
            "checkpoint_id": loaded.info.checkpoint_id,
            "checkpoint_step": loaded.info.global_step,
            "continuation_steps": continuation_steps,
            "final_step": baseline.state.global_step,
            "checkpoint_state_digest": checkpoint_boundary_digest,
            "final_baseline_state_digest": baseline.state.state_digest(config),
            "final_resumed_state_digest": resumed.state.state_digest(config),
            "final_baseline_payload_digest": baseline.state.payload_digest(),
            "final_resumed_payload_digest": resumed.state.payload_digest(),
            "comparisons": comparisons,
            "first_divergence": None,
            "migration": loaded.migration.as_dict(),
        }

    def verify_replay(
        self,
        config: TrainingConfig,
        checkpoint: CheckpointInfo | str,
        replay_steps: int,
    ) -> dict[str, Any]:
        if replay_steps <= 0:
            raise ValueError("replay_steps must be positive")
        fingerprint_before = self.store.fingerprint(checkpoint)
        try:
            first_loaded = self.store.load(checkpoint, config)
            second_loaded = self.store.load(checkpoint, config)
        except BusinessError as exc:
            if exc.code == "E_CONFIG":
                raise BusinessError(
                    "E_REPLAY",
                    "训练配置或数据分片方式与检查点不一致，无法一致性重放",
                    reason="configuration_or_sharding_conflict",
                    cause=exc.to_dict(),
                ) from exc
            raise

        assert_bitwise_equal(
            first_loaded.state,
            second_loaded.state,
            step=first_loaded.state.global_step,
            context={"phase": "replay_restore"},
        )
        first_trainer = Trainer(config, initialize_training(config).dataset, first_loaded.state)
        second_trainer = Trainer(config, initialize_training(config).dataset, second_loaded.state)
        comparisons: list[dict[str, Any]] = []
        for _ in range(replay_steps):
            first_record = first_trainer.step()
            second_record = second_trainer.step()
            divergence = first_divergence(
                first_trainer.state,
                second_trainer.state,
                step=first_trainer.state.global_step,
            )
            if divergence is not None:
                raise BusinessError(
                    "E_REPLAY",
                    "同一检查点两次恢复重放产生了不同状态",
                    reason="replay_state_mismatch",
                    first_divergence=divergence.as_dict(),
                    step=first_trainer.state.global_step,
                )
            comparisons.append(
                {
                    "step": first_trainer.state.global_step,
                    "first_state_digest": first_record.state_digest,
                    "second_state_digest": second_record.state_digest,
                    "equal": True,
                }
            )

        fingerprint_after = self.store.fingerprint(checkpoint)
        if fingerprint_before != fingerprint_after:
            raise BusinessError(
                "E_REPLAY",
                "重放过程修改了原始检查点",
                reason="checkpoint_modified_during_replay",
                before=fingerprint_before,
                after=fingerprint_after,
            )
        if first_trainer.state.state_digest(config) != second_trainer.state.state_digest(config):
            raise BusinessError(
                "E_REPLAY",
                "两次重放的最终状态摘要不一致",
                reason="replay_digest_mismatch",
            )
        return {
            "status": "OK",
            "reproducible": True,
            "business_code": "OK",
            "checkpoint_id": first_loaded.info.checkpoint_id,
            "checkpoint_step": first_loaded.info.global_step,
            "replay_steps": replay_steps,
            "final_step": first_trainer.state.global_step,
            "first_run_state_digest": first_trainer.state.state_digest(config),
            "second_run_state_digest": second_trainer.state.state_digest(config),
            "first_run_payload_digest": first_trainer.state.payload_digest(),
            "second_run_payload_digest": second_trainer.state.payload_digest(),
            "checkpoint_fingerprint_before": fingerprint_before,
            "checkpoint_fingerprint_after": fingerprint_after,
            "checkpoint_unmodified": True,
            "comparisons": comparisons,
            "first_divergence": None,
            "migration": first_loaded.migration.as_dict(),
        }
