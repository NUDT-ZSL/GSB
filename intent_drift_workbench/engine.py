"""Incremental pipeline, exact invalidation propagation, and full verification."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable

from .canonical import quantize, sha256_json
from .comparison import compare_results
from .config import (
    ConfigError,
    base_vocab_hash,
    config_hash,
    load_config,
    validate_weights,
    weights_hash,
)
from .features import (
    VocabState,
    build_vocab,
    extract_features,
    fold_text,
    normalize_vocabulary_terms,
    refresh_feature_cache_key,
)
from .loading import (
    failure,
    load_events,
    sort_failures,
    validate_event,
)
from .scoring import label_alerts
from .snapshot import SnapshotManager
from .updates import (
    affected_start_keys,
    force_exception_maps,
    injection_hash,
    load_injections,
    load_plan,
    plan_hash,
)
from .windows import (
    build_all_windows,
    build_window,
    merge_scoring_artifact,
    recompute_score_starts,
    score_all_windows,
    windows_to_stage_artifact,
    windows_to_scoring_artifact,
)


def _event_key(event: dict[str, Any]) -> tuple[str, int]:
    return str(event["session_id"]), int(event["turn_no"])


def _turn_key(feature: dict[str, Any]) -> tuple[str, int]:
    return str(feature["session_id"]), int(feature["turn_no"])


def _window_key(window: dict[str, Any]) -> tuple[str, int]:
    return str(window["session_id"]), int(window["start_turn"])


def _event_from_op(op: dict[str, Any], op_kind: str) -> tuple[dict[str, Any] | None, str | None]:
    session_id = op.get("session_id")
    turn_no = op.get("turn_no")
    if op_kind in {"add", "replace", "upsert"}:
        raw_event = op.get("event")
        if raw_event is None:
            raw_event = {
                key: value
                for key, value in op.items()
                if key not in {"op", "batch_id", "event", "reason"}
            }
        if not isinstance(raw_event, dict):
            return None, "event must be an object"
        event = dict(raw_event)
        if "session_id" in event and session_id is not None and event["session_id"] != session_id:
            return None, "op session_id and event session_id disagree"
        if "turn_no" in event and turn_no is not None and event["turn_no"] != turn_no:
            return None, "op turn_no and event turn_no disagree"
        if "session_id" not in event:
            event["session_id"] = session_id
        if "turn_no" not in event:
            event["turn_no"] = turn_no
        return event, None
    return None, None


class WorkbenchEngine:
    def __init__(
        self,
        *,
        config_path: Path,
        events_path: Path,
        plan_path: Path,
        injections_path: Path | None,
        snapshot_root: Path | None = None,
        execution: str = "parallel",
        resume: bool = True,
    ):
        self.config_path = Path(config_path)
        self.events_path = Path(events_path)
        self.plan_path = Path(plan_path)
        self.injections_path = Path(injections_path) if injections_path else None
        self.execution = execution
        if execution not in {"serial", "parallel"}:
            raise ValueError("execution must be 'serial' or 'parallel'")
        self.config = load_config(self.config_path)
        self.plan = load_plan(self.plan_path)
        self.injections = load_injections(self.injections_path)
        self.worker_count = 1 if execution == "serial" else int(self.config["concurrency"])
        self.force_turn_map, self.force_window_map = force_exception_maps(self.injections)
        self.snapshot_manager = SnapshotManager(Path(snapshot_root)) if snapshot_root else None
        self.resume = resume
        self.snapshot_manifest: dict[str, Any] | None = None
        self.recovery_info: dict[str, Any] | None = None
        self.prepared = False
        self._reset_runtime()

    def _reset_runtime(self) -> None:
        self.vocab = self._make_vocab(self.config["vocabulary"], self.config["intent_vocab_version"])
        self.weights = dict(self.config["weights"])
        self.source_events: dict[tuple[str, int], dict[str, Any]] = {}
        self.features: dict[tuple[str, int], dict[str, Any]] = {}
        self.windows: dict[tuple[str, int], dict[str, Any]] = {}
        self.term_refs: dict[str, set[tuple[str, int]]] = {}
        self.ingest_validation_failures: list[dict[str, Any]] = []
        self.update_structural_failures: list[dict[str, Any]] = []
        self.feature_failures_by_key: dict[tuple[str, int], dict[str, Any]] = {}
        self.window_failures_by_key: dict[tuple[str, int], dict[str, Any]] = {}
        self.failures: list[dict[str, Any]] = []
        self.applied_batches: list[str] = []
        self.invalidation_history: list[dict[str, Any]] = []
        self.state_version = 0
        self.full_rebuild_result: dict[str, Any] | None = None
        self.equivalence: dict[str, Any] | None = None

    def _make_vocab(self, terms: Iterable[str], version: str) -> VocabState:
        normalized = normalize_vocabulary_terms(
            terms, self.config["fold_rules"], self.config["categories"]
        )
        return build_vocab(self.config["categories"], normalized, version)

    def prepare(self) -> "WorkbenchEngine":
        if self.prepared:
            return self
        recovered = False
        if self.snapshot_manager is not None and self.resume:
            latest = self.snapshot_manager.load_latest()
            if latest is not None:
                self._restore_snapshot(*latest)
                recovered = True
        if not recovered:
            self._initial_build()
            if self.snapshot_manager is not None:
                self.commit_snapshot(force=True)
        self.prepared = True
        return self

    def _initial_build(self) -> None:
        events, failures = load_events(self.events_path)
        self.source_events = dict(events)
        self.ingest_validation_failures = list(failures)
        extracted, feature_failures = self._extract_many(events, batch_id=None)
        self.features = extracted
        self.feature_failures_by_key = {
            _turn_key(item): item for item in feature_failures
        }
        self._rebuild_term_refs()
        turns_by_session = self._turns_by_session()
        window_list = build_all_windows(
            turns_by_session,
            vocab=self.vocab,
            window_size=int(self.config["window_size"]),
        )
        window_list, score_failures, _ = score_all_windows(
            window_list,
            vocab=self.vocab,
            weights=self.weights,
            threshold=float(self.config["drift_threshold"]),
            precision=int(self.config["precision"]),
            forced_window_exceptions=self.force_window_map,
        )
        self.windows = {_window_key(item): item for item in window_list}
        self.window_failures_by_key = {
            _turn_key_from_window_failure(item): item for item in score_failures
        }
        self._assemble_failures()

    def _restore_snapshot(self, manifest: dict[str, Any], payload: dict[str, Any]) -> None:
        expected_config_hash = config_hash(self.config)
        expected_base_vocab_hash = base_vocab_hash(self.config)
        if manifest.get("config_hash") != expected_config_hash:
            raise ConfigError(
                "E_CONFIG_MISMATCH",
                "snapshot config_hash does not match the current static configuration",
            )
        if manifest.get("base_vocab_hash") != expected_base_vocab_hash:
            raise ConfigError(
                "E_CONFIG_MISMATCH",
                "snapshot base_vocab_hash does not match the current base vocabulary",
            )
        state = payload.get("state")
        if not isinstance(state, dict):
            raise ConfigError("E_CONFIG_MISMATCH", "snapshot state payload is invalid")
        if state.get("plan_hash") != plan_hash(self.plan):
            raise ConfigError("E_CONFIG_MISMATCH", "update plan differs from snapshot checkpoint")
        if state.get("injection_hash") != injection_hash(self.injections):
            raise ConfigError("E_CONFIG_MISMATCH", "injection config differs from snapshot checkpoint")
        vocab_payload = state.get("vocab")
        if not isinstance(vocab_payload, dict):
            raise ConfigError("E_CONFIG_MISMATCH", "snapshot vocabulary payload is invalid")
        restored_vocab = build_vocab(
            vocab_payload.get("categories", []),
            vocab_payload.get("terms", []),
            str(vocab_payload.get("version", "")),
        )
        if restored_vocab.hash_value != manifest.get("vocab_hash"):
            raise ConfigError("E_CONFIG_MISMATCH", "snapshot runtime vocab hash is inconsistent")
        self.vocab = restored_vocab
        self.weights = validate_weights(state.get("weights"))
        if weights_hash(self.weights) != manifest.get("weights_hash"):
            raise ConfigError("E_CONFIG_MISMATCH", "snapshot runtime weights hash is inconsistent")
        self.source_events = {
            _event_key(item): item for item in state.get("source_events", [])
        }
        self.features = {
            _turn_key(item): item for item in payload.get("features", [])
        }
        restored_windows = list(payload.get("windows", []))
        restored_scoring = list(payload.get("scoring", []))
        merge_scoring_artifact(restored_windows, restored_scoring)
        self.windows = {_window_key(item): item for item in restored_windows}
        self.ingest_validation_failures = list(state.get("ingest_validation_failures", []))
        self.update_structural_failures = list(state.get("update_structural_failures", []))
        self.feature_failures_by_key = {
            _turn_key(item): item for item in state.get("feature_failures", [])
        }
        self.window_failures_by_key = {
            _turn_key_from_window_failure(item): item
            for item in state.get("window_failures", [])
        }
        self.applied_batches = list(manifest.get("completed_batches", []))
        expected_prefix = [
            item["batch_id"] for item in self.plan["batches"][: len(self.applied_batches)]
        ]
        if self.applied_batches != expected_prefix:
            raise ConfigError("E_CONFIG_MISMATCH", "snapshot batch prefix does not match update plan")
        self.invalidation_history = list(state.get("invalidation_history", []))
        self.state_version = int(manifest.get("global_version", len(self.applied_batches)))
        self.snapshot_manifest = manifest
        self.recovery_info = self.snapshot_manager.recovery_info(manifest)
        self._rebuild_term_refs()
        self._assemble_failures()

    def _turns_by_session(self) -> dict[str, dict[int, dict[str, Any]]]:
        output: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
        for feature in self.features.values():
            output[str(feature["session_id"])][int(feature["turn_no"])] = feature
        return dict(output)

    def _extract_one(
        self,
        event: dict[str, Any],
        *,
        batch_id: str | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        key = _event_key(event)
        forced = self.force_turn_map.get(key)
        if forced is not None:
            item = failure(
                code="E_TURN_INTERNAL",
                stage="feature",
                message="injected turn feature exception",
                session_id=key[0],
                turn_no=key[1],
                batch_id=batch_id,
                injection_id=forced.get("id"),
                exception_type="InjectedTurnError",
            )
            return None, item
        try:
            return extract_features(event, self.vocab, self.config["fold_rules"]), None
        except Exception as exc:
            return None, failure(
                code="E_TURN_INTERNAL",
                stage="feature",
                message=str(exc),
                session_id=key[0],
                turn_no=key[1],
                batch_id=batch_id,
                injection_id=event.get("injection_id"),
                exception_type=type(exc).__name__,
            )

    def _extract_many(
        self,
        events: dict[tuple[str, int], dict[str, Any]],
        *,
        batch_id: str | None,
    ) -> tuple[dict[tuple[str, int], dict[str, Any]], list[dict[str, Any]]]:
        tasks = [events[key] for key in sorted(events)]
        if not tasks:
            return {}, []
        if self.worker_count <= 1 or len(tasks) == 1:
            results = [self._extract_one(event, batch_id=batch_id) for event in tasks]
        else:
            with ThreadPoolExecutor(max_workers=min(self.worker_count, len(tasks))) as executor:
                futures = [
                    executor.submit(self._extract_one, event, batch_id=batch_id)
                    for event in tasks
                ]
                results = [future.result() for future in futures]
        features: dict[tuple[str, int], dict[str, Any]] = {}
        failures: list[dict[str, Any]] = []
        for feature, error in results:
            if feature is not None:
                features[_turn_key(feature)] = feature
            if error is not None:
                failures.append(error)
        return features, sort_failures(failures)

    def _rebuild_term_refs(self) -> None:
        refs: dict[str, set[tuple[str, int]]] = defaultdict(set)
        for key, feature in self.features.items():
            for term in feature["raw_terms"]:
                refs[term].add(key)
        self.term_refs = dict(refs)

    def _assemble_failures(self) -> None:
        items = (
            list(self.ingest_validation_failures)
            + list(self.update_structural_failures)
            + list(self.feature_failures_by_key.values())
            + list(self.window_failures_by_key.values())
        )
        self.failures = sort_failures(items)

    def commit_snapshot(self, *, force: bool = False) -> dict[str, Any] | None:
        if self.snapshot_manager is None:
            return None
        if (
            not force
            and self.snapshot_manifest is not None
            and int(self.snapshot_manifest.get("global_version", -1)) == self.state_version
        ):
            return self.snapshot_manifest
        window_list = self._sorted_windows()
        state = {
            "plan_hash": plan_hash(self.plan),
            "injection_hash": injection_hash(self.injections),
            "config_hash": config_hash(self.config),
            "base_vocab_hash": base_vocab_hash(self.config),
            "state_version": self.state_version,
            "vocab": self.vocab.payload(),
            "weights": dict(self.weights),
            "source_events": [self.source_events[key] for key in sorted(self.source_events)],
            "ingest_validation_failures": self.ingest_validation_failures,
            "update_structural_failures": self.update_structural_failures,
            "feature_failures": list(self.feature_failures_by_key.values()),
            "window_failures": list(self.window_failures_by_key.values()),
            "invalidation_history": self.invalidation_history,
        }
        manifest = self.snapshot_manager.commit(
            global_version=self.state_version,
            features=[self.features[key] for key in sorted(self.features)],
            windows=windows_to_stage_artifact(window_list),
            scoring=windows_to_scoring_artifact(window_list),
            failures=self.failures,
            state=state,
            completed_batches=self.applied_batches,
            config_hash=config_hash(self.config),
            base_vocab_hash=base_vocab_hash(self.config),
            vocab_hash=self.vocab.hash_value,
            weights_hash=weights_hash(self.weights),
        )
        self.snapshot_manifest = manifest
        return manifest

    def apply_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        if not self.prepared:
            self.prepare()
        batch_id = str(batch["batch_id"])
        old_term_refs = {term: set(keys) for term, keys in self.term_refs.items()}
        turn_changed: set[tuple[str, int]] = set()
        turn_operations = 0
        structural_failures: list[dict[str, Any]] = []

        for op in batch.get("turn_ops", []):
            turn_operations += 1
            if not isinstance(op, dict):
                structural_failures.append(self._update_failure(
                    "E_UPDATE_OP_INVALID", "turn op must be an object", batch_id=batch_id
                ))
                continue
            kind = op.get("op")
            if kind not in {"add", "replace", "delete", "upsert"}:
                structural_failures.append(self._update_failure(
                    "E_UPDATE_OP_INVALID",
                    f"unsupported turn op: {kind!r}",
                    session_id=op.get("session_id"),
                    turn_no=op.get("turn_no"),
                    batch_id=batch_id,
                ))
                continue
            if kind == "delete":
                session_id = op.get("session_id")
                turn_no = op.get("turn_no")
                if not isinstance(session_id, str) or not session_id or not isinstance(turn_no, int):
                    structural_failures.append(self._update_failure(
                        "E_UPDATE_OP_INVALID",
                        "delete requires session_id and integer turn_no",
                        batch_id=batch_id,
                    ))
                    continue
                key = (session_id, turn_no)
                if key not in self.source_events:
                    structural_failures.append(self._update_failure(
                        "E_UPDATE_TARGET_MISSING",
                        "delete target does not exist",
                        session_id=session_id,
                        turn_no=turn_no,
                        batch_id=batch_id,
                    ))
                    continue
                self.source_events.pop(key, None)
                self.features.pop(key, None)
                self.feature_failures_by_key.pop(key, None)
                turn_changed.add(key)
                continue

            raw_event, error_message = _event_from_op(op, kind)
            if raw_event is None:
                structural_failures.append(self._update_failure(
                    "E_UPDATE_OP_INVALID",
                    error_message or "invalid event payload",
                    session_id=op.get("session_id"),
                    turn_no=op.get("turn_no"),
                    batch_id=batch_id,
                ))
                continue
            event, validation_failure = validate_event(
                raw_event,
                line_no=None,
                expected_turn=None,
                stage="update",
                batch_id=batch_id,
            )
            if validation_failure is not None:
                session_id = raw_event.get("session_id")
                turn_no = raw_event.get("turn_no")
                if isinstance(session_id, str) and isinstance(turn_no, int):
                    key = (session_id, turn_no)
                    if kind in {"replace", "upsert"} and key in self.source_events:
                        self.source_events.pop(key, None)
                        self.features.pop(key, None)
                        self.feature_failures_by_key.pop(key, None)
                        turn_changed.add(key)
                structural_failures.append(validation_failure)
                continue
            assert event is not None
            key = _event_key(event)
            if kind in {"replace", "upsert"}:
                if key not in self.source_events:
                    structural_failures.append(self._update_failure(
                        "E_UPDATE_TARGET_MISSING",
                        "replace target does not exist",
                        session_id=key[0],
                        turn_no=key[1],
                        batch_id=batch_id,
                    ))
                    continue
            else:
                if key in self.source_events:
                    structural_failures.append(self._update_failure(
                        "E_UPDATE_TARGET_EXISTS",
                        "add target already exists",
                        session_id=key[0],
                        turn_no=key[1],
                        batch_id=batch_id,
                    ))
                    continue
            self.source_events[key] = event
            self.features.pop(key, None)
            self.feature_failures_by_key.pop(key, None)
            turn_changed.add(key)

        vocab_changed_terms: set[str] = set()
        vocab_operations = 0
        working_terms = set(self.vocab.terms)
        for op in batch.get("vocab_ops", []):
            vocab_operations += 1
            if not isinstance(op, dict):
                structural_failures.append(self._update_failure(
                    "E_UPDATE_OP_INVALID", "vocab op must be an object", batch_id=batch_id
                ))
                continue
            kind = op.get("op")
            raw_term = op.get("term")
            if not isinstance(raw_term, str) or not raw_term.strip():
                structural_failures.append(self._update_failure(
                    "E_VOCAB_TERM_INVALID",
                    "vocab term must be a non-empty string",
                    batch_id=batch_id,
                ))
                continue
            try:
                folded, tokens = fold_text(raw_term, self.config["fold_rules"])
                if len(tokens) != 1:
                    raise ValueError(f"term must fold to one token: {folded!r}")
                term = tokens[0]
            except Exception as exc:
                structural_failures.append(self._update_failure(
                    "E_VOCAB_TERM_INVALID",
                    str(exc),
                    batch_id=batch_id,
                ))
                continue
            if kind == "add":
                if term in working_terms:
                    structural_failures.append(self._update_failure(
                        "E_VOCAB_DUPLICATE",
                        f"vocabulary term already exists: {term}",
                        batch_id=batch_id,
                    ))
                    continue
                working_terms.add(term)
                vocab_changed_terms.add(term)
            elif kind == "remove":
                if term not in working_terms:
                    structural_failures.append(self._update_failure(
                        "E_TERM_REFERENCE",
                        f"vocabulary term does not exist: {term}",
                        batch_id=batch_id,
                    ))
                    continue
                working_terms.remove(term)
                vocab_changed_terms.add(term)
            else:
                structural_failures.append(self._update_failure(
                    "E_UPDATE_OP_INVALID",
                    f"unsupported vocab op: {kind!r}",
                    batch_id=batch_id,
                ))

        vocab_affected_turns: set[tuple[str, int]] = set()
        for term in vocab_changed_terms:
            vocab_affected_turns.update(old_term_refs.get(term, set()))
        if vocab_changed_terms:
            self.vocab = self._make_vocab(working_terms, f"{self.vocab.version}+{batch_id}")
            for key, feature in list(self.features.items()):
                if key not in vocab_affected_turns and key not in turn_changed:
                    self.features[key] = refresh_feature_cache_key(
                        feature, self.vocab, self.config["fold_rules"]
                    )

        parameter_changed = False
        weights_before = dict(self.weights)
        patch = batch.get("weight_patch", {})
        if patch:
            candidate = dict(self.weights)
            candidate.update(patch)
            try:
                validated = validate_weights(candidate)
            except ConfigError as exc:
                structural_failures.append(self._update_failure(
                    "E_WEIGHT_INVALID",
                    exc.message,
                    batch_id=batch_id,
                ))
            else:
                parameter_changed = validated != self.weights
                self.weights = validated

        self.update_structural_failures.extend(structural_failures)
        feature_affected = turn_changed | vocab_affected_turns
        for key in feature_affected:
            self.features.pop(key, None)
            self.feature_failures_by_key.pop(key, None)
        reextract_events = {
            key: self.source_events[key] for key in feature_affected if key in self.source_events
        }
        extracted, feature_failures = self._extract_many(reextract_events, batch_id=batch_id)
        self.features.update(extracted)
        for item in feature_failures:
            key = (str(item["session_id"]), int(item["turn_no"]))
            self.feature_failures_by_key[key] = item
        self._rebuild_term_refs()

        window_size = int(self.config["window_size"])
        current_turns = self._turns_by_session()
        turn_window_keys: set[tuple[str, int]] = set()
        for session_id, turn_no in turn_changed:
            existing_starts = {
                start for sid, start in self.windows if sid == session_id
            }
            turn_window_keys.update(affected_start_keys(
                session_id,
                turn_no,
                window_size,
                set(current_turns.get(session_id, {})),
                existing_starts,
            ))
        vocab_window_keys: set[tuple[str, int]] = set()
        for session_id, turn_no in vocab_affected_turns:
            existing_starts = {
                start for sid, start in self.windows if sid == session_id
            }
            vocab_window_keys.update(affected_start_keys(
                session_id,
                turn_no,
                window_size,
                set(current_turns.get(session_id, {})),
                existing_starts,
            ))
        window_recompute_keys = turn_window_keys | vocab_window_keys
        self._rebuild_windows(window_recompute_keys)
        for key in window_recompute_keys:
            self.window_failures_by_key.pop(key, None)

        score_keys = set(window_recompute_keys)
        score_keys.update((session_id, start + 1) for session_id, start in window_recompute_keys)
        if parameter_changed:
            window_list, score_failures, _ = score_all_windows(
                self._sorted_windows(),
                vocab=self.vocab,
                weights=self.weights,
                threshold=float(self.config["drift_threshold"]),
                precision=int(self.config["precision"]),
                forced_window_exceptions=self.force_window_map,
                batch_id=batch_id,
            )
            self.windows = {_window_key(item): item for item in window_list}
            for item in score_failures:
                self.window_failures_by_key[_turn_key_from_window_failure(item)] = item
            score_keys.update(_window_key(item) for item in window_list if item.get("status") == "full")
        elif score_keys:
            window_list, score_failures, _ = recompute_score_starts(
                self._sorted_windows(),
                score_keys,
                vocab=self.vocab,
                weights=self.weights,
                threshold=float(self.config["drift_threshold"]),
                precision=int(self.config["precision"]),
                forced_window_exceptions=self.force_window_map,
                batch_id=batch_id,
            )
            self.windows = {_window_key(item): item for item in window_list}
            for item in score_failures:
                self.window_failures_by_key[_turn_key_from_window_failure(item)] = item

        self._assemble_failures()
        self.state_version += 1
        self.applied_batches.append(batch_id)
        parameter_window_keys = {
            key for key, item in self.windows.items() if item.get("status") == "full"
        } if parameter_changed else set()
        turn_score_only = {
            (session_id, start + 1) for session_id, start in turn_window_keys
        } - turn_window_keys
        vocab_score_only = {
            (session_id, start + 1) for session_id, start in vocab_window_keys
        } - vocab_window_keys
        invalidation = {
            "batch_id": batch_id,
            "batch_index": len(self.applied_batches),
            "source_counts": {
                "turn": {
                    "operations": turn_operations,
                    "affected_turns": len(turn_changed),
                    "affected_windows": len(turn_window_keys),
                },
                "vocab": {
                    "operations": vocab_operations,
                    "changed_terms": len(vocab_changed_terms),
                    "affected_turns": len(vocab_affected_turns),
                    "affected_windows": len(vocab_window_keys),
                },
                "parameter": {
                    "changed": bool(parameter_changed),
                    "affected_windows": len(parameter_window_keys),
                },
            },
            "turn": {
                "operations": turn_operations,
                "affected_turns": _serialize_turn_keys(turn_changed),
                "affected_windows": _serialize_window_keys(turn_window_keys),
                "downstream_scoring_windows": _serialize_window_keys(turn_score_only),
            },
            "vocab": {
                "operations": vocab_operations,
                "changed_terms": sorted(vocab_changed_terms),
                "affected_turns": _serialize_turn_keys(vocab_affected_turns),
                "affected_windows": _serialize_window_keys(vocab_window_keys),
                "downstream_scoring_windows": _serialize_window_keys(vocab_score_only),
            },
            "parameter": {
                "changed": bool(parameter_changed),
                "changed_fields": sorted(
                    key for key in set(weights_before) | set(self.weights)
                    if weights_before.get(key) != self.weights.get(key)
                ),
                "weights_before": weights_before,
                "weights_after": dict(self.weights),
                "affected_windows": _serialize_window_keys(parameter_window_keys),
            },
            "features_recomputed": _serialize_turn_keys(feature_affected),
            "distributions_recomputed": _serialize_window_keys(window_recompute_keys),
            "scoring_recomputed": _serialize_window_keys(score_keys),
            "feature_recompute_count": len(feature_affected),
            "distribution_recompute_count": len(window_recompute_keys),
            "scoring_recompute_count": len(score_keys),
        }
        self.invalidation_history.append(invalidation)
        return invalidation

    def _update_failure(
        self,
        code: str,
        message: str,
        *,
        batch_id: str,
        session_id: Any = None,
        turn_no: Any = None,
    ) -> dict[str, Any]:
        return failure(
            code=code,
            stage="update",
            message=message,
            session_id=session_id,
            turn_no=turn_no,
            batch_id=batch_id,
        )

    def _rebuild_windows(self, keys: set[tuple[str, int]]) -> None:
        if not keys:
            return
        turns_by_session = self._turns_by_session()
        for session_id, start_turn in sorted(keys):
            if start_turn not in turns_by_session.get(session_id, {}):
                self.windows.pop((session_id, start_turn), None)
                continue
            self.windows[(session_id, start_turn)] = build_window(
                session_id,
                start_turn,
                turns_by_session=turns_by_session,
                vocab=self.vocab,
                window_size=int(self.config["window_size"]),
            )

    def _sorted_windows(self) -> list[dict[str, Any]]:
        return sorted(self.windows.values(), key=lambda item: (item["session_id"], item["start_turn"]))

    def run(
        self,
        *,
        max_batches: int | None = None,
        crash_after_batch: int | None = None,
    ) -> dict[str, Any]:
        self.prepare()
        batches = self.plan["batches"]
        executed_this_run = 0
        for index in range(len(self.applied_batches), len(batches)):
            if max_batches is not None and executed_this_run >= max_batches:
                break
            batch = batches[index]
            self.apply_batch(batch)
            executed_this_run += 1
            final_batch = index == len(batches) - 1
            checkpoint = (
                self.state_version % int(self.config["snapshot_interval"]) == 0
                or final_batch
                or (crash_after_batch is not None and executed_this_run >= crash_after_batch)
            )
            if checkpoint:
                self.commit_snapshot(force=True)
            if crash_after_batch is not None and executed_this_run >= crash_after_batch:
                return {"crash_requested": True, "snapshot": self.snapshot_manifest}
        if self.snapshot_manager is not None:
            self.commit_snapshot(force=False)
        self.full_rebuild_result = self._full_rebuild()
        incremental_result = self.result()
        self.equivalence = compare_results(incremental_result, self.full_rebuild_result)
        return self.result()

    def _full_rebuild(self) -> dict[str, Any]:
        extracted, feature_failures = self._extract_many(self.source_events, batch_id=None)
        turns_by_session: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
        for feature in extracted.values():
            turns_by_session[str(feature["session_id"])][int(feature["turn_no"])] = feature
        window_list = build_all_windows(
            turns_by_session,
            vocab=self.vocab,
            window_size=int(self.config["window_size"]),
        )
        window_list, score_failures, _ = score_all_windows(
            window_list,
            vocab=self.vocab,
            weights=self.weights,
            threshold=float(self.config["drift_threshold"]),
            precision=int(self.config["precision"]),
            forced_window_exceptions=self.force_window_map,
        )
        failures = sort_failures(
            list(self.ingest_validation_failures)
            + list(self.update_structural_failures)
            + list(feature_failures)
            + list(score_failures)
        )
        return {
            "turns": [extracted[key] for key in sorted(extracted)],
            "windows": sorted(window_list, key=lambda item: (item["session_id"], item["start_turn"])),
            "failures": failures,
            "counts": _counts(extracted, window_list, failures),
            "execution": {"mode": self.execution, "max_workers": self.worker_count},
        }

    def result(self) -> dict[str, Any]:
        if not self.prepared:
            self.prepare()
        window_list = self._sorted_windows()
        boundary = label_alerts(
            window_list,
            float(self.config["drift_threshold"]),
            int(self.config["precision"]),
        )
        result = {
            "turns": [self.features[key] for key in sorted(self.features)],
            "windows": window_list,
            "failures": self.failures,
            "counts": _counts(self.features, window_list, self.failures),
            "boundary_alerts": boundary,
            "invalidation_history": self.invalidation_history,
            "execution": {"mode": self.execution, "max_workers": self.worker_count},
            "state_version": self.state_version,
            "applied_batches": list(self.applied_batches),
            "recovery": self.recovery_info,
            "vocab": {
                "version": self.vocab.version,
                "hash": self.vocab.hash_value,
                "term_count": len(self.vocab.terms),
            },
            "weights": dict(self.weights),
            "threshold": quantize(
                float(self.config["drift_threshold"]), int(self.config["precision"])
            ),
            "dependency_summary": self.dependency_summary(),
            "injection_hits": self.injection_hits(boundary),
        }
        if self.snapshot_manifest is not None:
            result["snapshot"] = {
                "global_version": self.snapshot_manifest.get("global_version"),
                "path": self.snapshot_manifest.get("_directory"),
                "completed_batches": self.snapshot_manifest.get("completed_batches", []),
                "stage_versions": self.snapshot_manifest.get("stage_versions", {}),
            }
        if self.equivalence is not None:
            result["equivalence"] = self.equivalence
        return result

    def dependency_summary(self) -> dict[str, Any]:
        term_index = {
            term: _serialize_turn_keys(self.term_refs.get(term, set()))
            for term in self.vocab.terms
        }
        window_index = {
            item["window_id"]: {
                "covered_turns": _serialize_turn_keys(
                    {(turn["session_id"], turn["turn_no"]) for turn in item["covered_turns"]}
                ),
                "referenced_terms": list(item.get("referenced_terms", [])),
                "status": item.get("status"),
            }
            for item in self._sorted_windows()
        }
        return {
            "vocab_term_refs": term_index,
            "window_dependencies": window_index,
            "raw_term_count": len(self.term_refs),
        }

    def injection_hits(self, boundary_alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        failure_ids: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in self.failures:
            injection_id = item.get("injection_id")
            if injection_id:
                failure_ids[str(injection_id)].append(item)
        for declaration in self.injections.get("injections", []):
            identifier = str(declaration.get("id"))
            evidence = failure_ids.get(identifier, [])
            if not evidence:
                for key, item in self.feature_failures_by_key.items():
                    if item.get("injection_id") == identifier:
                        evidence.append(item)
            expected_code = declaration.get("expected_code")
            matched = bool(evidence) and (
                expected_code is None or any(item.get("business_code") == expected_code for item in evidence)
            )
            if declaration.get("kind") == "empty_terms" and not matched:
                target_session = declaration.get("session_id")
                target_turn = declaration.get("turn_no")
                empty_turns = []
                for key, feature in self.features.items():
                    if not feature.get("empty_terms"):
                        continue
                    if target_session is not None and key[0] != target_session:
                        continue
                    if target_turn is not None and key[1] != target_turn:
                        continue
                    empty_turns.append({
                        "session_id": key[0],
                        "turn_no": key[1],
                        "term_counts": feature.get("term_counts", {}),
                        "empty_terms": True,
                    })
                if empty_turns:
                    matched = True
                    evidence = empty_turns
            if declaration.get("kind") == "threshold_boundary" and not matched:
                if boundary_alerts:
                    matched = True
                    evidence = boundary_alerts
            hits.append({
                "id": identifier,
                "kind": declaration.get("kind", "declared"),
                "hit": bool(matched),
                "evidence": evidence,
            })
        for declaration in self.injections.get("force_exceptions", []):
            identifier = str(declaration.get("id"))
            evidence = failure_ids.get(identifier, [])
            hits.append({
                "id": identifier,
                "kind": declaration.get("kind", "forced_exception"),
                "hit": bool(evidence),
                "evidence": evidence,
            })
        for probe in self.injections.get("probes", []):
            identifier = str(probe.get("id"))
            kind = probe.get("kind")
            if kind == "illegal_weight":
                candidate = dict(self.weights)
                patch = probe.get("patch", {})
                if isinstance(patch, dict):
                    candidate.update(patch)
                try:
                    validate_weights(candidate)
                except ConfigError as exc:
                    hits.append({
                        "id": identifier,
                        "kind": kind,
                        "hit": exc.code == "E_CONFIG_INVALID",
                        "evidence": {"business_code": "E_WEIGHT_INVALID", "validation_code": exc.code, "message": exc.message},
                    })
                else:
                    hits.append({"id": identifier, "kind": kind, "hit": False, "evidence": {}})
            elif kind == "term_reference":
                term = probe.get("term")
                missing = isinstance(term, str) and term not in self.vocab.terms
                hits.append({
                    "id": identifier,
                    "kind": kind,
                    "hit": missing,
                    "evidence": {
                        "business_code": "E_TERM_REFERENCE" if missing else None,
                        "term": term,
                        "term_exists": not missing,
                    },
                })
            elif kind == "threshold_boundary":
                hits.append({
                    "id": identifier,
                    "kind": kind,
                    "hit": bool(boundary_alerts),
                    "evidence": boundary_alerts,
                })
            else:
                hits.append({"id": identifier, "kind": kind, "hit": False, "evidence": {}})
        return hits


def _serialize_turn_keys(keys: Iterable[tuple[str, int]]) -> list[dict[str, Any]]:
    return [
        {"session_id": session_id, "turn_no": turn_no}
        for session_id, turn_no in sorted(keys)
    ]


def _serialize_window_keys(keys: Iterable[tuple[str, int]]) -> list[dict[str, Any]]:
    return [
        {"session_id": session_id, "start_turn": start_turn}
        for session_id, start_turn in sorted(keys)
    ]


def _turn_key_from_window_failure(item: dict[str, Any]) -> tuple[str, int]:
    return str(item.get("session_id")), int(item.get("window_start") or 0)


def _counts(
    features: dict[tuple[str, int], dict[str, Any]] | Iterable[dict[str, Any]],
    windows: list[dict[str, Any]],
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    feature_values = features.values() if isinstance(features, dict) else list(features)
    sessions = {str(item["session_id"]) for item in feature_values}
    status_counts = Counter(item.get("status") for item in windows)
    return {
        "sessions": len(sessions),
        "turns": sum(1 for _ in feature_values),
        "windows": len(windows),
        "windows_full": status_counts.get("full", 0),
        "windows_skipped": status_counts.get("skipped", 0),
        "windows_error": status_counts.get("error", 0),
        "alerts": sum(1 for item in windows if item.get("alert")),
        "boundary_alerts": sum(
            1 for item in windows if item.get("boundary_equal_threshold")
        ),
        "failures": len(failures),
    }


def aggregate_invalidation(history: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "turn": {"batches": 0, "operations": 0, "affected_turns": 0, "affected_windows": 0},
        "vocab": {"batches": 0, "operations": 0, "affected_turns": 0, "affected_windows": 0},
        "parameter": {"batches": 0, "changed": 0, "affected_windows": 0},
    }
    union_windows: set[tuple[str, int]] = set()
    for item in history:
        turn_counts = item["source_counts"]["turn"]
        vocab_counts = item["source_counts"]["vocab"]
        parameter_counts = item["source_counts"]["parameter"]
        if turn_counts["operations"]:
            totals["turn"]["batches"] += 1
        if vocab_counts["operations"]:
            totals["vocab"]["batches"] += 1
        if parameter_counts["changed"]:
            totals["parameter"]["batches"] += 1
        totals["turn"]["operations"] += turn_counts["operations"]
        totals["turn"]["affected_turns"] += turn_counts["affected_turns"]
        totals["turn"]["affected_windows"] += turn_counts["affected_windows"]
        totals["vocab"]["operations"] += vocab_counts["operations"]
        totals["vocab"]["affected_turns"] += vocab_counts["affected_turns"]
        totals["vocab"]["affected_windows"] += vocab_counts["affected_windows"]
        totals["parameter"]["changed"] += int(bool(parameter_counts["changed"]))
        totals["parameter"]["affected_windows"] += parameter_counts["affected_windows"]
        for section in ("turn", "vocab", "parameter"):
            for window in item[section]["affected_windows"]:
                union_windows.add((window["session_id"], window["start_turn"]))
    totals["union_affected_windows"] = len(union_windows)
    totals["features_recomputed"] = sum(
        int(item.get("feature_recompute_count", 0)) for item in history
    )
    totals["distributions_recomputed"] = sum(
        int(item.get("distribution_recompute_count", 0)) for item in history
    )
    totals["scoring_recomputed"] = sum(
        int(item.get("scoring_recompute_count", 0)) for item in history
    )
    return totals


def run_workbench(**kwargs: Any) -> dict[str, Any]:
    engine = WorkbenchEngine(**kwargs)
    return engine.run()
