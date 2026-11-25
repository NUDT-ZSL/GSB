"""Deterministic pipeline orchestration, incremental cache and replay."""

from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any, Callable

from .cache import CacheStore, derive_cache_key
from .config import validate_config
from .cross import expand_cross_features, standardize_cross_features
from .errors import DiagnosticError, stale_result
from .forecast import build_error_spectrum, predict_quantiles, predict_window
from .generator import read_jsonl
from .hashing import canonical_json, sha256_json
from .preprocessing import build_window_specs, preprocess_window, select_window_records, validate_records
from .reduction import reduce_features
from .state import STAGES, StateManager


NODE_ORDER = [
    "preprocess",
    "cross_expand",
    "standardize",
    "reduce",
    "predict",
    "error",
]


class DiagnosticEngine:
    def __init__(
        self,
        config: dict[str, Any],
        records: list[dict[str, Any]] | None = None,
        profile: str | None = None,
        workdir: str | Path = ".",
        crash_after_commits: int | None = None,
    ):
        self.config = config
        self.workdir = Path(workdir)
        self.profile_name = profile or config.get("precision", {}).get("default", "float64")
        self.precision = validate_config(config, self.profile_name)
        self.records = records or []

        cache_cfg = config.get("cache", {}).get("directory", ".diagnostic/cache")
        state_cfg = config.get("state", {}).get("directory", ".diagnostic/state")
        sqlite_cfg = config.get("sqlite", {}).get("path", ".diagnostic/index.sqlite3")
        cache_path = Path(cache_cfg)
        state_path = Path(state_cfg)
        sqlite_path = Path(sqlite_cfg)
        if not cache_path.is_absolute():
            cache_path = self.workdir / cache_path
        if not state_path.is_absolute():
            state_path = self.workdir / state_path
        if not sqlite_path.is_absolute():
            sqlite_path = self.workdir / sqlite_path
        self.cache = CacheStore(cache_path, sqlite_path)
        self.state = StateManager(state_path)
        self.crash_after_commits = crash_after_commits
        self.commit_count = 0
        self.actions: list[dict[str, Any]] = []

    @property
    def config_hash(self) -> str:
        return sha256_json(self.config)

    def recover(self) -> dict[str, Any]:
        removed_temp = self.state.cleanup_temporary()
        referenced = self.state.committed_cache_keys()
        gc_report = self.cache.gc(referenced)
        return {"removed_temp": removed_temp, "cache_gc": gc_report, "referenced_keys": len(referenced)}

    def _window_range(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "window_id": spec["window_id"],
            "start_ms": spec["start_ms"],
            "end_ms": spec["end_ms"],
            "grid_rows": spec["grid_rows"],
        }

    def _base_identity(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {
            "config_version": self.config.get("config_version"),
            "precision": self.precision.config_view(),
            "window_range": self._window_range(spec),
        }

    def _record_action(self, action: str, node: str, spec: dict[str, Any], key: str, content_hash: str) -> None:
        item = {
            "action": action,
            "node": node,
            "window_id": spec["window_id"],
            "window_index": spec["window_index"],
            "cache_key": key,
            "content_hash": content_hash,
            "profile": self.profile_name,
        }
        self.actions.append(item)

    def _get_or_compute(
        self,
        node: str,
        key: str,
        producer: Callable[[], dict[str, Any]],
        spec: dict[str, Any],
    ) -> tuple[dict[str, Any], str, str]:
        body, action, content_hash = self.cache.get_or_compute(
            key,
            node,
            producer,
            {
                "config_version": self.config.get("config_version"),
                "precision_profile": self.profile_name,
                "window_id": spec["window_id"],
            },
        )
        self._record_action(action, node, spec, key, content_hash)
        return body, action, content_hash

    def _commit_stage_if_needed(
        self,
        spec: dict[str, Any],
        stage_index: int,
        stage_name: str,
        keys: dict[str, str],
        hashes: dict[str, str],
        v_idx: list[int],
        input_digest: str,
    ) -> dict[str, Any]:
        latest = self.state.load_latest(spec["window_id"], self.profile_name)
        if latest and int(latest.get("stage_index", 0)) >= stage_index:
            existing_keys = latest.get("cache_keys", {})
            if all(existing_keys.get(node) == key for node, key in keys.items()):
                latest["commit_action"] = "skipped"
                return latest

        snapshot = self.state.commit_snapshot(
            {
                "window_id": spec["window_id"],
                "window_index": spec["window_index"],
                "window_range": self._window_range(spec),
                "stage": stage_name,
                "stage_index": stage_index,
                "stage_names": STAGES,
                "config_version": self.config.get("config_version"),
                "config_hash": self.config_hash,
                "precision_profile": self.profile_name,
                "precision": self.precision.config_view(),
                "completed_v_idx": sorted(int(x) for x in v_idx),
                "completed_v_idx_count": len(v_idx),
                "input_digest": input_digest,
                "cache_keys": keys,
                "content_hashes": hashes,
                "complete": stage_index == len(STAGES),
            }
        )
        self.cache.mark_committed(list(keys.values()))
        self.commit_count += 1
        snapshot["commit_action"] = "committed"
        if self.crash_after_commits is not None and self.commit_count >= self.crash_after_commits:
            os.kill(os.getpid(), signal.SIGKILL)
        return snapshot

    def run_window(
        self,
        spec: dict[str, Any],
        records: list[dict[str, Any]],
        previous_drift: dict[str, Any] | None,
    ) -> dict[str, Any]:
        validate_records(records, self.config)
        input_digest = sha256_json({"window": spec, "records": records})
        base = self._base_identity(spec)
        keys: dict[str, str] = {}
        hashes: dict[str, str] = {}

        pre_identity = {
            **base,
            "input_digest": input_digest,
            "variables": self.config.get("variables", []),
            "completion": self.config.get("completion", {}),
        }
        keys["preprocess"] = derive_cache_key("preprocess", pre_identity)
        pre, _, hashes["preprocess"] = self._get_or_compute(
            "preprocess",
            keys["preprocess"],
            lambda: preprocess_window(records, spec, self.config, self.precision),
            spec,
        )
        self._commit_stage_if_needed(
            spec, 1, STAGES[0], {"preprocess": keys["preprocess"]},
            {"preprocess": hashes["preprocess"]}, pre["v_idx"], input_digest
        )

        cross_identity = {
            **base,
            "upstream_preprocess_key": keys["preprocess"],
            "upstream_preprocess_hash": hashes["preprocess"],
            "cross_config": self.config.get("cross", {}),
        }
        keys["cross_expand"] = derive_cache_key("cross_expand", cross_identity)
        cross, _, hashes["cross_expand"] = self._get_or_compute(
            "cross_expand",
            keys["cross_expand"],
            lambda: expand_cross_features(pre, self.config, self.precision),
            spec,
        )

        std_identity = {
            **base,
            "upstream_cross_key": keys["cross_expand"],
            "upstream_cross_hash": hashes["cross_expand"],
            "upstream_preprocess_hash": hashes["preprocess"],
            "condition_config": {
                "method": self.config.get("cross", {}).get("condition_method"),
                "bins": self.config.get("cross", {}).get("condition_bins"),
            },
        }
        keys["standardize"] = derive_cache_key("standardize", std_identity)
        standardized, _, hashes["standardize"] = self._get_or_compute(
            "standardize",
            keys["standardize"],
            lambda: standardize_cross_features(cross, pre, self.config, self.precision),
            spec,
        )
        stage2_keys = {
            "preprocess": keys["preprocess"],
            "cross_expand": keys["cross_expand"],
            "standardize": keys["standardize"],
        }
        stage2_hashes = {k: hashes[k] for k in stage2_keys}
        self._commit_stage_if_needed(
            spec, 2, STAGES[1], stage2_keys, stage2_hashes, pre["v_idx"], input_digest
        )

        reduction_cfg = self.config.get("reduction", {})
        active_candidate = next(
            c for c in reduction_cfg.get("candidates", []) if c.get("name") == reduction_cfg.get("active")
        )
        reduce_identity = {
            **base,
            "upstream_standardize_key": keys["standardize"],
            "upstream_standardize_hash": hashes["standardize"],
            "candidate": active_candidate,
            "covariance": self.config.get("covariance", {}),
        }
        keys["reduce"] = derive_cache_key("reduce", reduce_identity)
        reduced, _, hashes["reduce"] = self._get_or_compute(
            "reduce",
            keys["reduce"],
            lambda: reduce_features(standardized, self.config, self.precision),
            spec,
        )
        stage3_keys = {**stage2_keys, "reduce": keys["reduce"]}
        stage3_hashes = {k: hashes[k] for k in stage3_keys}
        self._commit_stage_if_needed(
            spec, 3, STAGES[2], stage3_keys, stage3_hashes, pre["v_idx"], input_digest
        )

        predictor_identity = {
            **base,
            "upstream_reduce_key": keys["reduce"],
            "upstream_reduce_hash": hashes["reduce"],
            "upstream_preprocess_hash": hashes["preprocess"],
            "predictor": self.config.get("predictor", {}),
        }
        keys["predict"] = derive_cache_key("predict", predictor_identity)
        prediction, _, hashes["predict"] = self._get_or_compute(
            "predict",
            keys["predict"],
            lambda: predict_window(reduced, pre, self.config, self.precision),
            spec,
        )

        previous_drift_hash = sha256_json(previous_drift) if previous_drift is not None else None
        error_identity = {
            **base,
            "upstream_predict_key": keys["predict"],
            "upstream_predict_hash": hashes["predict"],
            "upstream_reduce_hash": hashes["reduce"],
            "upstream_preprocess_hash": hashes["preprocess"],
            "previous_drift_hash": previous_drift_hash,
            "drift_config": self.config.get("drift", {}),
            "gating": self.config.get("gating", {}),
        }
        keys["error"] = derive_cache_key("error", error_identity)
        error, _, hashes["error"] = self._get_or_compute(
            "error",
            keys["error"],
            lambda: build_error_spectrum(
                prediction, reduced, pre, self.config, self.precision, previous_drift
            ),
            spec,
        )

        stage4_keys = {**stage3_keys, "predict": keys["predict"], "error": keys["error"]}
        stage4_hashes = {k: hashes[k] for k in stage4_keys}
        snapshot = self._commit_stage_if_needed(
            spec, 4, STAGES[3], stage4_keys, stage4_hashes, pre["v_idx"], input_digest
        )

        trace = self._build_trace(
            spec, keys, hashes, pre, prediction, error, previous_drift, previous_drift_hash
        )
        trace_key = self.cache.save_trace(trace)
        trace["trace_key"] = trace_key
        for record in records:
            self.cache.upsert_v_idx(
                int(record["v_idx"]), spec["window_id"], str(record["src_name"]), int(record["obs_time_ms"])
            )
        return {
            "spec": spec,
            "preprocess": pre,
            "cross": cross,
            "standardized": standardized,
            "reduced": reduced,
            "prediction": prediction,
            "error": error,
            "keys": keys,
            "hashes": hashes,
            "trace": trace,
            "snapshot": snapshot,
        }

    def _build_trace(
        self,
        spec: dict[str, Any],
        keys: dict[str, str],
        hashes: dict[str, str],
        pre: dict[str, Any],
        prediction: dict[str, Any],
        error: dict[str, Any],
        previous_drift: dict[str, Any] | None,
        previous_drift_hash: str | None,
    ) -> dict[str, Any]:
        trace_identity = {
            "window_id": spec["window_id"],
            "profile": self.profile_name,
            "error_key": keys["error"],
            "config_hash": self.config_hash,
        }
        trace_key = derive_cache_key("trace", trace_identity)
        return {
            "trace_key": trace_key,
            "window_id": spec["window_id"],
            "window_range": self._window_range(spec),
            "v_idx": list(pre["v_idx"]),
            "config_version": self.config.get("config_version"),
            "config_hash": self.config_hash,
            "precision": self.precision.config_view(),
            "node_path": list(NODE_ORDER),
            "cache_keys": dict(keys),
            "content_hashes": dict(hashes),
            "previous_drift_hash": previous_drift_hash,
            "previous_drift": previous_drift,
            "model": prediction["model"],
            "candidate": {
                "name": self.config["reduction"]["active"],
                "method": next(
                    c["method"]
                    for c in self.config["reduction"]["candidates"]
                    if c["name"] == self.config["reduction"]["active"]
                ),
            },
            "summary": {
                "prediction_shape": list(prediction["predictions"].shape),
                "residual_count": int(prediction["residuals"].shape[0]),
                "drift_score": error["drift"]["score"],
                "drift_gate": error["drift"]["gate"],
                "gating_accept_rate": error["gating"]["accept_rate"],
            },
        }

    def run_all(
        self,
        records: list[dict[str, Any]] | None = None,
        recover_first: bool = True,
    ) -> dict[str, Any]:
        if records is not None:
            self.records = records
        validate_records(self.records, self.config)
        if recover_first:
            recovery = self.recover()
        else:
            recovery = None
        self.actions = []
        specs = build_window_specs(self.config, self.records)
        window_results = []
        previous_drift = None
        for spec in specs:
            selected = select_window_records(self.records, spec)
            result = self.run_window(spec, selected, previous_drift)
            previous_drift = result["error"]["drift"]
            window_results.append(result)

        verification = self.verify_consistency(window_results)
        return {
            "ok": True,
            "profile": self.profile_name,
            "config_version": self.config.get("config_version"),
            "config_hash": self.config_hash,
            "window_count": len(window_results),
            "record_count": len(self.records),
            "recovery": recovery,
            "actions": self.actions,
            "windows": [self._window_summary(item) for item in window_results],
            "verification": verification,
        }

    def _window_summary(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "window_id": result["spec"]["window_id"],
            "v_idx": result["preprocess"]["v_idx"],
            "keys": result["keys"],
            "hashes": result["hashes"],
            "trace_key": result["trace"]["trace_key"],
            "summary": result["trace"]["summary"],
            "snapshot_version": result["snapshot"].get("version"),
        }

    def verify_consistency(self, window_results: list[dict[str, Any]]) -> dict[str, Any]:
        checks = []
        for result in window_results:
            pred = result["prediction"]
            if not all(key in result["error"]["drift"] for key in ("residual_mean", "score", "feature_shift")):
                raise DiagnosticError("E_DRIFT_MISSING", "window result lacks drift statistics")
            levels = pred["quantiles"]
            if any(a >= b for a, b in zip(levels, levels[1:])):
                raise DiagnosticError("E_QUANTILE_ORDER", "quantile levels are not ascending")
            checks.append(
                {
                    "window_id": result["spec"]["window_id"],
                    "cache_key_count": len(result["keys"]),
                    "snapshot_complete": bool(result["snapshot"].get("complete")),
                    "trace_key": result["trace"]["trace_key"],
                }
            )
        return {"ok": True, "window_checks": checks, "check_count": len(checks)}

    def query_v_idx(self, v_idx: int, profile: str | None = None) -> dict[str, Any]:
        selected_profile = profile or self.profile_name
        row = self.cache.query_v_idx_profile(int(v_idx), selected_profile)
        if row is None:
            raise DiagnosticError("E_NOT_FOUND", "v_idx was not found in completed index", v_idx=v_idx)
        trace = row.pop("trace", None)
        if trace is None:
            raise DiagnosticError("E_NOT_FOUND", "trace is not available for requested profile", v_idx=v_idx, profile=selected_profile)
        return {"v_idx": int(v_idx), "index": row, "trace": trace}

    def verify_v_idx(self, v_idx: int, profile: str | None = None) -> dict[str, Any]:
        queried = self.query_v_idx(v_idx, profile)
        trace = queried["trace"]
        if trace.get("config_hash") != self.config_hash:
            raise stale_result(
                "current configuration dependency hash differs from recorded trace",
                v_idx=v_idx,
                recorded_config_hash=trace.get("config_hash"),
                current_config_hash=self.config_hash,
            )
        precision = self.precision if profile is None or profile == self.profile_name else validate_config(self.config, profile)
        if precision.digest() != trace["precision"]["digest"]:
            raise stale_result(
                "precision dependency differs from recorded trace",
                recorded=trace["precision"],
                current=precision.config_view(),
            )

        for node, key in trace["cache_keys"].items():
            envelope = self.cache.get_envelope(key)
            if envelope["content_hash"] != trace["content_hashes"].get(node):
                raise stale_result("recorded content hash does not match cache object", node=node, key=key)

        predict_body = self.cache.get_body(trace["cache_keys"]["predict"])
        replayed_prediction = predict_quantiles(predict_body["coordinates"], trace["model"], precision)
        if sha256_json(replayed_prediction["predictions"]) != sha256_json(predict_body["predictions"]):
            raise stale_result("prediction replay differs from recorded cache result", v_idx=v_idx)

        pre_body = self.cache.get_body(trace["cache_keys"]["preprocess"])
        reduce_body = self.cache.get_body(trace["cache_keys"]["reduce"])
        previous_drift = trace.get("previous_drift")
        if previous_drift is None and trace.get("previous_drift_hash"):
            raise DiagnosticError("E_DRIFT_MISSING", "trace lacks previous drift needed for full error replay")
        replayed_error = build_error_spectrum(
            predict_body, reduce_body, pre_body, self.config, precision, previous_drift
        )
        error_envelope = self.cache.get_envelope(trace["cache_keys"]["error"])
        if sha256_json(replayed_error) != error_envelope["content_hash"] and sha256_json(replayed_error) != sha256_json(error_envelope["body"]):
            raise stale_result("error spectrum replay differs from recorded cache result", v_idx=v_idx)

        return {
            "ok": True,
            "v_idx": int(v_idx),
            "replayed_prediction_hash": sha256_json(replayed_prediction["predictions"]),
            "replayed_error_hash": error_envelope["content_hash"],
            "cache_keys": trace["cache_keys"],
            "precision": precision.config_view(),
            "config_version": trace["config_version"],
            "node_path": trace["node_path"],
            "checks": ["config_hash", "cache_checksums", "prediction_recompute", "error_spectrum_recompute"],
        }

    def plan_invalidation(self, change: dict[str, Any]) -> dict[str, Any]:
        change_type = change.get("type")
        specs = build_window_specs(self.config, self.records)
        affected_windows: list[str] = []
        affected_v_idx: set[int] = set()
        affected_nodes: set[str] = set()
        reuse_nodes: set[str] = set(NODE_ORDER)

        def add_window(spec: dict[str, Any]) -> None:
            affected_windows.append(spec["window_id"])
            for record in select_window_records(self.records, spec):
                affected_v_idx.add(int(record["v_idx"]))

        if change_type == "window_slide":
            from_index = int(change.get("from_window_index", 0))
            for spec in specs:
                if spec["window_index"] >= from_index:
                    add_window(spec)
            affected_nodes.update(NODE_ORDER)
            reuse_nodes.clear()
        elif change_type == "variable_reorganized":
            variable = change.get("variable")
            from_index = int(change.get("from_window_index", 0))
            for spec in specs:
                records = select_window_records(self.records, spec)
                touches = variable is None or any(r["src_name"] == variable for r in records)
                if spec["window_index"] >= from_index and touches:
                    add_window(spec)
            affected_nodes.update(NODE_ORDER)
            reuse_nodes.clear()
        elif change_type == "cross_slot_changed":
            for spec in specs:
                add_window(spec)
            affected_nodes.update(["cross_expand", "standardize", "reduce", "predict", "error"])
            reuse_nodes.difference_update(affected_nodes)
        elif change_type == "reduction_changed":
            for spec in specs:
                add_window(spec)
            affected_nodes.update(["reduce", "predict", "error"])
            reuse_nodes.difference_update(affected_nodes)
        elif change_type == "precision_changed":
            for spec in specs:
                add_window(spec)
            affected_nodes.update(NODE_ORDER)
            reuse_nodes.clear()
        else:
            raise DiagnosticError("E_BAD_INPUT", "unknown invalidation change type", change=change_type)

        # Online drift couples every changed window to its suffix.
        if affected_windows and change_type in {"window_slide", "variable_reorganized"}:
            indexes = [spec["window_index"] for spec in specs if spec["window_id"] in affected_windows]
            if indexes:
                first = min(indexes)
                for spec in specs:
                    if spec["window_index"] > first and spec["window_id"] not in affected_windows:
                        add_window(spec)
                        affected_nodes.add("error")
        return {
            "change": change,
            "dependency_rules": {
                "window_slide": "changed window preprocess and all nodes; online drift invalidates suffix error nodes",
                "variable_reorganized": "windows containing variable recompute all nodes; suffix error nodes recompute",
                "cross_slot_changed": "preprocess reused; cross_expand and all downstream nodes recompute",
                "reduction_changed": "preprocess/cross/standardize reused; reduce and downstream recompute",
                "precision_changed": "structural path unchanged; precision-scoped keys recompute",
            },
            "affected_windows": sorted(set(affected_windows)),
            "affected_v_idx": sorted(affected_v_idx),
            "affected_nodes": sorted(affected_nodes),
            "reuse_candidate_nodes": sorted(reuse_nodes),
        }

    def run_incremental(self, change: dict[str, Any]) -> dict[str, Any]:
        plan = self.plan_invalidation(change)
        report = self.run_all(recover_first=True)
        recomputed = [a for a in self.actions if a["action"] == "recomputed"]
        reused = [a for a in self.actions if a["action"] == "reused"]
        manifest = {
            "plan": plan,
            "recomputed": recomputed,
            "reused": reused,
            "config_hash": self.config_hash,
            "profile": self.profile_name,
        }
        manifest_path = self.state.commit_incremental_manifest(manifest)
        report["incremental"] = {
            "plan": plan,
            "recomputed_count": len(recomputed),
            "reused_count": len(reused),
            "manifest_path": str(manifest_path),
        }
        return report


def engine_from_files(
    config_path: str | Path,
    observations_path: str | Path,
    profile: str | None = None,
    workdir: str | Path | None = None,
) -> DiagnosticEngine:
    import json

    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    records = read_jsonl(observations_path)
    base = Path(workdir) if workdir is not None else Path.cwd()
    return DiagnosticEngine(config, records, profile=profile, workdir=base)
