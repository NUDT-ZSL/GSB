"""End-to-end deterministic feature, scoring, fusion, cache, and snapshot pipeline."""

from __future__ import annotations

import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .adaptive import select_strategy
from .audio import AudioScan, scan_audio_directory
from .cache import ArtifactCache
from .constants import (
    E_INTERNAL,
    E_INVALID_GRANULARITY,
    E_MISSING_AUDIO,
    E_MISSING_PREDECESSOR,
    FINE_EMOTIONS,
    FINE_TO_COARSE,
    REPORT_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
)
from .dialogue import load_script, resolve_contexts
from .errors import BusinessError, GracefulStop
from .features import extract_audio_features
from .fusion import fuse_turn
from .model import centroid_parameters, logistic_parameters
from .scoring import score_segment
from .snapshot import SnapshotManager
from .util import artifact_digest, canonicalize, stable_hash


PHASE_ORDER = {"scan": 0, "audio": 1, "features": 2, "scores": 3, "fusion": 4, "internal": 5}


class Pipeline:
    def __init__(
        self,
        *,
        samples_dir: Path,
        script_path: Path,
        config: dict[str, Any],
        injections: dict[str, Any],
        state_dir: Path,
        mode: str = "incremental",
        stop_event: threading.Event | None = None,
    ):
        if mode not in {"incremental", "full"}:
            raise ValueError("mode must be incremental or full")
        self.samples_dir = samples_dir
        self.script_path = script_path
        self.config = config
        self.injections = injections
        self.state_dir = state_dir
        self.mode = mode
        self.stop_event = stop_event or threading.Event()
        self.cache = ArtifactCache(state_dir / "cache", enabled=mode != "full")
        self.failures: dict[tuple, dict] = {}
        self.injection_hits: dict[str, dict] = {}
        self.feature_artifacts: dict[str, dict] = {}
        self.score_artifacts: dict[str, dict] = {}
        self.fusion_artifacts: dict[str, dict] = {}
        self.interrupted_stage = ""
        self.snapshots: SnapshotManager | None = None
        self.decision: dict[str, Any] = {}
        self.scans: list[AudioScan] = []
        self.script: dict[str, Any] = {}
        self.scan_by_name: dict[str, AudioScan] = {}
        self.scores_by_filename: dict[str, dict] = {}
        self.injections_list = list(injections.get("injections", []))
        self.stage_config_hashes: dict[str, str] = {}
        self.stage_configs: dict[str, dict] = {}
        self.resume_cursors: dict[str, int] = {}

    # ------------------------------------------------------------------ setup
    def _record_failure_dict(self, failure: dict[str, Any]) -> None:
        failure = canonicalize(failure)
        key = (
            failure.get("phase", ""),
            failure.get("filename", ""),
            failure.get("turn_id", ""),
            failure.get("code", ""),
            failure.get("injection_id", ""),
        )
        self.failures.setdefault(key, failure)

    def _record_failure(self, failure: dict[str, Any]) -> None:
        self._record_failure_dict(failure)

    def _record_error(
        self,
        error: Exception,
        *,
        phase: str,
        filename: str = "",
        turn_id: str = "",
    ) -> None:
        if isinstance(error, BusinessError):
            failure = error.to_failure()
            failure["filename"] = failure.get("filename") or filename
            failure["turn_id"] = failure.get("turn_id") or turn_id
            if failure.get("injection_id"):
                self._record_injection_hit(
                    str(failure["injection_id"]),
                    phase=str(failure.get("phase") or phase),
                    target=failure.get("filename") or turn_id,
                    observed=error.code,
                )
        else:
            failure = {
                "code": E_INTERNAL,
                "phase": phase,
                "filename": filename,
                "turn_id": turn_id,
                "exception_type": type(error).__name__,
                "message": str(error),
                "injection_id": "",
            }
        self._record_failure(failure)

    def _apply_failure(self, failure: dict[str, Any], *, phase: str) -> None:
        self._record_failure(failure)
        injection_id = str(failure.get("injection_id", ""))
        if injection_id:
            self._record_injection_hit(
                injection_id,
                phase=str(failure.get("phase") or phase),
                target=failure.get("filename", "") or failure.get("turn_id", ""),
                observed=str(failure.get("code", "")),
            )

    def _record_injection_hit(
        self,
        injection_id: str,
        *,
        phase: str,
        target: str,
        observed: str,
    ) -> None:
        if not injection_id:
            return
        item = next((entry for entry in self.injections_list if entry["id"] == injection_id), None)
        if item is None:
            return
        self.injection_hits[injection_id] = {
            "id": injection_id,
            "kind": item["kind"],
            "phase": phase,
            "target": target,
            "observed": observed,
        }

    def _relevant_injections(self, target: str, kinds: set[str]) -> list[dict]:
        return [
            item
            for item in self.injections_list
            if item.get("target") == target and item.get("kind") in kinds
        ]

    # ----------------------------------------------------------------- stages
    def _stage_digest(self, stage: str) -> str:
        if stage == "features":
            results = self.feature_artifacts
        elif stage == "scores":
            results = self.score_artifacts
        else:
            results = self.fusion_artifacts
        payload = {
            "stage": stage,
            "results": [
                {"task_id": task_id, "digest": artifact_digest(artifact)}
                for task_id, artifact in sorted(results.items(), key=lambda item: (item[0].lower(), item[0]))
            ],
            "failures": sorted(
                [
                    item
                    for item in self.failures.values()
                    if item.get("phase") == stage
                ],
                key=lambda item: (
                    item.get("filename", "").lower(),
                    item.get("filename", ""),
                    item.get("turn_id", ""),
                    item.get("code", ""),
                ),
            ),
        }
        return stable_hash(payload)

    def _commit_stage_snapshot(
        self,
        stage: str,
        processed: set[str],
        *,
        status: str = "committed",
    ) -> None:
        assert self.snapshots is not None
        self.snapshots.save(
            stage,
            processed=sorted(processed, key=lambda value: (value.lower(), value)),
            artifact_digest=self._stage_digest(stage),
            status=status,
        )

    def _run_tasks(
        self,
        stage: str,
        items: list[dict[str, Any]],
        worker: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        """Run cacheable tasks concurrently while committing stable checkpoints."""
        snapshot = self.snapshots.load(stage) if self.snapshots else None
        resumed = set(snapshot.get("processed", [])) if snapshot else set()
        self.resume_cursors[stage] = len(resumed)
        processed: set[str] = set()
        pending: list[dict[str, Any]] = []
        for item in items:
            artifact = self.cache.get(stage, item["cache_key"], item["task_id"])
            if artifact is not None:
                if "__failure__" in artifact:
                    self._apply_failure(artifact["__failure__"], phase=stage)
                else:
                    self._store_artifact(stage, item["task_id"], artifact)
                processed.add(item["task_id"])
            else:
                pending.append(item)
        if self.stop_event.is_set():
            self.interrupted_stage = stage
            self._commit_stage_snapshot(stage, processed, status="interrupted")
            raise GracefulStop()
        batch_size = max(int(self.config["concurrency"]), int(self.config["snapshot_interval"]))
        if not pending:
            self._commit_stage_snapshot(stage, processed)
            return
        with ThreadPoolExecutor(max_workers=int(self.config["concurrency"])) as executor:
            for start in range(0, len(pending), batch_size):
                if self.stop_event.is_set():
                    self.interrupted_stage = stage
                    self._commit_stage_snapshot(stage, processed, status="interrupted")
                    raise GracefulStop()
                batch = pending[start : start + batch_size]
                future_map = {executor.submit(worker, item["payload"]): item for item in batch}
                completed: list[tuple[int, dict, dict[str, Any] | None, Exception | None]] = []
                for future, item in future_map.items():
                    try:
                        artifact = future.result()
                        completed.append((item["order"], item, artifact, None))
                    except Exception as exc:  # isolated per task
                        completed.append((item["order"], item, None, exc))
                completed.sort(key=lambda row: row[0])
                for _, item, artifact, error in completed:
                    if error is not None:
                        self._record_error(
                            error,
                            phase=stage,
                            filename=item.get("filename", ""),
                            turn_id=item.get("turn_id", ""),
                        )
                        if isinstance(error, BusinessError):
                            failure = error.to_failure()
                            failure["filename"] = failure.get("filename") or item.get("filename", "")
                            failure["turn_id"] = failure.get("turn_id") or item.get("turn_id", "")
                            self.cache.put(
                                stage,
                                item["cache_key"],
                                item["task_id"],
                                {"__failure__": failure},
                                config_fingerprint=self.stage_config_hashes[stage],
                                scale_tier=self.decision["metrics"]["tier"],
                                upstream_digest=item["upstream_digest"],
                                injection_fingerprint=self.injections["injection_fingerprint"],
                            )
                    else:
                        assert artifact is not None
                        artifact = self.cache.put(
                            stage,
                            item["cache_key"],
                            item["task_id"],
                            artifact,
                            config_fingerprint=self.stage_config_hashes[stage],
                            scale_tier=self.decision["metrics"]["tier"],
                            upstream_digest=item["upstream_digest"],
                            injection_fingerprint=self.injections["injection_fingerprint"],
                        )
                        self._store_artifact(stage, item["task_id"], artifact)
                    processed.add(item["task_id"])
                self._commit_stage_snapshot(stage, processed)
                if self.stop_event.is_set():
                    self.interrupted_stage = stage
                    self._commit_stage_snapshot(stage, processed, status="interrupted")
                    raise GracefulStop()
        self._commit_stage_snapshot(stage, processed)

    def _store_artifact(self, stage: str, task_id: str, artifact: dict[str, Any]) -> None:
        if stage == "features":
            self.feature_artifacts[task_id] = artifact
        elif stage == "scores":
            self.score_artifacts[task_id] = artifact
            self.scores_by_filename[artifact["filename"]] = artifact
        elif stage == "fusion":
            self.fusion_artifacts[task_id] = artifact
        else:  # pragma: no cover
            raise AssertionError(stage)

    def _feature_stage(self) -> None:
        items = []
        effective = list(self.decision["effective"]["feature_names"])
        config_fingerprint = self.stage_config_hashes["features"]
        for order, scan in enumerate(self.scans):
            if not scan.valid:
                continue
            relevant = self._relevant_injections(
                scan.filename, {"nonfinite_sample", "zero_duration", "bad_audio_format"}
            )
            cache_key = stable_hash(
                {
                    "stage": "features",
                    "filename": scan.filename,
                    "content_sha256": scan.content_sha256,
                    "config_fingerprint": config_fingerprint,
                    "relevant_injections": relevant,
                }
            )
            items.append(
                {
                    "order": order,
                    "task_id": scan.filename,
                    "filename": scan.filename,
                    "cache_key": cache_key,
                    "upstream_digest": scan.content_sha256,
                    "payload": {
                        "scan": scan,
                        "effective_features": effective,
                        "frame_length_ms": self.config["frame_length_ms"],
                        "frame_shift_ms": self.config["frame_shift_ms"],
                    },
                }
            )

        def worker(payload: dict[str, Any]) -> dict[str, Any]:
            return extract_audio_features(
                payload["scan"],
                frame_length_ms=payload["frame_length_ms"],
                frame_shift_ms=payload["frame_shift_ms"],
                effective_features=payload["effective_features"],
                injections=self.injections_list,
            )

        self._run_tasks("features", items, worker)

    def _score_stage(self) -> None:
        items = []
        granularity = self.config["emotion_granularity"]
        classifier = self.decision["effective"]["classifier"]
        config_fingerprint = self.stage_config_hashes["scores"]
        feature_names = tuple(self.decision["effective"]["feature_names"])
        # Prewarm the deterministic model outside worker threads.
        if classifier == "weighted_logistic_regression":
            logistic_parameters(granularity, feature_names)
        else:
            centroid_parameters(granularity, feature_names)
        for order, filename in enumerate(sorted(self.feature_artifacts, key=lambda value: (value.lower(), value))):
            artifact = self.feature_artifacts[filename]
            relevant = self._relevant_injections(filename, {"invalid_granularity"})
            cache_key = stable_hash(
                {
                    "stage": "scores",
                    "upstream_digest": artifact_digest(artifact),
                    "config_fingerprint": config_fingerprint,
                    "relevant_injections": relevant,
                }
            )
            items.append(
                {
                    "order": order,
                    "task_id": filename,
                    "filename": filename,
                    "cache_key": cache_key,
                    "upstream_digest": artifact_digest(artifact),
                    "payload": {
                        "artifact": artifact,
                        "injection": relevant[0] if relevant else None,
                    },
                }
            )

        def worker(payload: dict[str, Any]) -> dict[str, Any]:
            injection = payload.get("injection")
            local_granularity = granularity
            injection_id = ""
            if injection is not None:
                injection_id = str(injection.get("id", ""))
                local_granularity = str(injection.get("value", "__invalid__"))
            return score_segment(
                payload["artifact"],
                granularity=local_granularity,
                classifier=classifier,
                boundary_margin=float(self.config["boundary_margin"]),
                reference_precision=float(self.config["reference_precision"]),
                injection_id=injection_id,
            )

        self._run_tasks("scores", items, worker)

    def _fusion_stage(self) -> None:
        effective_window = int(self.decision["effective"]["fusion_window"])
        fusion_config = self.config["fusion"]
        forced_missing = {
            item["target"]
            for item in self.injections_list
            if item["kind"] == "missing_predecessor"
        }
        contexts = resolve_contexts(
            self.script,
            window=effective_window,
            forced_missing_targets=forced_missing,
            rule=fusion_config["missing_predecessor_rule"],
        )
        items = []
        config_fingerprint = self.stage_config_hashes["fusion"]
        for order, context in enumerate(contexts):
            turn = context["turn"]
            if turn["turn_id"] in forced_missing and turn["turn_id"] in {
                item["target"] for item in self.injections_list if item["kind"] == "missing_predecessor"
            }:
                injection = next(
                    item
                    for item in self.injections_list
                    if item["kind"] == "missing_predecessor" and item["target"] == turn["turn_id"]
                )
                self._record_injection_hit(
                    injection["id"],
                    phase="fusion",
                    target=turn["turn_id"],
                    observed="missing_predecessor_fallback",
                )
            if context["missing_predecessor"]:
                injection_id = ""
                matching = [
                    item
                    for item in self.injections_list
                    if item["kind"] == "missing_predecessor" and item["target"] == turn["turn_id"]
                ]
                if matching:
                    injection_id = matching[0]["id"]
                    self._record_injection_hit(
                        injection_id,
                        phase="fusion",
                        target=turn["turn_id"],
                        observed=context["fallback_source"],
                    )
                self._record_failure(
                    {
                        "code": E_MISSING_PREDECESSOR,
                        "phase": "fusion",
                        "filename": turn["audio"],
                        "turn_id": turn["turn_id"],
                        "exception_type": "MissingPredecessorFallback",
                        "message": f"missing predecessor; fallback={context['fallback_source']}",
                        "injection_id": injection_id,
                        "detail": {
                            "fallback_source": context["fallback_source"],
                            "predecessor_ids": context["predecessor_ids"],
                        },
                    }
                )
            if turn["audio"] not in self.scan_by_name:
                self._record_failure(
                    {
                        "code": E_MISSING_AUDIO,
                        "phase": "fusion",
                        "filename": turn["audio"],
                        "turn_id": turn["turn_id"],
                        "exception_type": "MissingAudioReference",
                        "message": "dialogue turn references an audio file not present in the sample directory",
                        "injection_id": "",
                    }
                )
            context_entries = []
            context_signature = []
            for chain_turn in context["chain"]:
                score_artifact = self.scores_by_filename.get(chain_turn["audio"])
                context_entries.append({"turn": chain_turn, "score": score_artifact})
                context_signature.append(
                    {
                        "turn_id": chain_turn["turn_id"],
                        "order": chain_turn["order"],
                        "speaker": chain_turn["speaker"],
                        "audio": chain_turn["audio"],
                        "score_digest": artifact_digest(score_artifact) if score_artifact else None,
                    }
                )
            relevant = self._relevant_injections(turn["turn_id"], {"missing_predecessor"})
            current_signature = {
                "turn_id": turn["turn_id"],
                "order": turn["order"],
                "speaker": turn["speaker"],
                "audio": turn["audio"],
                "intent": turn["intent"],
                "prev_turn": turn["prev_turn"],
            }
            cache_key = stable_hash(
                {
                    "stage": "fusion",
                    "config_fingerprint": config_fingerprint,
                    "current_turn": current_signature,
                    "context": context_signature,
                    "fallback_source": context["fallback_source"],
                    "missing_predecessor": context["missing_predecessor"],
                    "relevant_injections": relevant,
                }
            )
            upstream = stable_hash(
                {"context": context_signature, "fallback": context["fallback_source"]}
            )
            items.append(
                {
                    "order": order,
                    "task_id": turn["turn_id"],
                    "filename": turn["audio"],
                    "turn_id": turn["turn_id"],
                    "cache_key": cache_key,
                    "upstream_digest": upstream,
                    "payload": {
                        "turn": turn,
                        "context_entries": context_entries,
                        "fallback_source": context["fallback_source"],
                    },
                }
            )

        def worker(payload: dict[str, Any]) -> dict[str, Any]:
            return fuse_turn(
                payload["turn"],
                payload["context_entries"],
                decay=float(fusion_config["decay"]),
                same_speaker_weight=float(fusion_config["same_speaker_weight"]),
                cross_speaker_weight=float(fusion_config["cross_speaker_weight"]),
                fallback_source=payload["fallback_source"],
                boundary_margin=float(self.config["boundary_margin"]),
            )

        self._run_tasks("fusion", items, worker)

    # ------------------------------------------------------------------ report
    def _failure_list(self) -> list[dict[str, Any]]:
        return sorted(
            self.failures.values(),
            key=lambda item: (
                PHASE_ORDER.get(item.get("phase", ""), 99),
                item.get("filename", "").lower(),
                item.get("filename", ""),
                item.get("turn_id", ""),
                item.get("code", ""),
                item.get("injection_id", ""),
            ),
        )

    def _turn_by_audio(self) -> dict[str, dict[str, Any]]:
        mapping: dict[str, dict[str, Any]] = {}
        for turn in self.script.get("turns", []):
            mapping.setdefault(turn["audio"], turn)
        return mapping

    def _segment_reports(self, *, interrupted: bool) -> list[dict[str, Any]]:
        turn_by_audio = self._turn_by_audio()
        reports = []
        for scan in self.scans:
            feature = self.feature_artifacts.get(scan.filename)
            score = self.scores_by_filename.get(scan.filename)
            turn = turn_by_audio.get(scan.filename)
            filename_failures = [item for item in self._failure_list() if item.get("filename") == scan.filename]
            if not scan.valid:
                status = "failed"
            elif feature is None:
                status = "pending" if interrupted and not any(f["phase"] in {"features", "scores"} for f in filename_failures) else "failed"
            elif score is None:
                status = "partial"
            else:
                status = "ok"
            report = {
                "filename": scan.filename,
                "content_sha256": scan.content_sha256,
                "status": status,
                "sample_rate": scan.header.sample_rate if scan.header else None,
                "channels": scan.header.channels if scan.header else None,
                "sample_width": scan.header.sample_width if scan.header else None,
                "duration_sec": scan.duration_sec,
                "frame_length_samples": feature.get("frame_length_samples") if feature else None,
                "frame_shift_samples": feature.get("frame_shift_samples") if feature else None,
                "frame_count": feature.get("frame_count") if feature else 0,
                "frame_feature_digest": artifact_digest(feature.get("frames", [])) if feature else "",
                "frame_features": feature.get("frames", []) if feature else [],
                "all_features": feature.get("all_segment_features", {}) if feature else {},
                "effective_features": feature.get("effective_features", []) if feature else [],
                "feature_vector": feature.get("segment_feature_vector", []) if feature else [],
                "scores": score.get("scores", {}) if score else {},
                "predicted_emotion": score.get("predicted_emotion", "") if score else "",
                "classifier": score.get("classifier", "") if score else "",
                "classifier_path": score.get("classifier_path", "") if score else "",
                "boundary": score.get("boundary", False) if score else False,
                "score_margin": score.get("score_margin", 0.0) if score else 0.0,
                "reference_precision": score.get("reference_precision", 0.0) if score else 0.0,
                "truth_emotion": turn.get("emotion_label", "") if turn else "",
                "turn_id": turn.get("turn_id", "") if turn else "",
                "failure_codes": sorted({failure["code"] for failure in filename_failures}),
            }
            reports.append(canonicalize(report))
        return reports

    def _dialog_reports(self) -> list[dict[str, Any]]:
        return [
            self.fusion_artifacts[turn_id]
            for turn_id in sorted(
                self.fusion_artifacts,
                key=lambda value: (
                    self.fusion_artifacts[value]["order"],
                    self.fusion_artifacts[value]["turn_id"],
                ),
            )
        ]

    def _accuracy(self, segments: list[dict[str, Any]]) -> dict[str, Any]:
        correct = 0
        total = 0
        for segment in segments:
            truth = segment.get("truth_emotion", "")
            predicted = segment.get("predicted_emotion", "")
            if not truth or not predicted or truth not in FINE_EMOTIONS:
                continue
            if self.config["emotion_granularity"] == "coarse":
                truth = FINE_TO_COARSE[truth]
            total += 1
            if truth == predicted:
                correct += 1
        return {
            "evaluated_segments": total,
            "correct": correct,
            "accuracy": round(correct / total, 6) if total else 0.0,
        }

    def _build_report(self, *, status: str) -> dict[str, Any]:
        segments = self._segment_reports(interrupted=status == "interrupted")
        dialog = self._dialog_reports()
        failures = self._failure_list()
        injection_hits = [self.injection_hits[key] for key in sorted(self.injection_hits)]
        score_path_counts: dict[str, int] = defaultdict(int)
        for segment in segments:
            if segment["classifier_path"]:
                score_path_counts[segment["classifier_path"]] += 1
        total_frames = sum(int(segment["frame_count"] or 0) for segment in segments)
        boundary_segments = sum(1 for segment in segments if segment["boundary"])
        fallback_turns = sum(1 for turn in dialog if turn["missing_predecessor"])
        effective = self.decision["effective"]
        diagnostics = {
            "segment_count": len(self.scans),
            "valid_audio_count": sum(1 for scan in self.scans if scan.valid),
            "feature_ready_count": len(self.feature_artifacts),
            "scored_count": len(self.score_artifacts),
            "frame_count_total": total_frames,
            "turn_count": len(self.script.get("turns", [])),
            "fused_turn_count": len(dialog),
            "failure_count": len(failures),
            "injection_hit_count": len(injection_hits),
            "boundary_segment_count": boundary_segments,
            "fallback_turn_count": fallback_turns,
            "score_path_counts": dict(sorted(score_path_counts.items())),
            "accuracy": self._accuracy(segments),
            "scale_tier": self.decision["metrics"]["tier"],
            "effective_features": effective["feature_names"],
            "effective_feature_count": len(effective["feature_names"]),
            "effective_classifier": effective["classifier"],
            "effective_fusion_window": effective["fusion_window"],
            "decision_fingerprint": stable_hash(self.decision),
            "algorithm_fingerprint": stable_hash(
                {
                    "effective": effective,
                    "config_fingerprint": self.config["config_fingerprint"],
                    "report_version": REPORT_VERSION,
                }
            ),
        }
        snapshot_summary = (
            self.snapshots.summary()
            if self.snapshots is not None
            else {"versions": {stage: SNAPSHOT_SCHEMA_VERSION for stage in ("features", "scores", "fusion")}, "committed": {}, "errors": []}
        )
        report = {
            "report_version": REPORT_VERSION,
            "status": status,
            "config": self.config,
            "config_fingerprint": self.config["config_fingerprint"],
            "injection_fingerprint": self.injections["injection_fingerprint"],
            "dialogue": {
                "dialogue_id": self.script.get("dialogue_id", ""),
                "script_fingerprint": self.script.get("script_fingerprint", ""),
                "turn_count": len(self.script.get("turns", [])),
            },
            "scale_decision": self.decision,
            "segments": segments,
            "dialog_turns": dialog,
            "diagnostics": diagnostics,
            "failures": failures,
            "injections": {
                "configured": self.injections_list,
                "hits": injection_hits,
            },
            "snapshot_version": SNAPSHOT_SCHEMA_VERSION,
            "snapshots": snapshot_summary,
            "cache": self.cache.summary(),
            "runtime": {
                "mode": self.mode,
                "interrupted_stage": self.interrupted_stage,
                "resume_cursors": self.resume_cursors,
            },
        }
        if status == "interrupted":
            report["interrupted_stage"] = self.interrupted_stage
        return attach_report_fingerprint_placeholder(report)

    # -------------------------------------------------------------------- run
    def run(self) -> dict[str, Any]:
        self.scans = scan_audio_directory(self.samples_dir)
        self.scan_by_name = {scan.filename: scan for scan in self.scans}
        self.script = load_script(self.script_path)
        for scan in self.scans:
            if not scan.valid:
                matching = [
                    item
                    for item in self.injections_list
                    if item["kind"] == "bad_audio_format" and item["target"] == scan.filename
                ]
                injection_id = matching[0]["id"] if matching else ""
                if injection_id:
                    self._record_injection_hit(
                        injection_id, phase="scan", target=scan.filename, observed=scan.error_code
                    )
                self._record_failure(
                    {
                        "code": scan.error_code,
                        "phase": "scan",
                        "filename": scan.filename,
                        "turn_id": "",
                        "exception_type": "AudioFormatError",
                        "message": scan.error_message,
                        "injection_id": injection_id,
                    }
                )
        self.decision = select_strategy(
            segment_count=len(self.scans),
            valid_durations=[scan.duration_sec for scan in self.scans if scan.valid],
            turn_count=len(self.script["turns"]),
            granularity=self.config["emotion_granularity"],
            config=self.config,
        )
        tier = self.decision["metrics"]["tier"]
        effective = self.decision["effective"]
        self.stage_configs = {
            "features": {
                "frame_length_ms": self.config["frame_length_ms"],
                "frame_shift_ms": self.config["frame_shift_ms"],
                "acoustic_feature_set": self.config["acoustic_feature_set"],
                "feature_dimension_limit": self.config["feature_dimension_limit"],
                "effective_features": effective["feature_names"],
                "scale_tier": tier,
            },
            "scores": {
                "emotion_granularity": self.config["emotion_granularity"],
                "classifier": effective["classifier"],
                "effective_features": effective["feature_names"],
                "boundary_margin": self.config["boundary_margin"],
                "reference_precision": self.config["reference_precision"],
                "scale_tier": tier,
            },
            "fusion": {
                "window": effective["fusion_window"],
                "decay": self.config["fusion"]["decay"],
                "same_speaker_weight": self.config["fusion"]["same_speaker_weight"],
                "cross_speaker_weight": self.config["fusion"]["cross_speaker_weight"],
                "missing_predecessor_rule": self.config["fusion"]["missing_predecessor_rule"],
                "emotion_granularity": self.config["emotion_granularity"],
                "boundary_margin": self.config["boundary_margin"],
                "scale_tier": tier,
            },
        }
        self.stage_config_hashes = {
            stage: stable_hash(config) for stage, config in self.stage_configs.items()
        }
        self.snapshots = SnapshotManager(
            self.state_dir / "snapshots",
            config_hashes=self.stage_config_hashes,
            injection_fingerprint=self.injections["injection_fingerprint"],
            read_enabled=self.mode != "full",
            write_enabled=True,
        )
        try:
            self._feature_stage()
            self._score_stage()
            self._fusion_stage()
        except GracefulStop:
            return self._build_report(status="interrupted")
        return self._build_report(status="completed")


def attach_report_fingerprint_placeholder(report: dict[str, Any]) -> dict[str, Any]:
    # Imported lazily to keep the stage implementation self-contained.
    from .reporting import attach_report_fingerprint

    return attach_report_fingerprint(report)
