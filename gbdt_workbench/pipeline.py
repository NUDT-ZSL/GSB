from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .binning import binning_cache_key, build_binning
from .config import ConfigValidationError, LoadedConfig, load_config
from .data import Dataset, generate_dataset
from .diagnostics import trace_error_propagation
from .ensemble import (
    compute_contributions,
    contribution_cache_key,
    contributions_as_lists,
    ensemble_cache_key,
    score_ensemble,
)
from .errors import (
    E_DATA_INVALID,
    E_INTERNAL,
    E_TRAIN_TREE,
    ConfigMismatchError,
    Failure,
    TreeTrainingError,
    WorkbenchError,
)
from .precision import PrecisionContext
from .snapshots import SNAPSHOT_VERSION, SnapshotStore
from .tree import (
    residual_fingerprint,
    train_tree,
    tree_cache_key,
    tree_structure_summary,
)
from .utils import atomic_write_json, digest_json


REPORT_SCHEMA_VERSION = 1
DIAGNOSTIC_VERSION = 1


@dataclass
class RunResult:
    exit_code: int
    status: str
    diagnostic_report: dict[str, Any]
    run_report: dict[str, Any]
    diagnostic_path: Path
    run_report_path: Path


class WorkbenchEngine:
    def __init__(
        self,
        config_path: str | Path,
        output_dir: str | Path,
        *,
        force_rebuild: bool = False,
        strict_restore: bool = False,
    ) -> None:
        self.config_path = Path(config_path)
        self.output_dir = Path(output_dir)
        self.force_rebuild = bool(force_rebuild)
        self.strict_restore = bool(strict_restore)
        self._started = 0.0

    def run(self) -> RunResult:
        self._started = time.perf_counter()
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkbenchError(
                "E_OUTPUT_INVALID",
                "output",
                f"output directory is not writable: {exc}",
            ) from exc
        try:
            loaded = load_config(self.config_path)
        except ConfigValidationError as exc:
            return self._write_early_failure(exc.failures, exit_code=2, status="failed")
        except WorkbenchError as exc:
            return self._write_early_failure([exc.failure], exit_code=2, status="failed")

        try:
            return self._run_loaded(loaded)
        except ConfigMismatchError as exc:
            return self._write_early_failure(
                [exc.failure],
                exit_code=3,
                status="failed",
                loaded=loaded,
            )
        except WorkbenchError as exc:
            exit_code = 2 if exc.failure.code == E_DATA_INVALID else 4
            return self._write_early_failure(
                [exc.failure],
                exit_code=exit_code,
                status="failed",
                loaded=loaded,
            )
        except Exception as exc:  # convert unexpected failures to a deterministic business result
            failure = Failure(
                code=E_INTERNAL,
                stage="pipeline",
                message=f"{type(exc).__name__}: {exc}",
            )
            return self._write_early_failure(
                [failure],
                exit_code=4,
                status="failed",
                loaded=loaded,
            )

    def _run_loaded(self, loaded: LoadedConfig) -> RunResult:
        config = loaded.config
        dataset = generate_dataset(loaded)
        precision = PrecisionContext(config.precision)
        initial_state = precision.initial_binary_state(dataset.y)
        base_score = float(initial_state["base_score"])
        reference_base_score = float(initial_state["reference_base_score"])
        residual = np.asarray(initial_state["residual"], dtype=precision.dtype)
        reference_residual = np.asarray(
            initial_state["reference_residual"], dtype=np.float64
        )
        residual_hash = residual_fingerprint(residual, reference_residual, precision)
        initial_state["residual_fingerprint"] = residual_hash

        store = SnapshotStore(
            self.output_dir,
            loaded.config_fingerprint,
            strict_config_match=self.strict_restore,
        )
        initial_commit_count = int(store.state.get("commit_count", 0))
        store.state["stage_keys"] = {}
        store.state["tree_keys"] = {}

        failures: list[Failure] = []
        actions: dict[str, Any] = {"binning": None, "trees": [], "ensemble": None, "contributions": None, "diagnostics": None}
        invalidations: dict[str, Any] = {"stages": [], "trees": []}

        binning_key = binning_cache_key(dataset, config.n_bins)
        binning_invalidation = store.stage_invalidation("binning", binning_key)
        invalidations["stages"].append(binning_invalidation)
        binning_payload: dict[str, Any] | None = None
        if not self.force_rebuild:
            binning_payload = store.load("binning", binning_key)
        if binning_payload is not None:
            if (
                binning_payload.get("cache_key") != binning_key
                or binning_payload.get("data_fingerprint") != dataset.data_fingerprint
                or not binning_payload.get("fingerprint")
            ):
                raise ConfigMismatchError(
                    "restored binning fingerprint does not match current data",
                    details={
                        "reason": "binning_fingerprint_mismatch",
                        "expected_data_fingerprint": dataset.data_fingerprint,
                        "actual_data_fingerprint": binning_payload.get(
                            "data_fingerprint"
                        ),
                    },
                )
            actions["binning"] = "reused"
        else:
            binning_payload = build_binning(dataset, config.n_bins)
            if binning_payload.get("cache_key") != binning_key:
                raise WorkbenchError(
                    E_INTERNAL,
                    "binning",
                    "binning cache key derivation is inconsistent",
                )
            store.save("binning", binning_key, binning_payload)
            actions["binning"] = "computed"
        store.record_stage_key("binning", binning_key)
        binning_payload["cache_key"] = binning_key
        sample_bins = np.asarray(binning_payload["sample_bins"], dtype=np.int32)
        binning_payload["sample_bins"] = sample_bins

        tree_keys = {
            tree_index: tree_cache_key(
                config,
                tree_index,
                str(binning_payload["fingerprint"]),
                residual_hash,
                precision,
            )
            for tree_index in range(config.n_trees)
        }
        trees_by_index: dict[int, dict[str, Any]] = {}
        missing_indices: list[int] = []
        for tree_index in range(config.n_trees):
            tree_key = tree_keys[tree_index]
            tree_invalidation = store.tree_invalidation(tree_index, tree_key)
            invalidations["trees"].append(tree_invalidation)
            cached_tree = None
            if not self.force_rebuild:
                cached_tree = store.load("trees", tree_key)
            if cached_tree is not None:
                if (
                    int(cached_tree.get("tree_index", -1)) != tree_index
                    or cached_tree.get("cache_key") != tree_key
                ):
                    raise ConfigMismatchError(
                        "restored tree identity does not match its cache key",
                        details={
                            "reason": "tree_identity_mismatch",
                            "tree_index": tree_index,
                            "key": tree_key,
                        },
                    )
                trees_by_index[tree_index] = cached_tree
                store.record_tree_key(tree_index, tree_key)
                actions["trees"].append(
                    {"tree_index": tree_index, "cache_key": tree_key, "action": "reused"}
                )
            else:
                missing_indices.append(tree_index)

        newly_trained = 0

        def save_tree_result(tree_index: int, tree_payload: dict[str, Any]) -> None:
            nonlocal newly_trained
            trees_by_index[tree_index] = tree_payload
            store.save("trees", tree_keys[tree_index], tree_payload)
            store.record_tree_key(tree_index, tree_keys[tree_index])
            actions["trees"].append(
                {
                    "tree_index": tree_index,
                    "cache_key": tree_keys[tree_index],
                    "action": "computed",
                }
            )
            newly_trained += 1
            if newly_trained % config.snapshot_interval == 0:
                store.commit()

        if config.n_jobs == 1:
            for tree_index in missing_indices:
                try:
                    tree_payload = train_tree(
                        tree_index,
                        dataset,
                        sample_bins,
                        str(binning_payload["fingerprint"]),
                        residual,
                        reference_residual,
                        precision,
                        config,
                        residual_hash,
                    )
                    save_tree_result(tree_index, tree_payload)
                except TreeTrainingError as exc:
                    failures.append(exc.as_failure())
                except Exception as exc:
                    failures.append(
                        Failure(
                            code=E_TRAIN_TREE,
                            stage="train_tree",
                            message=f"{type(exc).__name__}: {exc}",
                            tree_index=tree_index,
                        )
                    )
        else:
            with ThreadPoolExecutor(
                max_workers=config.n_jobs,
                thread_name_prefix="gbdt-tree",
            ) as executor:
                futures = {
                    executor.submit(
                        train_tree,
                        tree_index,
                        dataset,
                        sample_bins,
                        str(binning_payload["fingerprint"]),
                        residual,
                        reference_residual,
                        precision,
                        config,
                        residual_hash,
                    ): tree_index
                    for tree_index in missing_indices
                }
                for future in as_completed(futures):
                    tree_index = futures[future]
                    try:
                        tree_payload = future.result()
                        save_tree_result(tree_index, tree_payload)
                    except TreeTrainingError as exc:
                        failures.append(exc.as_failure())
                    except Exception as exc:
                        failures.append(
                            Failure(
                                code=E_TRAIN_TREE,
                                stage="train_tree",
                                message=f"{type(exc).__name__}: {exc}",
                                tree_index=tree_index,
                            )
                        )

        trees = [trees_by_index[index] for index in sorted(trees_by_index)]
        actions["trees"].sort(key=lambda item: int(item["tree_index"]))
        failures.sort(key=lambda item: (item.tree_index if item.tree_index is not None else -1, item.stage, item.code))

        ensemble_key = ensemble_cache_key(
            trees,
            base_score,
            config.learning_rate,
            precision,
        )
        ensemble_invalidation = store.stage_invalidation("ensemble", ensemble_key)
        invalidations["stages"].append(ensemble_invalidation)
        loaded_ensemble = None
        if not self.force_rebuild:
            loaded_ensemble = store.load("ensemble", ensemble_key)
        if loaded_ensemble is not None:
            if loaded_ensemble.get("cache_key") != ensemble_key:
                raise ConfigMismatchError(
                    "restored ensemble identity does not match its cache key",
                    details={"reason": "ensemble_identity_mismatch", "key": ensemble_key},
                )
            ensemble_payload = loaded_ensemble
            ensemble_payload["raw_scores"] = np.asarray(
                ensemble_payload["raw_scores"], dtype=precision.dtype
            )
            ensemble_payload["probabilities"] = np.asarray(
                ensemble_payload["probabilities"], dtype=precision.dtype
            )
            actions["ensemble"] = "reused"
        else:
            ensemble_payload = score_ensemble(
                trees,
                sample_bins,
                base_score,
                config.learning_rate,
                precision,
            )
            store.save("ensemble", ensemble_key, ensemble_payload)
            actions["ensemble"] = "computed"
        store.record_stage_key("ensemble", ensemble_key)

        contribution_key = contribution_cache_key(
            trees,
            config.learning_rate,
            precision,
            dataset.feature_names,
        )
        contribution_invalidation = store.stage_invalidation(
            "contributions", contribution_key
        )
        invalidations["stages"].append(contribution_invalidation)
        loaded_contributions = None
        if not self.force_rebuild:
            loaded_contributions = store.load("contributions", contribution_key)
        if loaded_contributions is not None:
            if loaded_contributions.get("cache_key") != contribution_key:
                raise ConfigMismatchError(
                    "restored contribution identity does not match its cache key",
                    details={
                        "reason": "contribution_identity_mismatch",
                        "key": contribution_key,
                    },
                )
            contributions_payload = loaded_contributions
            contributions_payload["tree_contributions"] = np.asarray(
                contributions_payload["tree_contributions"], dtype=precision.dtype
            )
            contributions_payload["feature_contributions"] = np.asarray(
                contributions_payload["feature_contributions"], dtype=precision.dtype
            )
            actions["contributions"] = "reused"
        else:
            contributions_payload = compute_contributions(
                trees,
                sample_bins,
                config.learning_rate,
                precision,
                dataset.feature_names,
            )
            store.save("contributions", contribution_key, contributions_payload)
            actions["contributions"] = "computed"
        store.record_stage_key("contributions", contribution_key)

        trace = trace_error_propagation(
            trees,
            sample_bins,
            ensemble_payload,
            contributions_payload,
            precision,
            reference_base_score,
            config.learning_rate,
            config.tolerance,
            config.decision_threshold,
        )
        diagnostic_key = digest_json(
            {
                "stage": "diagnostics",
                "version": DIAGNOSTIC_VERSION,
                "ensemble_fingerprint": ensemble_payload["fingerprint"],
                "contribution_fingerprint": contributions_payload["fingerprint"],
                "tolerance": config.tolerance,
                "decision_threshold": config.decision_threshold,
                "base_precision": precision.tier,
                "reference_precision": precision.reference_tier,
            }
        )
        diagnostics_invalidation = store.stage_invalidation(
            "diagnostics", diagnostic_key
        )
        invalidations["stages"].append(diagnostics_invalidation)
        store.save("diagnostics", diagnostic_key, trace)
        store.record_stage_key("diagnostics", diagnostic_key)
        actions["diagnostics"] = "computed"

        tree_failures = [failure for failure in failures if failure.code == E_TRAIN_TREE]
        if tree_failures and len(trees) == 0:
            status = "failed"
        elif tree_failures:
            status = "partial_failure"
        else:
            status = "ok"
        exit_code = 1 if tree_failures else 0

        diagnostic_report = self._build_diagnostic_report(
            loaded,
            dataset,
            binning_payload,
            trees,
            ensemble_payload,
            contributions_payload,
            trace,
            failures,
            status,
            initial_state,
        )
        baseline = self._baseline_report(dataset, config)
        runtime = {
            "configured_n_jobs": config.n_jobs,
            "available_cpus": os.cpu_count(),
            "elapsed_ms": round((time.perf_counter() - self._started) * 1000.0, 3),
        }
        audit = {
            "stage_actions": actions,
            "invalidations": invalidations,
            "reused": {
                "stages": [
                    stage
                    for stage in ("binning", "ensemble", "contributions")
                    if actions.get(stage) == "reused"
                ],
                "tree_indices": [
                    int(item["tree_index"])
                    for item in actions["trees"]
                    if item["action"] == "reused"
                ],
            },
            "computed": {
                "stages": [
                    stage
                    for stage in ("binning", "ensemble", "contributions", "diagnostics")
                    if actions.get(stage) in {"computed", "recomputed"}
                ],
                "tree_indices": [
                    int(item["tree_index"])
                    for item in actions["trees"]
                    if item["action"] == "computed"
                ],
            },
            "recompute_set": {
                "stages": [
                    item["stage"]
                    for item in invalidations["stages"]
                    if item["invalidated"]
                ],
                "tree_indices": [
                    int(item["tree_index"])
                    for item in invalidations["trees"]
                    if item["invalidated"]
                ],
            },
            "snapshot": {
                **store.audit(config.snapshot_interval),
                "commits_this_run": int(store.state.get("commit_count", 0))
                - initial_commit_count,
            },
            "threads": runtime,
        }

        store.state["config_fingerprint"] = loaded.config_fingerprint
        store.commit()
        audit["snapshot"]["commit_count"] = int(store.state.get("commit_count", 0))
        audit["snapshot"]["commits_this_run"] = int(
            store.state.get("commit_count", 0)
        ) - initial_commit_count

        run_report = {
            **diagnostic_report,
            "operational_audit": audit,
            "runtime": runtime,
            "baseline": baseline,
        }
        return self._write_reports(
            exit_code=exit_code,
            status=status,
            diagnostic_report=diagnostic_report,
            run_report=run_report,
        )

    def _build_diagnostic_report(
        self,
        loaded: LoadedConfig,
        dataset: Dataset,
        binning: dict[str, Any],
        trees: list[dict[str, Any]],
        ensemble: dict[str, Any],
        contributions: dict[str, Any],
        trace: dict[str, Any],
        failures: list[Failure],
        status: str,
        initial_state: dict[str, Any],
    ) -> dict[str, Any]:
        config = loaded.config
        report: dict[str, Any] = {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "diagnostic_version": DIAGNOSTIC_VERSION,
            "status": status,
            "snapshot_version": SNAPSHOT_VERSION,
            "config_fingerprint": loaded.config_fingerprint,
            "config": config.normalized(),
            "decision_threshold": config.decision_threshold,
            "data": dataset.public_summary(),
            "initial_state": {
                "base_score": initial_state["base_score"],
                "base_probability": initial_state["base_probability"],
                "reference_base_score": initial_state["reference_base_score"],
                "reference_base_probability": initial_state[
                    "reference_base_probability"
                ],
                "residual_fingerprint": initial_state["residual_fingerprint"],
            },
            "binning": {
                "fingerprint": binning["fingerprint"],
                "cache_key": binning["cache_key"],
                "data_fingerprint": binning["data_fingerprint"],
                "n_bins": binning["n_bins"],
                "tie_policy": binning["tie_policy"],
                "feature_names": binning["feature_names"],
                "feature_kinds": binning["feature_kinds"],
                "features": binning["features"],
                "sample_bins": np.asarray(binning["sample_bins"], dtype=np.int32),
                "shape": binning["shape"],
            },
            "trees": [tree_structure_summary(tree) for tree in trees],
            "ensemble": {
                "fingerprint": ensemble["fingerprint"],
                "cache_key": ensemble["cache_key"],
                "tree_indices": ensemble["tree_indices"],
                "tree_fingerprints": ensemble["tree_fingerprints"],
                "base_score": ensemble["base_score"],
                "learning_rate": ensemble["learning_rate"],
                "precision": ensemble["precision"],
                "raw_scores": np.asarray(ensemble["raw_scores"]),
                "probabilities": np.asarray(ensemble["probabilities"]),
            },
            "contributions": {
                "fingerprint": contributions["fingerprint"],
                "cache_key": contributions["cache_key"],
                **contributions_as_lists(contributions),
            },
            "error_attribution": trace["error_attribution"],
            "threshold_sensitive_samples": trace["threshold_sensitive_samples"],
            "failures": [failure.to_dict() for failure in failures],
        }
        report["equivalence_digest"] = digest_json(report)
        return report

    def _baseline_report(self, dataset: Dataset, config: Any) -> dict[str, Any]:
        if not config.include_baseline:
            return {"enabled": False}
        try:
            from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
            from sklearn.metrics import accuracy_score, log_loss, roc_auc_score

            results: dict[str, Any] = {"enabled": True, "note": "scikit-learn reference only"}
            classifiers = {
                "random_forest": RandomForestClassifier(
                    n_estimators=max(1, min(40, config.n_trees * 2)),
                    max_depth=config.max_depth,
                    random_state=config.seed,
                    n_jobs=1,
                ),
                "hist_gradient_boosting": HistGradientBoostingClassifier(
                    max_iter=max(1, min(60, config.n_trees)),
                    max_depth=config.max_depth,
                    learning_rate=config.learning_rate,
                    random_state=config.seed,
                ),
            }
            for name, classifier in classifiers.items():
                classifier.fit(dataset.X, dataset.y)
                probabilities = classifier.predict_proba(dataset.X)[:, 1]
                predictions = (probabilities >= config.decision_threshold).astype(int)
                metrics: dict[str, Any] = {
                    "accuracy": float(accuracy_score(dataset.y, predictions)),
                    "log_loss": float(log_loss(dataset.y, probabilities, labels=[0, 1])),
                }
                if len(np.unique(dataset.y)) > 1:
                    metrics["roc_auc"] = float(roc_auc_score(dataset.y, probabilities))
                else:
                    metrics["roc_auc"] = None
                results[name] = metrics
            return results
        except Exception as exc:
            return {
                "enabled": True,
                "error": f"{type(exc).__name__}: {exc}",
                "note": "baseline is optional and never feeds the diagnostic model",
            }

    def _write_early_failure(
        self,
        failures: list[Failure],
        *,
        exit_code: int,
        status: str,
        loaded: LoadedConfig | None = None,
    ) -> RunResult:
        config_fingerprint = loaded.config_fingerprint if loaded is not None else None
        diagnostic = {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "diagnostic_version": DIAGNOSTIC_VERSION,
            "status": status,
            "snapshot_version": SNAPSHOT_VERSION,
            "config_fingerprint": config_fingerprint,
            "config": loaded.config.normalized() if loaded is not None else None,
            "failures": [failure.to_dict() for failure in failures],
            "failure_categories": sorted({failure.code for failure in failures}),
            "trees": [],
            "ensemble": None,
            "contributions": None,
            "error_attribution": None,
            "threshold_sensitive_samples": [],
        }
        diagnostic["equivalence_digest"] = digest_json(diagnostic)
        run_report = {
            **diagnostic,
            "runtime": {
                "elapsed_ms": round((time.perf_counter() - self._started) * 1000.0, 3),
                "available_cpus": os.cpu_count(),
            },
            "operational_audit": {
                "reused": {"stages": [], "tree_indices": []},
                "computed": {"stages": [], "tree_indices": []},
                "snapshot": {"snapshot_version": SNAPSHOT_VERSION},
            },
        }
        return self._write_reports(
            exit_code=exit_code,
            status=status,
            diagnostic_report=diagnostic,
            run_report=run_report,
        )

    def _write_reports(
        self,
        *,
        exit_code: int,
        status: str,
        diagnostic_report: dict[str, Any],
        run_report: dict[str, Any],
    ) -> RunResult:
        diagnostic_path = self.output_dir / "diagnostic_report.json"
        run_report_path = self.output_dir / "run_report.json"
        atomic_write_json(diagnostic_path, diagnostic_report)
        atomic_write_json(run_report_path, run_report)
        return RunResult(
            exit_code=exit_code,
            status=status,
            diagnostic_report=diagnostic_report,
            run_report=run_report,
            diagnostic_path=diagnostic_path,
            run_report_path=run_report_path,
        )
