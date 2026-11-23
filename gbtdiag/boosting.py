"""Round-by-round gradient boosting training and inference core."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .binning import BinningResult, build_binnings
from .config import TrainConfig
from .data import Dataset
from .errors import ErrorCode
from .injection import InjectionController, InjectionSpec
from .precision import quantize
from .records import (
    attach_degradation,
    stage_record,
    unattributed_record,
)
from .tree import Tree, fit_tree, skipped_tree


def squared_error_derivatives(
    y: np.ndarray, prediction: np.ndarray
) -> Tuple[np.ndarray, np.ndarray]:
    """Gradients and Hessians of ``0.5 * (y - p)^2``."""
    gradient = prediction - y
    hessian = np.ones_like(prediction, dtype=np.float64)
    return gradient, hessian


def rmse(y: np.ndarray, prediction: np.ndarray) -> float:
    return float(np.sqrt(np.mean((y - prediction) ** 2, dtype=np.float64)))


@dataclass
class RoundDetail:
    round_index: int
    row_sample: List[int]
    col_sample: List[int]
    learning_rate: float
    loss_before: float
    loss_after: float
    tree: Tree
    checkpoints: Dict[str, np.ndarray]
    records: List[Dict[str, Any]]
    tree_contribution: np.ndarray
    prediction_after: np.ndarray

    def to_dict(self) -> Dict[str, Any]:
        return {
            "round_index": self.round_index,
            "row_sample": list(self.row_sample),
            "col_sample": list(self.col_sample),
            "learning_rate": float(self.learning_rate),
            "loss_before": float(self.loss_before),
            "loss_after": float(self.loss_after),
            "tree": self.tree.to_dict(),
            "checkpoints": dict(self.checkpoints),
            "records": list(self.records),
            "tree_contribution": self.tree_contribution,
            "prediction_after": self.prediction_after,
        }


@dataclass
class BoostingModel:
    config: TrainConfig
    binnings: BinningResult
    trees: List[Tree]
    base_prediction: np.ndarray
    final_prediction: np.ndarray
    round_details: List[RoundDetail]
    records: List[Dict[str, Any]]
    injection_specs: List[InjectionSpec]
    dataset: Dataset

    def predict(self, X: np.ndarray) -> np.ndarray:
        return predict_with_model(self, X)

    def to_report(self) -> Dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "base_prediction": self.base_prediction,
            "final_prediction": self.final_prediction,
            "records": self.records,
            "rounds": [detail.to_dict() for detail in self.round_details],
            "binning": self.binnings.to_dict(),
            "injections": [spec.to_dict() for spec in self.injection_specs],
        }


def _sample_sequences(
    config: TrainConfig, n_rows: int, n_features: int
) -> Tuple[List[np.ndarray], List[np.ndarray]]:
    """Materialise every row/column sampling sequence up front.

    The sequences are drawn independently per round from dedicated,
    seed-derived RNG streams and stored in the round details, making the
    sampling order explicitly reproducible rather than implicit.
    """
    row_rng = config.rng("row_sample")
    col_rng = config.rng("col_sample")
    row_count = max(1, int(round(config.subsample * n_rows)))
    col_count = max(1, int(round(config.colsample * n_features)))
    row_sequences: List[np.ndarray] = []
    col_sequences: List[np.ndarray] = []
    for _ in range(config.n_trees):
        row_sequences.append(
            np.sort(
                row_rng.choice(n_rows, size=row_count, replace=False)
            ).astype(np.int64)
        )
        col_sequences.append(
            np.sort(
                col_rng.choice(
                    n_features, size=col_count, replace=False
                )
            ).astype(np.int64)
        )
    return row_sequences, col_sequences


def _guard_stage(
    stage: str,
    round_index: int,
    callback,
    *args: Any,
    sample_indices: Optional[Sequence[int]] = None,
    **kwargs: Any,
) -> Tuple[Any, List[Dict[str, Any]]]:
    """Run a stage, converting unexpected exceptions to unattributed records."""
    try:
        return callback(*args, **kwargs), []
    except Exception as exc:  # noqa: BLE001 - boundary that must not swallow
        record = unattributed_record(
            f"{stage}#round{round_index}",
            exc,
            sample_indices=(
                list(sample_indices)
                if sample_indices is not None and len(sample_indices)
                else None
            ),
        )
        return None, [record]


def train_boosting(
    dataset: Dataset,
    config: TrainConfig,
    *,
    injection_specs: Optional[Sequence[InjectionSpec]] = None,
) -> BoostingModel:
    """Train the ensemble and return fully instrumented round details."""
    config.validate_basic()
    specs = list(injection_specs or [])
    controller = InjectionController(specs, config, dataset.n_rows)
    records: List[Dict[str, Any]] = []

    # Stage 1: binning (with binning-stage injections applied to a copy).
    categorical_features = [
        j for j, flag in enumerate(dataset.categorical_mask) if flag
    ]
    X_for_binning = controller.apply_binning_injections(
        dataset.X, categorical_features
    )
    binnings, binning_guards = _guard_stage(
        "binning",
        -1,
        build_binnings,
        X_for_binning,
        dataset.categorical_mask,
        dataset.category_cardinalities,
        config,
    )
    records.extend(binning_guards)
    if binnings is None:
        raise RuntimeError("binning stage failed without attribution")
    records.extend(binnings.records)

    excluded = set(binnings.excluded_rows)
    row_sequences, col_sequences = _sample_sequences(
        config, dataset.n_rows, dataset.n_features
    )

    base_prediction = np.full(
        dataset.n_rows, float(config.base_score), dtype=np.float64
    )
    prediction = base_prediction.copy()
    trees: List[Tree] = []
    round_details: List[RoundDetail] = []

    for round_index in range(config.n_trees):
        round_records: List[Dict[str, Any]] = []
        row_sample = np.asarray(
            [i for i in row_sequences[round_index] if i not in excluded],
            dtype=np.int64,
        )
        col_sample = np.asarray(col_sequences[round_index], dtype=np.int64)

        # Stage 2: gain-stage injections on the working feature matrix copy.
        numeric_features = [
            j for j, flag in enumerate(dataset.categorical_mask) if not flag
        ]
        raw_fault = controller.raw_fault_active("gain", round_index)
        X_round = controller.apply_gain_injections(
            X_for_binning, numeric_features, round_index
        )

        gradient, hessian = squared_error_derivatives(
            dataset.y, prediction
        )
        gradient_q = quantize(gradient, config)
        hessian_q = quantize(hessian, config)

        # Stage 3: leaf-stage injections mutate the Hessian vector in memory.
        hessian_work = controller.apply_leaf_injections(
            hessian_q, round_index
        )

        tree, fit_guards = _guard_stage(
            "gain",
            round_index,
            _maybe_raw_fault_fit
            if raw_fault is not None
            else fit_tree,
            round_index,
            X_round,
            gradient_q,
            hessian_work,
            row_sample,
            col_sample,
            binnings.feature_binnings,
            config,
            detect_duplicate_threshold=controller.duplicate_active(round_index),
            sample_indices=row_sample,
        )
        original_records = list(tree.records) if tree is not None else []
        if tree is None:
            tree = skipped_tree(
                round_index,
                row_sample,
                col_sample,
                ErrorCode.UNATTRIBUTED.value,
            )
            if fit_guards:
                tree.records = list(fit_guards)

        gain_codes = {
            record["code"]
            for record in original_records
            if record["stage"] == "gain"
        }
        if ErrorCode.DUPLICATE_THRESHOLD.value in gain_codes:
            # The duplicate-threshold degradation replaces the whole tree
            # with a zero-contribution placeholder for this round.
            tree = skipped_tree(
                round_index,
                row_sample,
                col_sample,
                ErrorCode.DUPLICATE_THRESHOLD.value,
            )
            tree.records = original_records
        round_records.extend(tree.records)

        unscaled = tree.predict_rows(X_round)

        # Stage 4: prediction stage and learning-rate injection.
        lr, lr_specs = controller.effective_learning_rate(round_index)
        effective_lr = lr
        if lr_specs:
            record = stage_record(
                "prediction",
                ErrorCode.NEGATIVE_LEARNING_RATE,
                f"round {round_index} received negative learning rate {lr:g}",
                sample_indices=[int(i) for i in row_sample],
                details={"injected_value": float(lr)},
            )
            attach_degradation(
                record,
                category="zero_lr_hold",
                trigger_code=ErrorCode.NEGATIVE_LEARNING_RATE,
                action="clamp learning rate to zero and hold the previous prediction",
            )
            round_records.append(record)
            effective_lr = 0.0

        contribution = quantize(unscaled * effective_lr, config)
        # Quarantined rows never receive tree contributions.
        contribution[np.asarray(sorted(excluded), dtype=np.int64)] = 0.0
        new_prediction = quantize(prediction + contribution, config)

        checkpoints = {
            "quantization": np.concatenate([gradient_q, hessian_q]),
            "gain": np.asarray(
                [
                    _root_gain(tree),
                    float(tree.root.feature_index)
                    if tree.root.feature_index is not None
                    else -1.0,
                    float(tree.root.threshold)
                    if tree.root.threshold is not None
                    else -1.0,
                ],
                dtype=np.float64,
            ),
            "leaf_division": unscaled,
            "lr_scale": contribution,
            "accumulation": new_prediction,
        }

        detail = RoundDetail(
            round_index=round_index,
            row_sample=[int(i) for i in row_sample],
            col_sample=[int(i) for i in col_sample],
            learning_rate=float(effective_lr),
            loss_before=rmse(dataset.y, prediction),
            loss_after=rmse(dataset.y, new_prediction),
            tree=tree,
            checkpoints=checkpoints,
            records=round_records,
            tree_contribution=contribution,
            prediction_after=new_prediction,
        )
        round_details.append(detail)
        records.extend(round_records)
        prediction = new_prediction
        trees.append(tree)

    return BoostingModel(
        config=config,
        binnings=binnings,
        trees=trees,
        base_prediction=base_prediction,
        final_prediction=prediction,
        round_details=round_details,
        records=records,
        injection_specs=specs,
        dataset=dataset,
    )


def _root_gain(tree: Tree) -> float:
    decision = tree.root.split_decision
    if decision is None:
        return 0.0
    return float(decision.best_gain)


def _maybe_raw_fault_fit(*args: Any, **kwargs: Any) -> Tree:
    """Stage callback used by the ``raw_fault`` injection.

    It raises an exception that no stage-level mapping understands, letting
    the :func:`_guard_stage` boundary exercise the unattributed path.
    """
    raise RuntimeError("injected raw fault with no stage attribution")


def predict_with_model(model: BoostingModel, X: np.ndarray) -> np.ndarray:
    """Inference: base prediction plus each stored tree contribution."""
    prediction = np.full(X.shape[0], float(model.config.base_score))
    for detail in model.round_details:
        unscaled = detail.tree.predict_rows(X)
        prediction = prediction + unscaled * detail.learning_rate
    return prediction


def recompute_prediction(model: BoostingModel) -> np.ndarray:
    """Independently rebuild predictions from base score + tree details.

    The replay mirrors the documented training arithmetic exactly: each
    contribution uses the configured precision, quarantined rows stay at
    the base prediction and every additive update applies the same
    quantisation path, giving a byte-identical reconstruction.
    """
    prediction = model.base_prediction.copy()
    excluded = np.asarray(
        sorted(model.binnings.excluded_rows), dtype=np.int64
    )
    for detail in model.round_details:
        unscaled = detail.tree.predict_rows(model.dataset.X)
        contribution = quantize(
            unscaled * detail.learning_rate, model.config
        )
        if excluded.size:
            contribution[excluded] = 0.0
        prediction = quantize(prediction + contribution, model.config)
    return prediction
