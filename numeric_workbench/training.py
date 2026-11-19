"""Deterministic full-batch and explicitly reduced chunked training."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import numpy as np

from .config import TrainingConfig
from .convergence import ConvergenceDecision, convergence_summary, decide_convergence
from .features import PreparedData
from .precision import PrecisionEngine
from .serialization import (
    canonical_dumps,
    decode_array,
    encode_array,
    float_from_hex,
    float_hex,
    sha256_bytes,
    sha256_json,
)


@dataclass
class ChunkTrace:
    round_number: int
    chunk_index: int
    local_indices: tuple[int, ...]
    global_indices: tuple[int, ...]
    row_ids: tuple[str, ...]
    gradient: np.ndarray
    feature_l2: float
    learning_rate: float
    used_in_reduction: bool

    def gradient_float(self, engine: PrecisionEngine) -> list[float]:
        return [float(item) for item in engine.to_float(self.gradient)]

    def to_dict(self, engine: PrecisionEngine, *, include_vector: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "round": self.round_number,
            "chunk_index": self.chunk_index,
            "local_indices": list(self.local_indices),
            "global_indices": list(self.global_indices),
            "row_ids": list(self.row_ids),
            "sample_count": len(self.local_indices),
            "feature_l2": self.feature_l2,
            "learning_rate_effective": self.learning_rate,
            "used_in_reduction": self.used_in_reduction,
            "gradient_sha256": sha256_json(encode_array(self.gradient)),
        }
        if include_vector:
            payload["gradient"] = self.gradient_float(engine)
        return payload


@dataclass
class TrainingRun:
    run_id: str
    precision: str
    reduce_mode: str
    config: TrainingConfig
    prepared: PreparedData
    params: np.ndarray
    params_float: tuple[float, ...]
    loss_trajectory: list[float]
    validation_trajectory: list[dict[str, Any]]
    parameter_trajectory: list[np.ndarray]
    decisions: list[dict[str, Any]]
    chunk_trace: list[ChunkTrace]
    rounds: int
    stop_reason: str
    converged: bool
    fixed_trace: list[dict[str, Any]] = field(default_factory=list)
    initial_params: np.ndarray | None = None
    core_digest: str = ""
    interrupted: bool = False
    discarded_round: int | None = None
    started_from_round: int = 1
    committed_rounds: list[int] = field(default_factory=list)
    rebuilt_rounds: list[int] = field(default_factory=list)

    @property
    def chunk_plan_sha256(self) -> str:
        return str(self.prepared.chunk_plan_fingerprint["sha256"])

    @property
    def losses_hex(self) -> list[str]:
        return [float_hex(value) for value in self.loss_trajectory]

    @property
    def params_hex(self) -> list[str]:
        return [float_hex(value) for value in self.params_float]

    def loss_at(self, round_number: int) -> float:
        return self.loss_trajectory[round_number - 1]

    def core_payload(self) -> dict[str, Any]:
        return {
            "precision": self.precision,
            "reduce_mode": self.reduce_mode,
            "chunk_plan_sha256": self.chunk_plan_sha256,
            "train_index_sha256": self.prepared.sample_fingerprint["train_sha256"],
            "validation_index_sha256": self.prepared.sample_fingerprint["validation_sha256"],
            "loss_trajectory_hex": self.losses_hex,
            "validation_trajectory": self.validation_trajectory,
            "parameter_trajectory": [encode_array(item) for item in self.parameter_trajectory],
            "decisions": self.decisions,
            "params": encode_array(self.params),
            "rounds": self.rounds,
            "stop_reason": self.stop_reason,
            "converged": self.converged,
        }

    def compute_digest(self) -> str:
        return sha256_json(self.core_payload())

    def trajectory_digest(self) -> str:
        return sha256_json(
            {
                "losses": self.losses_hex,
                "validation": self.validation_trajectory,
                "params": encode_array(self.params),
                "rounds": self.rounds,
                "stop_reason": self.stop_reason,
            }
        )

    def fixed_replay_proof(self) -> dict[str, Any] | None:
        if self.precision != "fixed_point":
            return None
        engine = self.prepared.engine
        initial = (
            self.initial_params.copy()
            if self.initial_params is not None
            else engine.zeros(self.params.shape[0])
        )
        current = initial.copy()
        all_rounds_match = True
        mismatch_rounds: list[int] = []
        for entry in self.fixed_trace:
            gradient_sum = (
                decode_array(entry["gradient_sum"])
                if isinstance(entry["gradient_sum"], dict)
                else np.asarray(entry["gradient_sum"], dtype=np.int64)
            )
            expected_after = (
                decode_array(entry["params_after"])
                if isinstance(entry["params_after"], dict)
                else np.asarray(entry["params_after"], dtype=np.int64)
            )
            recomputed_after = engine.apply_update(
                current,
                gradient_sum,
                int(entry["sample_count"]),
                float(entry["regularization_strength"]),
                float(entry["learning_rate"]),
                self.prepared.regularized_mask,
            )
            if not np.array_equal(recomputed_after, expected_after):
                all_rounds_match = False
                mismatch_rounds.append(int(entry["round"]))
            current = expected_after
        final_match = np.array_equal(current, self.params)
        quantized_dequantized = engine.raw_numeric(
            np.asarray(self.params_float, dtype=np.float64)
        )
        quantize_roundtrip = np.array_equal(quantized_dequantized, self.params)
        replay_checksum = sha256_json(
            {
                "initial": encode_array(initial),
                "rounds": [
                    {
                        "round": entry["round"],
                        "gradient_sum": (
                            entry["gradient_sum"]
                            if isinstance(entry["gradient_sum"], dict)
                            else encode_array(entry["gradient_sum"])
                        ),
                        "params_after": (
                            entry["params_after"]
                            if isinstance(entry["params_after"], dict)
                            else encode_array(entry["params_after"])
                        ),
                    }
                    for entry in self.fixed_trace
                ],
            }
        )
        return {
            "scale": engine.scale,
            "rounding_mode": engine.rounding_mode,
            "formula": (
                "w_next = w - round_ratio(lr_units * "
                "(round_ratio(gradient_sum,n) + round_ratio(reg_units*w,scale)), scale)"
            ),
            "initial_params_scaled": encode_array(initial),
            "final_params_scaled": encode_array(self.params),
            "final_params": list(self.params_float),
            "operation_trace_sha256": replay_checksum,
            "recomputed_final_matches": bool(final_match and all_rounds_match),
            "quantize_dequantize_roundtrip_matches": bool(quantize_roundtrip),
            "mismatch_rounds": mismatch_rounds,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "precision": self.precision,
            "reduce_mode": self.reduce_mode,
            "rounds": self.rounds,
            "stop_reason": self.stop_reason,
            "converged": self.converged,
            "loss_trajectory": self.loss_trajectory,
            "loss_trajectory_hex": self.losses_hex,
            "validation_trajectory": self.validation_trajectory,
            "parameter_trajectory": [
                encode_array(item) for item in self.parameter_trajectory
            ],
            "final_validation_metrics": (
                self.validation_trajectory[-1] if self.validation_trajectory else None
            ),
            "params": list(self.params_float),
            "params_hex": self.params_hex,
            "params_sha256": sha256_json(encode_array(self.params)),
            "trajectory_sha256": self.trajectory_digest(),
            "core_sha256": self.core_digest,
            "chunk_plan_sha256": self.chunk_plan_sha256,
            "convergence": convergence_summary(
                self.decisions,
                selected_strategy=self.config.convergence_strategy,
                max_iterations=self.config.max_iterations,
                stop_reason=self.stop_reason,
                final_round=self.rounds,
            ),
            "fixed_replay": self.fixed_replay_proof(),
            "interrupted": self.interrupted,
            "discarded_round": self.discarded_round,
            "started_from_round": self.started_from_round,
            "committed_rounds": self.committed_rounds,
            "rebuilt_rounds": self.rebuilt_rounds,
        }

    def checkpoint_state(self) -> dict[str, Any]:
        return {
            "precision": self.precision,
            "reduce_mode": self.reduce_mode,
            "chunk_plan_sha256": self.chunk_plan_sha256,
            "train_index_sha256": self.prepared.sample_fingerprint["train_sha256"],
            "rounds_completed": self.rounds,
            "committed_rounds": list(self.committed_rounds),
            "params": encode_array(self.params),
            "initial_params": encode_array(
                self.initial_params
                if self.initial_params is not None
                else self.prepared.engine.zeros(self.params.shape[0])
            ),
            "loss_trajectory_hex": self.losses_hex,
            "validation_trajectory": self.validation_trajectory,
            "parameter_trajectory": [
                encode_array(item) for item in self.parameter_trajectory
            ],
            "decisions": self.decisions,
            "fixed_trace": _encode_fixed_trace(self.fixed_trace),
            "stop_reason": self.stop_reason,
            "converged": self.converged,
        }


def _encode_fixed_trace(trace: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "round": int(entry["round"]),
            "sample_count": int(entry["sample_count"]),
            "regularization_strength": float(entry["regularization_strength"]),
            "learning_rate": float(entry["learning_rate"]),
            "gradient_sum": encode_array(entry["gradient_sum"]),
            "params_before": encode_array(entry["params_before"]),
            "params_after": encode_array(entry["params_after"]),
        }
        for entry in trace
    ]


def _decode_fixed_trace(trace: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "round": int(entry["round"]),
            "sample_count": int(entry["sample_count"]),
            "regularization_strength": float(entry["regularization_strength"]),
            "learning_rate": float(entry["learning_rate"]),
            "gradient_sum": decode_array(entry["gradient_sum"]),
            "params_before": decode_array(entry["params_before"]),
            "params_after": decode_array(entry["params_after"]),
        }
        for entry in trace
    ]


def _decode_initial_state(initial_state: dict[str, Any], config: TrainingConfig) -> dict[str, Any]:
    losses = [float_from_hex(item) for item in initial_state["loss_trajectory_hex"]]
    if len(losses) != int(initial_state["rounds_completed"]):
        raise ValueError("checkpoint loss trajectory length does not match committed rounds")
    return {
        "params": decode_array(initial_state["params"]),
        "initial_params": decode_array(initial_state["initial_params"]),
        "losses": losses,
        "validation": list(initial_state["validation_trajectory"]),
        "parameter_trajectory": [
            decode_array(item) for item in initial_state.get("parameter_trajectory", [])
        ],
        "decisions": list(initial_state["decisions"]),
        "fixed_trace": _decode_fixed_trace(initial_state.get("fixed_trace", [])),
        "rounds_completed": int(initial_state["rounds_completed"]),
        "committed_rounds": [int(item) for item in initial_state["committed_rounds"]],
        "stop_reason": initial_state.get("stop_reason", ""),
        "converged": bool(initial_state.get("converged", False)),
    }


def train_prepared(
    prepared: PreparedData,
    config: TrainingConfig,
    *,
    reduce_mode: str,
    run_id: str,
    initial_state: dict[str, Any] | None = None,
    commit_callback: Callable[[dict[str, Any]], None] | None = None,
    stop_after_rounds: int | None = None,
    interrupt_round: int | None = None,
    interrupt_after_chunk: int | None = None,
    collect_chunk_details: bool = True,
    disable_early_stop: bool = False,
) -> TrainingRun:
    if reduce_mode not in {"chunked", "full"}:
        raise ValueError("reduce_mode must be chunked or full")
    engine = prepared.engine
    if initial_state is not None:
        state = _decode_initial_state(initial_state, config)
        params = state["params"].copy()
        initial_params = state["initial_params"].copy()
        losses = list(state["losses"])
        validation = list(state["validation"])
        parameter_trajectory = list(state["parameter_trajectory"])
        decisions = list(state["decisions"])
        fixed_trace = list(state["fixed_trace"])
        committed_rounds = list(state["committed_rounds"])
        started_from_round = state["rounds_completed"] + 1
        if state["stop_reason"] in {"converged", "max_iterations", "completed"}:
            raise ValueError("checkpoint is already complete")
    else:
        params = engine.zeros(prepared.train_features.shape[1])
        initial_params = params.copy()
        losses = []
        validation = []
        parameter_trajectory = []
        decisions = []
        fixed_trace = []
        committed_rounds = []
        started_from_round = 1

    rebuilt_rounds: list[int] = []
    chunk_traces: list[ChunkTrace] = []
    feature_norms = [
        engine.feature_norm(prepared.train_features[np.asarray(chunk, dtype=np.int64), :])
        for chunk in prepared.chunks
    ]
    all_indices = np.arange(prepared.train_features.shape[0], dtype=np.int64)
    effective_lr = engine.effective_learning_rate(config.learning_rate)
    interrupted = False
    discarded_round: int | None = None
    converged = False
    stop_reason = "running"
    rounds_executed: list[int] = []

    for round_number in range(started_from_round, config.max_iterations + 1):
        aborted_in_round = False
        round_gradients: list[np.ndarray] = []
        round_traces: list[ChunkTrace] = []

        if reduce_mode == "chunked":
            for chunk_index, chunk in enumerate(prepared.chunks):
                gradient = engine.gradient(params, prepared.train_features, prepared.train_labels, chunk)
                if collect_chunk_details:
                    trace = _make_chunk_trace(
                        prepared,
                        engine,
                        round_number,
                        chunk_index,
                        chunk,
                        gradient,
                        feature_norms[chunk_index],
                        effective_lr,
                        used_in_reduction=True,
                    )
                    round_traces.append(trace)
                round_gradients.append(gradient)
                if (
                    interrupt_round == round_number
                    and interrupt_after_chunk is not None
                    and chunk_index + 1 >= interrupt_after_chunk
                ):
                    aborted_in_round = True
                    break
            if aborted_in_round:
                interrupted = True
                discarded_round = round_number
                stop_reason = "interrupted_mid_round"
                break
            gradient_sum = engine.reduce_gradients(round_gradients)
        else:
            gradient_sum = engine.gradient(
                params, prepared.train_features, prepared.train_labels, all_indices
            )
            if collect_chunk_details:
                for chunk_index, chunk in enumerate(prepared.chunks):
                    diagnostic_gradient = engine.gradient(
                        params, prepared.train_features, prepared.train_labels, chunk
                    )
                    round_traces.append(
                        _make_chunk_trace(
                            prepared,
                            engine,
                            round_number,
                            chunk_index,
                            chunk,
                            diagnostic_gradient,
                            feature_norms[chunk_index],
                            effective_lr,
                            used_in_reduction=False,
                        )
                    )
            if (
                interrupt_round == round_number
                and interrupt_after_chunk is not None
                and len(round_traces) >= interrupt_after_chunk
            ):
                interrupted = True
                discarded_round = round_number
                stop_reason = "interrupted_mid_round"
                break

        params_before = params.copy()
        params = engine.apply_update(
            params,
            gradient_sum,
            prepared.train_features.shape[0],
            config.regularization_strength,
            config.learning_rate,
            prepared.regularized_mask,
        )
        train_loss = engine.loss(
            params,
            prepared.train_features,
            prepared.train_labels,
            chunks=prepared.chunks if reduce_mode == "chunked" else None,
            reduce_mode=reduce_mode,
            include_regularization=True,
            regularization_strength=config.regularization_strength,
            regularized_mask=prepared.regularized_mask,
        )
        validation_metrics = engine.validation_metrics(
            params,
            prepared.validation_features,
            prepared.validation_labels,
            regularization_strength=config.regularization_strength,
            regularized_mask=prepared.regularized_mask,
        )
        losses.append(float(train_loss))
        validation.append(validation_metrics)
        parameter_trajectory.append(params.copy())
        chunk_traces.extend(round_traces)

        if len(losses) >= 2:
            decision = decide_convergence(
                round_number,
                losses[-2],
                losses[-1],
                config,
                dtype_epsilon=engine.eps,
            )
            decisions.append(decision.to_dict())
            selected_converged = decision.selected_converged
        else:
            selected_converged = False

        if engine.is_fixed:
            fixed_trace.append(
                {
                    "round": round_number,
                    "sample_count": int(prepared.train_features.shape[0]),
                    "regularization_strength": float(config.regularization_strength),
                    "learning_rate": float(config.learning_rate),
                    "gradient_sum": gradient_sum.copy(),
                    "params_before": params_before,
                    "params_after": params.copy(),
                }
            )

        committed_rounds.append(round_number)
        rounds_executed.append(round_number)
        rebuilt_rounds.append(round_number)
        state_for_commit = _state_for_commit(
            prepared,
            reduce_mode,
            params,
            initial_params,
            losses,
            validation,
            parameter_trajectory,
            decisions,
            fixed_trace,
            committed_rounds,
            stop_reason="running",
            converged=False,
        )
        if commit_callback is not None:
            commit_callback(state_for_commit)

        if selected_converged and not disable_early_stop:
            converged = True
            stop_reason = "converged"
            break
        if disable_early_stop and round_number >= config.max_iterations:
            stop_reason = "max_iterations"
            break
        if (
            stop_after_rounds is not None
            and round_number >= stop_after_rounds
            and round_number < config.max_iterations
        ):
            stop_reason = "interrupted_at_round_boundary"
            interrupted = True
            break
        if round_number >= config.max_iterations:
            stop_reason = "max_iterations"
            break

    if stop_reason == "running":
        stop_reason = "max_iterations"
    if not loss_for_final(losses):
        stop_reason = "interrupted_before_first_commit"

    params_float = tuple(float(item) for item in engine.to_float(params))
    run = TrainingRun(
        run_id=run_id,
        precision=engine.name,
        reduce_mode=reduce_mode,
        config=config,
        prepared=prepared,
        params=params,
        params_float=params_float,
        loss_trajectory=losses,
        validation_trajectory=validation,
        parameter_trajectory=parameter_trajectory,
        decisions=decisions,
        chunk_trace=chunk_traces if collect_chunk_details else [],
        rounds=len(losses),
        stop_reason=stop_reason,
        converged=converged,
        fixed_trace=fixed_trace,
        initial_params=initial_params,
        interrupted=interrupted or stop_reason.startswith("interrupted"),
        discarded_round=discarded_round,
        started_from_round=started_from_round,
        committed_rounds=committed_rounds,
        rebuilt_rounds=rebuilt_rounds,
    )
    run.core_digest = run.compute_digest()
    if commit_callback is not None and run.rounds > 0:
        commit_callback(
            _state_for_commit(
                prepared,
                reduce_mode,
                run.params,
                run.initial_params
                if run.initial_params is not None
                else engine.zeros(run.params.shape[0]),
                run.loss_trajectory,
                run.validation_trajectory,
                run.parameter_trajectory,
                run.decisions,
                run.fixed_trace,
                run.committed_rounds,
                stop_reason=run.stop_reason,
                converged=run.converged,
            )
        )
    return run


def loss_for_final(losses: Sequence[float]) -> bool:
    return bool(losses)


def _make_chunk_trace(
    prepared: PreparedData,
    engine: PrecisionEngine,
    round_number: int,
    chunk_index: int,
    chunk: Sequence[int],
    gradient: np.ndarray,
    feature_norm: float,
    effective_lr: float,
    *,
    used_in_reduction: bool,
) -> ChunkTrace:
    global_indices = tuple(prepared.chunk_global_indices[chunk_index])
    return ChunkTrace(
        round_number=round_number,
        chunk_index=chunk_index,
        local_indices=tuple(int(item) for item in chunk),
        global_indices=global_indices,
        row_ids=tuple(prepared.train_row_ids[int(item)] for item in chunk),
        gradient=gradient.copy(),
        feature_l2=float(feature_norm),
        learning_rate=float(effective_lr),
        used_in_reduction=used_in_reduction,
    )


def _state_for_commit(
    prepared: PreparedData,
    reduce_mode: str,
    params: np.ndarray,
    initial_params: np.ndarray,
    losses: Sequence[float],
    validation: Sequence[dict[str, Any]],
    parameter_trajectory: Sequence[np.ndarray],
    decisions: Sequence[dict[str, Any]],
    fixed_trace: Sequence[dict[str, Any]],
    committed_rounds: Sequence[int],
    *,
    stop_reason: str,
    converged: bool,
) -> dict[str, Any]:
    return {
        "precision": prepared.engine.name,
        "reduce_mode": reduce_mode,
        "chunk_plan_sha256": str(prepared.chunk_plan_fingerprint["sha256"]),
        "train_index_sha256": prepared.sample_fingerprint["train_sha256"],
        "rounds_completed": len(losses),
        "committed_rounds": list(committed_rounds),
        "params": encode_array(params),
        "initial_params": encode_array(initial_params),
        "loss_trajectory_hex": [float_hex(item) for item in losses],
        "validation_trajectory": list(validation),
        "parameter_trajectory": [encode_array(item) for item in parameter_trajectory],
        "decisions": list(decisions),
        "fixed_trace": _encode_fixed_trace(fixed_trace),
        "stop_reason": stop_reason,
        "converged": converged,
    }


def collect_recompute_digest(
    prepared: PreparedData,
    config: TrainingConfig,
    *,
    reduce_mode: str,
) -> str:
    run = train_prepared(
        prepared,
        config,
        reduce_mode=reduce_mode,
        run_id="recompute",
        collect_chunk_details=False,
    )
    return run.compute_digest()
