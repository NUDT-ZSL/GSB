from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .adjudicate import (
    enforce_conflict_threshold,
    index_boundary_truth,
    judge_candidate,
    judgments_to_ledger_rows,
)
from .audio import extract_acoustic_record
from .cache import ContentCache, build_cache_key
from .canonical import (
    bytes_sha256,
    canonical_hash,
    file_sha256,
    read_json,
    read_jsonl,
    sweep_temp_files,
    write_json_atomic,
    write_jsonl_atomic,
    write_text_atomic,
)
from .config import Config
from .errors import E_LEDGER_MISMATCH, DiagnosticError
from .errorspec import (
    build_candidate_deviation,
    build_frame_deviation,
    check_tolerances,
    summarize_error_spectrum,
    summarize_truth_observation_details,
    truth_observation_details,
)
from .precision import PrecisionContext
from .resources import ResourceGovernor
from .state import STAGES, StateStore
from .trees import infer_candidate, load_decision_tree
from .validation import validate_boundary_references, validate_revision_watermark


EXTRACTOR_VERSION = "acoustic-v1"
ADJUDICATOR_VERSION = "adjudication-v2"
ERROR_SPECTRUM_VERSION = "error-spectrum-v2"


class DiagnosticPipeline:
    def __init__(
        self,
        sample_dir: str | Path,
        output_dir: str | Path,
        config: Config | None = None,
        *,
        profiles: list[str] | None = None,
        interrupt_at: tuple[str, int] | None = None,
        overrides: dict[str, Any] | None = None,
        change_spec: dict[str, Any] | None = None,
    ):
        self.cfg = config or Config()
        self.cfg.validate()
        self.sample_dir = Path(sample_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir = self.output_dir
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        sweep_temp_files(self.output_dir)

        self.overrides = overrides or {}
        self.change_spec = change_spec
        self.tasks = read_jsonl(self.sample_dir / "tasks.jsonl")
        self._apply_task_overrides()
        from .validation import validate_task_records

        validate_task_records(self.tasks)
        self.frame_truth = read_jsonl(self.sample_dir / "frame_truth.jsonl")
        self.boundary_truth = read_jsonl(self.sample_dir / "boundary_truth.jsonl")
        self.truth_by_utterance: dict[str, list[dict[str, Any]]] = {}
        for row in self.frame_truth:
            self.truth_by_utterance.setdefault(row["utterance_id"], []).append(row)
        self.truth_index = index_boundary_truth(self.boundary_truth)

        self.trees = self._load_trees()
        self._apply_rule_overrides()
        requested_profiles = profiles or ["fp64", "fp32", "q15"]
        # float64 is always retained as the reference path.
        self.profiles = list(dict.fromkeys(["fp64", *requested_profiles]))
        self.interrupt_at = interrupt_at

        self.cache = ContentCache(self.output_dir / "cache")
        self.state = StateStore(self.output_dir)
        self.registry_path = self.state.state_dir / "key_registry.json"
        self.registry: dict[str, str] = {}
        if self.registry_path.exists():
            loaded = read_json(self.registry_path)
            if isinstance(loaded, dict):
                self.registry = loaded
        self.watermark_path = self.state.state_dir / "revision_watermark.json"
        self._validate_watermark()

        self.governor = ResourceGovernor(
            max_utterances_per_run=self.cfg.max_utterances_per_run,
            max_resident_frames=self.cfg.max_resident_frames,
            max_cache_entries=self.cfg.max_cache_entries,
            default_prune_threshold=self.cfg.candidate_min_score,
            prune_boost=self.cfg.prune_boost,
        )
        estimated_entries = len(self.tasks) * (1 + len(self.profiles) * 3)
        self.governor.observe_cache_entries(estimated_entries)

        self.feature_keys: dict[str, str] = {}
        self.decision_keys: dict[tuple[str, str], str] = {}
        self.plan: dict[str, Any] = {
            "recomputed_utterance_ids": [],
            "reused_utterance_ids": [],
            "feature_recomputed": [],
            "feature_reused": [],
            "decision_recomputed": [],
            "decision_reused": [],
            "error_recomputed": [],
            "error_reused": [],
            "affected_utterance_ids": self._planned_affected_uids(),
            "degradations": self.governor.degradations,
            "change_spec": change_spec,
        }
        self._pcm_hash_cache: dict[str, str] = {}

    def _apply_task_overrides(self) -> None:
        revisions = self.overrides.get("revision_overrides", {})
        profile_overrides = self.overrides.get("profile_overrides", {})
        for task in self.tasks:
            uid = task["utterance_id"]
            if uid in revisions:
                task["revision"] = int(revisions[uid])
            if uid in profile_overrides:
                task["precision_profile"] = profile_overrides[uid]

    def _load_trees(self) -> dict[str, dict[str, Any]]:
        versions = {"v1", "v2"} if self.cfg.tree_version == "both" else {self.cfg.tree_version}
        trees: dict[str, dict[str, Any]] = {}
        if "v1" in versions:
            trees["v1"] = load_decision_tree(self.sample_dir / "trees" / "v1" / "prosody_tree_v1.json")
        if "v2" in versions:
            trees["v2"] = load_decision_tree(self.sample_dir / "trees" / "v2")
        return trees

    def _apply_rule_overrides(self) -> None:
        overrides = self.overrides.get("rule_overrides", {})
        for rule_id, changes in overrides.items():
            for tree in self.trees.values():
                for rule in tree["rules"]:
                    if rule["rule_id"] == rule_id:
                        for key, value in changes.items():
                            if key == "threshold":
                                rule["conditions"][0]["threshold"] = float(value)
                            else:
                                rule[key] = value

    def _validate_watermark(self) -> None:
        if self.watermark_path.exists():
            watermark = read_json(self.watermark_path)
            validate_revision_watermark(self.tasks, watermark)

    def _planned_affected_uids(self) -> list[str]:
        if not self.change_spec:
            return []
        if self.change_spec.get("type") == "revision":
            return [self.change_spec["utterance_id"]]
        if self.change_spec.get("type") == "profile":
            return [self.change_spec["utterance_id"]]
        if self.change_spec.get("type") == "rule":
            rule_id = self.change_spec["rule_id"]
            kind = self.change_spec.get("change_kind", "confidence")
            affected: set[str] = set()
            for entry in self.cache.index.values():
                if entry.get("stage") != "candidate_decisions":
                    continue
                ids = entry.get("evaluated_rule_ids", []) if kind == "threshold" else entry.get(
                    "matched_rule_ids", []
                )
                if rule_id in ids:
                    affected.add(entry["utterance_id"])
            return sorted(affected)
        return []

    def _maybe_interrupt(self, stage: str, processed_utterances: int) -> None:
        if self.interrupt_at and self.interrupt_at[0] == stage:
            target = int(self.interrupt_at[1])
            if processed_utterances == target:
                sys.stderr.write(
                    f"INTERRUPT_POINT stage={stage} utterances={processed_utterances}; hard exit\n"
                )
                sys.stderr.flush()
                os._exit(37)

    def _save_registry(self) -> None:
        write_json_atomic(self.registry_path, dict(sorted(self.registry.items())))

    def pcm_segment_hash(self, task: dict[str, Any]) -> str:
        speaker_id = task["speaker_id"]
        if speaker_id not in self._pcm_hash_cache:
            pcm_path = self.sample_dir / "audio" / f"{speaker_id}.pcm"
            self._pcm_hash_cache[speaker_id] = file_sha256(pcm_path)
        pcm_path = self.sample_dir / "audio" / f"{speaker_id}.pcm"
        data = pcm_path.read_bytes()
        start = int(task["start_ms"]) * self.cfg.sample_rate // 1000 * 2
        end = int(task["end_ms"]) * self.cfg.sample_rate // 1000 * 2
        return bytes_sha256(data[start:end])

    def _tree_components(self) -> dict[str, Any]:
        from .trees import tree_hash

        return {
            version: {
                "tree_hash": tree_hash(tree),
                "tree_rev": tree["tree_rev"],
                "rule_generation": tree.get("rule_generation"),
                "version_header": tree["version_header"],
            }
            for version, tree in sorted(self.trees.items())
        }

    def _feature_input_hash(self) -> str:
        manifest = read_json(self.sample_dir / "manifest.json")
        return canonical_hash(
            {
                "tasks": self.tasks,
                "config": self.cfg.to_dict(),
                "manifest": manifest,
                "extractor_version": EXTRACTOR_VERSION,
                "prune_threshold": self.governor.candidate_score_threshold,
            }
        )

    def _decision_input_hash(self) -> str:
        return canonical_hash(
            {
                "feature_input": self._feature_input_hash(),
                "trees": self._tree_components(),
                "profiles": self.profiles,
                "overrides": self.overrides,
                "adjudicator_version": ADJUDICATOR_VERSION,
            }
        )

    def _error_input_hash(self) -> str:
        return canonical_hash(
            {
                "decision_input": self._decision_input_hash(),
                "tolerances": self.cfg.error_tolerances,
                "near_threshold_band": self.cfg.near_threshold_band,
                "error_spectrum_version": ERROR_SPECTRUM_VERSION,
            }
        )

    def _report_input_hash(self, artifact_hashes: dict[str, str]) -> str:
        return canonical_hash(
            {
                "error_input": self._error_input_hash(),
                "artifact_hashes": artifact_hashes,
                "plan_shape": {
                    "affected": self.plan["affected_utterance_ids"],
                    "degradations": self.plan["degradations"],
                },
            }
        )

    def _feature_key(self, task: dict[str, Any]) -> str:
        uid = task["utterance_id"]
        truth_rows = self.truth_by_utterance.get(uid, [])
        return build_cache_key(
            stage="acoustic_features",
            utterance_id=uid,
            revision=task.get("revision", 1),
            pcm_segment_sha256=self.pcm_segment_hash(task),
            frame_config={
                "sample_rate": self.cfg.sample_rate,
                "frame_length_ms": self.cfg.frame_length_ms,
                "frame_shift_ms": self.cfg.frame_shift_ms,
            },
            truth_sha256=canonical_hash(truth_rows),
            candidate_min_score=self.cfg.candidate_min_score,
            prune_threshold=self.governor.candidate_score_threshold,
            extractor_version=EXTRACTOR_VERSION,
        )

    def _decision_key(self, task: dict[str, Any], profile: str) -> str:
        uid = task["utterance_id"]
        feature_key = self.feature_keys[uid]
        normal_key = build_cache_key(
            stage="candidate_decisions",
            utterance_id=uid,
            revision=task.get("revision", 1),
            pcm_segment_sha256=self.pcm_segment_hash(task),
            acoustic_feature_key=feature_key,
            upstream_hashes={"acoustic_features": feature_key},
            precision_profile=PrecisionContext(profile).spec_hash_object(),
            task_declared_precision=task.get("precision_profile"),
            tree_versions=self.cfg.tree_version,
            trees=self._tree_components(),
            decision_tree_rev=task.get("decision_tree_rev"),
            rule_generation={
                version: tree.get("rule_generation") for version, tree in self.trees.items()
            },
            adjudicator_version=ADJUDICATOR_VERSION,
        )
        change = self.change_spec or {}
        if change.get("type") == "rule":
            old_key = self.registry.get(f"decision:{uid}:{profile}")
            affected = set(self.plan["affected_utterance_ids"])
            if old_key and uid not in affected and self.cache.has(old_key):
                return old_key
        return normal_key

    def run_feature_stage(self) -> dict[str, Any]:
        stage = "features"
        input_hash = self._feature_input_hash()
        latest = self.state.load_latest(stage)
        if latest and latest.get("stage_inputs_hash") == input_hash:
            for task in self.tasks:
                key = self.registry.get(f"feature:{task['utterance_id']}") or self._feature_key(task)
                self.feature_keys[task["utterance_id"]] = key
                self.plan["feature_reused"].append(task["utterance_id"])
                self.plan["reused_utterance_ids"].append(task["utterance_id"])
            return latest

        records: list[dict[str, Any]] = []
        batch_size = max(1, self.governor.batch_size(len(self.tasks)))
        processed = 0
        for batch_start in range(0, len(self.tasks), batch_size):
            for task in self.tasks[batch_start : batch_start + batch_size]:
                uid = task["utterance_id"]
                key = self._feature_key(task)
                self.feature_keys[uid] = key
                self.registry[f"feature:{uid}"] = key
                if self.cache.has(key):
                    self.plan["feature_reused"].append(uid)
                    self.plan["reused_utterance_ids"].append(uid)
                    record = self.cache.get(key)
                else:
                    pcm_path = self.sample_dir / "audio" / f"{task['speaker_id']}.pcm"
                    record = extract_acoustic_record(
                        task, pcm_path, self.truth_by_utterance.get(uid, []), self.cfg
                    )
                    self.governor.observe_resident_frames(record["frame_count"], uid)
                    record["candidates"] = self.governor.prune_candidates(record["candidates"])
                    self.cache.put(
                        key,
                        record,
                        {
                            "stage": "acoustic_features",
                            "utterance_id": uid,
                            "revision": task.get("revision", 1),
                            "pcm_sha256": record["pcm_segment_sha256"],
                            "frame_count": record["frame_count"],
                        },
                    )
                    self.plan["feature_recomputed"].append(uid)
                    self.plan["recomputed_utterance_ids"].append(uid)
                records.append(record)
                processed += 1
                self._save_cache_index_locked()
                self._save_registry()
                self._maybe_interrupt(stage, processed)

        validate_boundary_references(records, self.boundary_truth)
        frame_rows = [frame for record in records for frame in record["frames"]]
        candidate_rows = [
            {**candidate, "utterance_id": record["utterance_id"], "speaker_id": record["speaker_id"]}
            for record in records
            for candidate in record["candidates"]
        ]
        frame_path = self.artifacts_dir / "frame_features.jsonl"
        candidate_path = self.artifacts_dir / "boundary_candidates.jsonl"
        write_jsonl_atomic(frame_path, frame_rows)
        write_jsonl_atomic(candidate_path, candidate_rows)
        artifact_hashes = {
            "frame_features.jsonl": file_sha256(frame_path),
            "boundary_candidates.jsonl": file_sha256(candidate_path),
        }
        snapshot = self.state.commit_snapshot(
            stage,
            [task["utterance_id"] for task in self.tasks],
            artifact_hashes=artifact_hashes,
            extra={"stage_inputs_hash": input_hash},
        )
        return snapshot

    def _save_cache_index_locked(self) -> None:
        with self.state.commit_lock():
            self.cache.save_index()

    def run_adjudication_stage(self) -> dict[str, Any]:
        stage = "adjudication"
        input_hash = self._decision_input_hash()
        latest = self.state.load_latest(stage)
        if latest and latest.get("stage_inputs_hash") == input_hash:
            for task in self.tasks:
                for profile in self.profiles:
                    key = self.registry.get(f"decision:{task['utterance_id']}:{profile}")
                    if key:
                        self.decision_keys[(task["utterance_id"], profile)] = key
                self.plan["decision_reused"].append(task["utterance_id"])
                self.plan["reused_utterance_ids"].append(task["utterance_id"])
            return latest

        all_judgments: list[dict[str, Any]] = []
        processed = 0
        for task in self.tasks:
            uid = task["utterance_id"]
            feature_key = self.registry.get(f"feature:{uid}") or self._feature_key(task)
            self.feature_keys[uid] = feature_key
            feature_record = self.cache.get(feature_key)
            for profile in self.profiles:
                key = self._decision_key(task, profile)
                self.decision_keys[(uid, profile)] = key
                self.registry[f"decision:{uid}:{profile}"] = key
                if self.cache.has(key):
                    payload = self.cache.get(key)
                    judgments = payload["judgments"]
                    self.plan["decision_reused"].append(uid)
                    self.plan["reused_utterance_ids"].append(uid)
                else:
                    judgments = [
                        judge_candidate(
                            task,
                            candidate,
                            self.trees,
                            self.truth_index,
                            profile,
                            self.cfg,
                        )
                        for candidate in feature_record["candidates"]
                    ]
                    evaluated = sorted(
                        {
                            trace["rule_id"]
                            for judgment in judgments
                            for version in ("v1", "v2")
                            if judgment.get(version)
                            for trace in judgment[version]["decision_path"]
                        }
                    )
                    matched = sorted(
                        {
                            rule_id
                            for judgment in judgments
                            for version in ("v1", "v2")
                            if judgment.get(version)
                            for rule_id in judgment[version]["matching_rule_ids"]
                        }
                    )
                    self.cache.put(
                        key,
                        {"judgments": judgments},
                        {
                            "stage": "candidate_decisions",
                            "utterance_id": uid,
                            "precision_profile": profile,
                            "revision": task.get("revision", 1),
                            "tree_versions": sorted(self.trees),
                            "tree_components": self._tree_components(),
                            "evaluated_rule_ids": evaluated,
                            "matched_rule_ids": matched,
                        },
                    )
                    self.plan["decision_recomputed"].append(uid)
                    self.plan["recomputed_utterance_ids"].append(uid)
                all_judgments.extend(judgments)
            processed += 1
            self._save_cache_index_locked()
            self._save_registry()
            self._maybe_interrupt(stage, processed)

        conflicts = enforce_conflict_threshold(all_judgments, self.cfg.conflict_threshold)
        judgment_path = self.artifacts_dir / "candidate_judgments.jsonl"
        ledger_path = self.artifacts_dir / "annotation_ledger.jsonl"
        write_jsonl_atomic(judgment_path, all_judgments)
        ledger_rows = judgments_to_ledger_rows(all_judgments)
        write_jsonl_atomic(ledger_path, ledger_rows)
        reread_judgments = read_jsonl(judgment_path)
        recomputed_rows = judgments_to_ledger_rows(reread_judgments)
        if canonical_hash(recomputed_rows) != canonical_hash(ledger_rows):
            raise DiagnosticError(
                E_LEDGER_MISMATCH,
                "ledger could not be recomputed byte-consistently from candidate details",
                field="annotation_ledger.jsonl",
            )
        artifact_hashes = {
            "candidate_judgments.jsonl": file_sha256(judgment_path),
            "annotation_ledger.jsonl": file_sha256(ledger_path),
        }
        snapshot = self.state.commit_snapshot(
            stage,
            [task["utterance_id"] for task in self.tasks],
            artifact_hashes=artifact_hashes,
            extra={"stage_inputs_hash": input_hash, "conflict_count": len(conflicts)},
        )
        self.plan["conflicts"] = conflicts
        return snapshot

    def _load_decisions(self, uid: str, profile: str) -> list[dict[str, Any]]:
        key = self.registry.get(f"decision:{uid}:{profile}") or self.decision_keys.get((uid, profile))
        if key is None:
            task = next(task for task in self.tasks if task["utterance_id"] == uid)
            key = self._decision_key(task, profile)
        return self.cache.get(key)["judgments"]

    def run_error_stage(self) -> dict[str, Any]:
        stage = "error_spectrum"
        input_hash = self._error_input_hash()
        latest = self.state.load_latest(stage)
        if latest and latest.get("stage_inputs_hash") == input_hash:
            for task in self.tasks:
                self.plan["error_reused"].append(task["utterance_id"])
                self.plan["reused_utterance_ids"].append(task["utterance_id"])
            return latest

        frame_deviations: list[dict[str, Any]] = []
        candidate_deviations: list[dict[str, Any]] = []
        stability_records: list[dict[str, Any]] = []
        processed = 0
        for task in self.tasks:
            uid = task["utterance_id"]
            feature_key = self.registry[f"feature:{uid}"]
            acoustic = self.cache.get(feature_key)
            reference_judgments = {
                item["candidate_id"]: item for item in self._load_decisions(uid, "fp64")
            }
            uid_error_recomputed = False
            for profile in self.profiles:
                if profile == "fp64":
                    continue
                profile_judgments = {
                    item["candidate_id"]: item for item in self._load_decisions(uid, profile)
                }
                error_key = build_cache_key(
                    stage="error_spectrum",
                    utterance_id=uid,
                    profile=PrecisionContext(profile).spec_hash_object(),
                    decision_key=self.registry.get(f"decision:{uid}:{profile}"),
                    reference_decision_key=self.registry.get(f"decision:{uid}:fp64"),
                    tolerance_hash=canonical_hash(self.cfg.error_tolerances),
                    near_threshold_band=self.cfg.near_threshold_band,
                )
                self.registry[f"error:{uid}:{profile}"] = error_key
                if self.cache.has(error_key):
                    self.plan["error_reused"].append(uid)
                    self.plan["reused_utterance_ids"].append(uid)
                    cached_error = self.cache.get(error_key)
                    frame_deviations.extend(cached_error.get("frame_deviations", []))
                    candidate_deviations.extend(cached_error.get("candidate_deviations", []))
                    continue

                uid_error_recomputed = True
                profile_frame_deviations = [
                    build_frame_deviation(frame, self.cfg, profile)
                    for frame in acoustic["frames"]
                ]
                profile_candidate_deviations: list[dict[str, Any]] = []
                for candidate_id, profile_judgment in profile_judgments.items():
                    reference_judgment = reference_judgments[candidate_id]
                    for tree_version in ("v1", "v2"):
                        if tree_version in self.trees:
                            deviation = build_candidate_deviation(
                                profile_judgment, reference_judgment, tree_version
                            )
                            if deviation:
                                profile_candidate_deviations.append(deviation)
                frame_deviations.extend(profile_frame_deviations)
                candidate_deviations.extend(profile_candidate_deviations)
                self.cache.put(
                    error_key,
                    {
                        "frame_deviations": profile_frame_deviations,
                        "candidate_deviations": profile_candidate_deviations,
                    },
                    {"stage": "error_spectrum", "utterance_id": uid, "precision_profile": profile},
                )

            stability_key = build_cache_key(
                stage="error_stability",
                utterance_id=uid,
                profile_specs=[PrecisionContext(p).spec_hash_object() for p in self.profiles],
                decision_keys={
                    profile: self.registry.get(f"decision:{uid}:{profile}")
                    for profile in self.profiles
                },
                near_threshold_band=self.cfg.near_threshold_band,
                stability_repeats=self.cfg.stability_repeats,
            )
            self.registry[f"error_stability:{uid}"] = stability_key
            if self.cache.has(stability_key):
                stability_records.extend(self.cache.get(stability_key)["records"])
            else:
                uid_error_recomputed = True
                stability = self._stability_for_utterance(acoustic, reference_judgments, uid)
                stability_records.extend(stability)
                self.cache.put(
                    stability_key,
                    {"records": stability},
                    {"stage": "error_stability", "utterance_id": uid},
                )
            if uid_error_recomputed:
                self.plan["error_recomputed"].append(uid)
                self.plan["recomputed_utterance_ids"].append(uid)
            processed += 1
            self._save_cache_index_locked()
            self._maybe_interrupt(stage, processed)

        unstable = [row for row in stability_records if not row["stable"]]
        if unstable:
            raise DiagnosticError(
                "E_ROUNDING_FLIP",
                "near-threshold classification changed across precision repetitions",
                field="stability",
                details={"unstable": unstable[:5]},
            )
        check_tolerances(frame_deviations, candidate_deviations, self.cfg)
        summary = summarize_error_spectrum(frame_deviations, candidate_deviations)
        detail_checksum = canonical_hash(
            {
                "frames": frame_deviations,
                "candidates": candidate_deviations,
            }
        )
        recomputed_summary = summarize_error_spectrum(frame_deviations, candidate_deviations)
        if canonical_hash(recomputed_summary) != canonical_hash(summary):
            raise DiagnosticError(
                "E_ERROR_SPECTRUM_MISMATCH",
                "error summary could not be recomputed from frame-level details",
            )
        observation_details = truth_observation_details(
            [self.cache.get(self.registry[f"feature:{task['utterance_id']}"]) for task in self.tasks],
            self.frame_truth,
        )
        observation = summarize_truth_observation_details(observation_details)
        spectrum = {
            "status": "PASS",
            "profiles": [profile for profile in self.profiles if profile != "fp64"],
            "detail_checksum": detail_checksum,
            "summary": summary,
            "observation_vs_truth": observation,
            "observation_frame_deviations": observation_details,
            "stability": {
                "near_threshold_count": len(stability_records),
                "all_stable": not unstable,
                "records": stability_records,
            },
            "frame_deviations": frame_deviations,
            "candidate_deviations": candidate_deviations,
        }
        spectrum_path = self.artifacts_dir / "error_spectrum.json"
        write_json_atomic(spectrum_path, spectrum)
        artifact_hashes = {"error_spectrum.json": file_sha256(spectrum_path)}
        snapshot = self.state.commit_snapshot(
            stage,
            [task["utterance_id"] for task in self.tasks],
            artifact_hashes=artifact_hashes,
            extra={
                "stage_inputs_hash": input_hash,
                "detail_checksum": detail_checksum,
                "dominant_error_source": summary["dominant_error_source"],
            },
        )
        return snapshot

    def _stability_for_utterance(
        self,
        acoustic: dict[str, Any],
        reference_judgments: dict[str, dict[str, Any]],
        uid: str,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        stability_tree = self.trees.get("v2") or self.trees.get("v1")
        for candidate in acoustic["candidates"]:
            judgment = reference_judgments[candidate["candidate_id"]]
            reference = judgment.get("v2") or judgment.get("v1")
            if not reference:
                continue
            near_nodes = [
                condition
                for trace in reference["decision_path"]
                for condition in trace["conditions"]
                if abs(float(condition["margin"])) <= self.cfg.near_threshold_band
            ]
            if not near_nodes:
                continue
            profile_results: dict[str, list[dict[str, str]]] = {}
            stable = True
            for profile in self.profiles:
                signatures = []
                context = PrecisionContext(profile)
                for _ in range(max(1, self.cfg.stability_repeats)):
                    result = infer_candidate(stability_tree, candidate, context, self.cfg)
                    signature = {
                        "label": result["label"],
                        "selected_rule_id": result["selected_rule_id"],
                        "path": canonical_hash(
                            [
                                [
                                    (condition["feature"], condition["result"])
                                    for condition in trace["conditions"]
                                ]
                                for trace in result["decision_path"]
                            ]
                        )[:16],
                    }
                    signatures.append(signature)
                profile_results[profile] = signatures
                if any(signature != signatures[0] for signature in signatures):
                    stable = False
                if profile != "fp64" and signatures[0]["label"] != reference["label"]:
                    # Precision may move a genuinely threshold-equivalent case;
                    # generated snapshots keep a guard band so this is a failure.
                    stable = False
            records.append(
                {
                    "utterance_id": uid,
                    "candidate_id": candidate["candidate_id"],
                    "frame_index": candidate["frame_index"],
                    "near_nodes": near_nodes,
                    "stable": stable,
                    "results": profile_results,
                }
            )
        return records

    def run_report_stage(
        self, migration_report: dict[str, Any] | None = None, determinism: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        stage = "report"
        artifact_hashes = {
            name: file_sha256(self.artifacts_dir / name)
            for name in (
                "frame_features.jsonl",
                "boundary_candidates.jsonl",
                "candidate_judgments.jsonl",
                "annotation_ledger.jsonl",
                "error_spectrum.json",
            )
            if (self.artifacts_dir / name).exists()
        }
        input_hash = self._report_input_hash(artifact_hashes)
        latest = self.state.load_latest(stage)
        if latest and latest.get("stage_inputs_hash") == input_hash:
            return latest

        spectrum = read_json(self.artifacts_dir / "error_spectrum.json")
        ledger = read_jsonl(self.artifacts_dir / "annotation_ledger.jsonl")
        conflicts = [row for row in ledger if row["has_version_conflict"]]
        snapshot_versions = {
            stage_name: (self.state.load_latest(stage_name) or {}).get("version")
            for stage_name in STAGES
        }
        snapshot_versions["report"] = self.state.next_version("report")
        report = {
            "status": "PASS",
            "mode": "full",
            "sample_dir": str(self.sample_dir),
            "config": self.cfg.to_dict(),
            "profiles": self.profiles,
            "counts": {
                "utterances": len(self.tasks),
                "frames": sum(
                    int(self.cache.get(self.registry[f"feature:{task['utterance_id']}"])["frame_count"])
                    for task in self.tasks
                ),
                "ledger_rows": len(ledger),
                "unique_conflict_candidates": len(
                    {
                        row["candidate_id"]
                        for row in conflicts
                        if row["precision_profile"] == "fp64"
                    }
                ),
            },
            "conflicts_fp64": [
                {
                    "utterance_id": row["utterance_id"],
                    "candidate_id": row["candidate_id"],
                    "v1_label": row["v1_label"],
                    "v2_label": row["v2_label"],
                    "merged_label": row["merged_label"],
                    "decided_by": row["decided_by"],
                }
                for row in conflicts
                if row["precision_profile"] == "fp64"
            ],
            "error_summary": spectrum["summary"],
            "observation_vs_truth": spectrum["observation_vs_truth"],
            "stability": {
                "near_threshold_count": spectrum["stability"]["near_threshold_count"],
                "all_stable": spectrum["stability"]["all_stable"],
            },
            "resource_degradations": self.governor.degradations,
            "cache": {
                "entry_count": len(self.cache.index),
                "persistent_object_count": self.cache.persistent_entry_count(),
                "registry": dict(sorted(self.registry.items())),
            },
            "snapshot_versions": snapshot_versions,
            "migration": migration_report or {},
            "determinism": determinism or {},
        }
        report_path = self.artifacts_dir / "diagnostic_report.json"
        write_json_atomic(report_path, report)
        markdown_path = self.artifacts_dir / "diagnostic_report.md"
        write_text_atomic(markdown_path, self._report_markdown(report))
        artifact_hashes["diagnostic_report.json"] = file_sha256(report_path)
        artifact_hashes["diagnostic_report.md"] = file_sha256(markdown_path)
        snapshot = self.state.commit_snapshot(
            stage,
            [task["utterance_id"] for task in self.tasks],
            artifact_hashes=artifact_hashes,
            extra={"stage_inputs_hash": input_hash},
        )
        watermark = {
            "decision_tree_rev": max(int(task["decision_tree_rev"]) for task in self.tasks),
            "logical_clock_ms": max(int(task["logical_clock_ms"]) for task in self.tasks),
            "utterances": {
                task["utterance_id"]: {
                    "decision_tree_rev": int(task["decision_tree_rev"]),
                    "logical_clock_ms": int(task["logical_clock_ms"]),
                    "revision": int(task.get("revision", 1)),
                }
                for task in self.tasks
            },
        }
        write_json_atomic(self.watermark_path, watermark)
        return snapshot

    def _report_markdown(self, report: dict[str, Any]) -> str:
        lines = [
            "# Prosody Boundary Diagnostic Report",
            "",
            f"Status: **{report['status']}**",
            "",
            "## Counts",
            "",
            f"- Utterances: {report['counts']['utterances']}",
            f"- Frames: {report['counts']['frames']}",
            f"- Ledger rows: {report['counts']['ledger_rows']}",
            f"- Unique fp64 conflict candidates: {report['counts']['unique_conflict_candidates']}",
            f"- Dominant error source: `{report['error_summary']['dominant_error_source']}`",
            f"- Near-threshold stable: {report['stability']['all_stable']}",
            "",
            "## Conflicts",
            "",
        ]
        for conflict in report["conflicts_fp64"]:
            lines.append(
                f"- `{conflict['candidate_id']}`: v1={conflict['v1_label']} "
                f"v2={conflict['v2_label']} -> {conflict['merged_label']} ({conflict['decided_by']})"
            )
        if not report["conflicts_fp64"]:
            lines.append("- None")
        lines.extend(["", "## Resource Degradations", ""])
        for degradation in report["resource_degradations"]:
            lines.append(f"- `{degradation['category']}`: {degradation['reason']}")
        if not report["resource_degradations"]:
            lines.append("- None; all configured limits were respected.")
        lines.append("")
        return "\n".join(lines)

    def run(
        self, migration_report: dict[str, Any] | None = None, determinism: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.run_feature_stage()
        self.run_adjudication_stage()
        self.run_error_stage()
        report_snapshot = self.run_report_stage(migration_report, determinism)
        self._save_cache_index_locked()
        for key in (
            "feature_recomputed",
            "feature_reused",
            "decision_recomputed",
            "decision_reused",
            "error_recomputed",
            "error_reused",
        ):
            self.plan[key] = sorted(set(self.plan[key]))
        recomputed = sorted(set(self.plan["recomputed_utterance_ids"]))
        reused = sorted(set(self.plan["reused_utterance_ids"]) - set(recomputed))
        self.plan["recomputed_utterance_ids"] = recomputed
        self.plan["reused_utterance_ids"] = reused
        self.plan["report_snapshot"] = report_snapshot
        write_json_atomic(self.output_dir / "last_run_plan.json", self.plan)
        if self.change_spec:
            write_json_atomic(self.artifacts_dir / "cache_revalidation.json", self.plan)
        return self.plan
