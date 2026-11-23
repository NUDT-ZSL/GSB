"""Application service that coordinates the full deterministic pipeline."""

from __future__ import annotations

import copy
import datetime as dt
import math
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from .adaptive import select_for_dataset
from .alignment import (
    align_approximate,
    align_exact,
    alignment_digest,
    validate_truth_phonemes,
)
from .embedding import (
    ReferenceRegistry,
    RegistrationResult,
    replaced_frame_guard,
)
from .errorprop import compute_error_spectrum, validate_error_spectrum
from .errors import AppError, degradation_for
from .features import extract_dataset_features, validate_frame_sets
from .model import (
    EmbeddingSummary,
    PrecisionConfig,
    ProfileResult,
)
from .precision import (
    NumericOps,
    stable_quantize,
    stable_threshold_decision,
    validate_precision,
)
from .similarity import (
    compare_paths,
    score_utterances,
    topk_and_threshold,
)
from .snapshot import SnapshotStore
from .synth import (
    SynthConfig,
    synthesize_dataset,
    synthesize_registration_clip,
    write_dataset_artifacts,
)
from .util import digest_payload, stable_round


class LabService:
    def __init__(
        self,
        root: str | Path,
        synth_config: SynthConfig | None = None,
        memory_budget_mb: float = 64.0,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.synth_config = synth_config or SynthConfig()
        self.memory_budget_mb = memory_budget_mb
        self._lock = threading.RLock()
        self._recent_errors: list[dict[str, Any]] = []
        self._error_sequence = 0
        self.dataset = synthesize_dataset(self.synth_config)
        self.dataset_summary = write_dataset_artifacts(
            self.dataset, self.root / "artifacts"
        )
        self.registry = ReferenceRegistry(self.dataset)
        self.config = PrecisionConfig()
        self._profiles: dict[str, ProfileResult] = {}
        self._spectra: dict[str, dict[str, Any]] = {}
        self._similarities: dict[str, dict[str, Any]] = {}
        self._canonical_features = None
        self._canonical_alignment = None
        self._approx_alignment = None
        self._path_compare: dict[str, Any] | None = None
        self.adaptive = select_for_dataset(
            self.dataset, memory_budget_mb=self.memory_budget_mb
        )
        self._cache_root = self.root / "artifacts" / "cache"
        self.snapshots = SnapshotStore(
            self._cache_root,
            dataset_digest=self.dataset_summary["dataset_digest"],
        )
        initial_reused = tuple(range(self.dataset.total_frames))
        self.last_registration = RegistrationResult(
            changed_speakers=(),
            unchanged_speakers=self.dataset.speakers,
            drifts=(),
            affected_frames=initial_reused,
            recomputed_frames=(),
            reused_frames=initial_reused,
            affected_intervals=(),
            embedding_component_version=self.registry.state.embedding_version,
        )
        self.last_consistency: dict[str, Any] | None = None
        self._rebuild_all()
        self._sync_snapshot()

    # ------------------------------------------------------------------
    # Core pipeline
    # ------------------------------------------------------------------
    def _rebuild_all(self) -> None:
        validate_truth_phonemes(
            {u.utterance_id: u.truth for u in self.dataset.utterances},
            self.dataset.controlled_phonemes,
        )
        canonical_ops = NumericOps("float64")
        self._canonical_features = extract_dataset_features(
            self.dataset.utterances,
            self.dataset.frame_ms,
            self.dataset.frame_hop_ms,
            canonical_ops,
        )
        self._canonical_alignment = align_exact(
            self._canonical_features,
            self.dataset.controlled_phonemes,
            self.dataset.phoneme_prototypes,
        )
        self._approx_alignment = align_approximate(
            self._canonical_features,
            self.dataset.controlled_phonemes,
            self.dataset.phoneme_prototypes,
            coarse_hop_frames=2,
        )
        self._profiles = {}
        self._spectra = {}
        self._similarities = {}
        for profile in ("float64", "float32", "fixed"):
            self._profiles[profile] = self._build_profile(profile)
        canonical = self._profiles["float64"]
        for profile in ("float64", "float32", "fixed"):
            target = self._profiles[profile]
            config = replace(self.config, profile=profile)
            spectrum = compute_error_spectrum(
                self.dataset,
                canonical,
                target,
                config,
            )
            validate_error_spectrum(spectrum)
            self._spectra[profile] = spectrum
            self._similarities[profile] = topk_and_threshold(
                target.clone_scores,
                {u.utterance_id: u for u in self.dataset.utterances},
                self.config,
            )
        exact_scores = canonical.clone_scores
        approx_samples = {
            utterance.utterance_id: tuple(
                range(0, len(self._canonical_features[utterance.utterance_id]), 2)
            )
            for utterance in self.dataset.utterances
        }
        reference_vectors = {
            speaker_id: self.registry.embedding_for_profile(
                speaker_id, canonical_ops
            )
            for speaker_id in self.dataset.speakers
        }
        approximate_scores = score_utterances(
            self.dataset.utterances,
            self._canonical_features,
            reference_vectors,
            canonical_ops,
            approx_samples,
        )
        self._path_compare = compare_paths(
            self._canonical_alignment,
            self._approx_alignment.alignment,
            exact_scores,
            approximate_scores,
            self.config,
        )

    def _build_profile(self, profile: str) -> ProfileResult:
        config = replace(self.config, profile=profile)
        validate_precision(config)
        ops = NumericOps.from_config(config)
        features = extract_dataset_features(
            self.dataset.utterances,
            self.dataset.frame_ms,
            self.dataset.frame_hop_ms,
            ops,
        )
        validate_frame_sets(
            features,
            {u.utterance_id: u.truth for u in self.dataset.utterances},
        )
        alignment = self._canonical_alignment
        if alignment is None:
            alignment = align_exact(
                features,
                self.dataset.controlled_phonemes,
                self.dataset.phoneme_prototypes,
            )
        reference_vectors = {
            speaker_id: self.registry.embedding_for_profile(speaker_id, ops)
            for speaker_id in self.dataset.speakers
        }
        scores = score_utterances(
            self.dataset.utterances,
            features,
            reference_vectors,
            ops,
        )
        return ProfileResult(
            profile=profile,
            frames_by_utterance=features,
            alignment=alignment,
            embedding_vectors=reference_vectors,
            clone_scores=scores,
            feature_digest=digest_payload(features),
            alignment_digest=alignment_digest(alignment),
            score_digest=digest_payload(scores),
        )

    # ------------------------------------------------------------------
    # Snapshot management
    # ------------------------------------------------------------------
    def _active_spectrum(self) -> dict[str, Any]:
        return self._spectra[self.config.profile]

    def _spectrum_stats_payload(self) -> dict[str, Any]:
        spectrum = self._active_spectrum()
        return {
            "target_profile": spectrum["target_profile"],
            "dominant_source": spectrum["dominant_source"],
            "spectrum_digest": spectrum["spectrum_digest"],
            "levels": {
                level: {
                    "p95_abs": spectrum["levels"][level]["absolute_deviation"]["p95"],
                    "p95_rel": spectrum["levels"][level]["relative_deviation"]["p95"],
                    "amplification_ratio": spectrum["levels"][level]["amplification_ratio"],
                }
                for level in spectrum["level_order"]
            },
            "fixed_scale": self.config.fixed_scale,
            "rounding_mode": self.config.rounding_mode,
        }

    def _snapshot_payloads(self) -> dict[str, Any]:
        return {
            "embedding_stats": self.registry.summary_payload(),
            "precision_config": self.config.to_dict(),
            "error_spectrum_stats": self._spectrum_stats_payload(),
        }

    def _sync_snapshot(self) -> None:
        payloads = self._snapshot_payloads()
        current = self.snapshots.current
        if current is None:
            self.snapshots.bootstrap(payloads)
            return
        changed = False
        for name, payload in payloads.items():
            if current.sections[name]["checksum"] != digest_payload(payload):
                changed = True
                break
        if changed:
            self.snapshots.commit(payloads)

    def _commit_snapshot(self, expected_version: int | None = None) -> dict[str, Any]:
        self.snapshots.commit(
            self._snapshot_payloads(),
            expected_version=expected_version,
        )
        return self.snapshots.summary()

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "current": self.snapshots.summary(),
            "verification": self.snapshots.verify(),
            "policy": "whole_snapshot_atomic_forward_only",
        }

    # ------------------------------------------------------------------
    # Public read models
    # ------------------------------------------------------------------
    def embeddings(self) -> dict[str, Any]:
        return self.registry.summary_payload()

    def frame_rows(
        self,
        utterance_id: str,
        profile: str | None = None,
    ) -> list[dict[str, Any]]:
        profile = profile or self.config.profile
        self._ensure_profile(profile)
        utterance = self.dataset.utterance(utterance_id)
        frames = self._profiles[profile].frames_by_utterance[utterance_id]
        alignment = self._profiles[profile].alignment[utterance_id]
        rows: list[dict[str, Any]] = []
        for frame, label, truth in zip(frames, alignment, utterance.truth):
            row = frame.to_dict()
            row.update(
                {
                    "aligned_phoneme_id": label.phoneme_id,
                    "alignment_score": label.score,
                    "runner_up": label.runner_up,
                    "margin": label.margin,
                    "truth_phoneme_id": truth.phoneme_id,
                    "f0_true": stable_round(truth.f0_true, 9),
                    "energy_true": stable_round(truth.energy_true, 12),
                }
            )
            rows.append(row)
        return rows

    def alignment_payload(
        self,
        profile: str | None = None,
        utterance_id: str | None = None,
    ) -> dict[str, Any]:
        profile = profile or self.config.profile
        self._ensure_profile(profile)
        result = self._profiles[profile]
        utterances = (
            [self.dataset.utterance(utterance_id)]
            if utterance_id is not None
            else list(self.dataset.utterances)
        )
        rows = [
            row
            for utterance in utterances
            for row in self.frame_rows(utterance.utterance_id, profile)
        ]
        return {
            "profile": profile,
            "alignment_digest": result.alignment_digest,
            "canonical_label_invariant": True,
            "controlled_phonemes": list(self.dataset.controlled_phonemes),
            "rows": rows,
        }

    def precision_payload(self) -> dict[str, Any]:
        comparisons: list[dict[str, Any]] = []
        canonical = self._profiles["float64"]
        canonical_labels = {
            (utterance_id, row.frame_index): row.phoneme_id
            for utterance_id, rows in canonical.alignment.items()
            for row in rows
        }
        for profile in ("float64", "float32", "fixed"):
            result = self._profiles[profile]
            mismatches = 0
            first = None
            for utterance_id, rows in result.alignment.items():
                for row in rows:
                    expected = canonical_labels[(utterance_id, row.frame_index)]
                    if row.phoneme_id != expected:
                        mismatches += 1
                        if first is None:
                            first = {
                                "utterance_id": utterance_id,
                                "frame_index": row.frame_index,
                                "expected": expected,
                                "actual": row.phoneme_id,
                            }
            comparisons.append(
                {
                    "profile": profile,
                    "feature_digest": result.feature_digest,
                    "alignment_digest": result.alignment_digest,
                    "score_digest": result.score_digest,
                    "alignment_label_mismatches": mismatches,
                    "first_label_mismatch": first,
                    "fixed_scale": self.config.fixed_scale
                    if profile == "fixed"
                    else None,
                    "rounding_mode": self.config.rounding_mode
                    if profile == "fixed"
                    else None,
                }
            )
        return {
            "active_profile": self.config.profile,
            "profiles": comparisons,
            "labels_invariant": all(
                row["alignment_label_mismatches"] == 0 for row in comparisons
            ),
        }

    def similarity_payload(self, profile: str | None = None) -> dict[str, Any]:
        profile = profile or self.config.profile
        self._ensure_profile(profile)
        payload = copy.deepcopy(self._similarities[profile])
        payload["profile"] = profile
        payload["path_consistency"] = self._path_compare
        return payload

    def error_spectrum(self, profile: str | None = None) -> dict[str, Any]:
        profile = profile or self.config.profile
        self._ensure_profile(profile)
        return self._spectra[profile]

    def _ensure_profile(self, profile: str) -> None:
        validate_precision(replace(self.config, profile=profile))
        if profile not in self._profiles:
            self._profiles[profile] = self._build_profile(profile)

    # ------------------------------------------------------------------
    # Public actions
    # ------------------------------------------------------------------
    def append_registration(
        self,
        speaker_id: int,
        ref_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if speaker_id not in self.dataset.speakers:
                raise AppError(
                    "E_INPUT_INVALID",
                    f"Unknown speaker_id {speaker_id}.",
                    stage="registration.request",
                    field="speaker_id",
                    details={"allowed": list(self.dataset.speakers)},
                )
            next_version = (
                max(
                    (
                        clip.version
                        for clip in self.registry.state.clips[speaker_id]
                    ),
                    default=1,
                )
                + 1
            )
            clip_id = ref_id or f"ref_spk_{speaker_id:02d}_v{next_version}"
            clip = synthesize_registration_clip(
                speaker_id=speaker_id,
                ref_id=clip_id,
                version=next_version,
                seed=self.synth_config.seed,
            )
            result = self.registry.append_clip(
                clip,
                expected_seed=clip.clone_seed,
                expected_shape=clip.shape,
            )
            self.last_registration = result
            self._rebuild_all()
            guard = replaced_frame_guard(
                result, tuple(range(self.dataset.total_frames))
            )
            if not guard["disjoint"] or not guard["covers_all"]:
                self._commit_snapshot()
                raise AppError(
                    "E_INCONSISTENT",
                    "Registration recompute/reuse sets failed coverage.",
                    stage="registration.consistency",
                    details={"guard": guard},
                )
            snapshot = self._commit_snapshot()
            return {
                "ok": True,
                "registration": result.to_dict(),
                "recompute_guard": guard,
                "snapshot": snapshot,
            }

    def switch_profile(self, profile: str) -> dict[str, Any]:
        with self._lock:
            config = replace(self.config, profile=profile)
            validate_precision(config)
            self.config = config
            self._ensure_profile(profile)
            snapshot = self._commit_snapshot()
            return {
                "ok": True,
                "active_profile": profile,
                "precision": self.precision_payload(),
                "error_spectrum_summary": {
                    "dominant_source": self._spectra[profile]["dominant_source"],
                    "spectrum_digest": self._spectra[profile]["spectrum_digest"],
                },
                "snapshot": snapshot,
            }

    def update_similarity_config(
        self,
        threshold: float | None = None,
        top_k: int | None = None,
        strategy: str | None = None,
        tolerance: float | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            config = replace(
                self.config,
                threshold=self.config.threshold if threshold is None else float(threshold),
                top_k=self.config.top_k if top_k is None else int(top_k),
                comparison_strategy=(
                    self.config.comparison_strategy if strategy is None else strategy
                ),
                tolerance=(
                    self.config.tolerance if tolerance is None else float(tolerance)
                ),
            )
            if not math.isfinite(config.threshold):
                raise AppError(
                    "E_INPUT_INVALID",
                    "Threshold must be finite.",
                    stage="similarity.configure",
                    field="threshold",
                )
            validate_precision(config)
            self.config = config
            for profile in self._profiles:
                self._similarities[profile] = topk_and_threshold(
                    self._profiles[profile].clone_scores,
                    {u.utterance_id: u for u in self.dataset.utterances},
                    self.config,
                )
            snapshot = self._commit_snapshot()
            return {
                "ok": True,
                "config": self.config.to_dict(),
                "similarity": self.similarity_payload(),
                "snapshot": snapshot,
            }

    def attempt_snapshot_rollback(self, target_version: int) -> dict[str, Any]:
        try:
            self.snapshots.rollback(target_version)
        except AppError as error:
            record = self.record_error(error, entrypoint="snapshot.rollback")
            return {
                "ok": False,
                "error": error.to_dict(),
                "chain": record,
                "degradation": degradation_for(error.code),
            }
        raise AssertionError("rollback should always be rejected")

    # ------------------------------------------------------------------
    # Injection and degradation
    # ------------------------------------------------------------------
    def record_error(
        self,
        error: AppError,
        entrypoint: str,
        injection_stage: str | None = None,
    ) -> dict[str, Any]:
        self._error_sequence += 1
        degradation = degradation_for(error.code)
        record = {
            "sequence": self._error_sequence,
            "entrypoint": entrypoint,
            "stage": error.stage,
            "injection_stage": injection_stage,
            "code": error.code,
            "message": error.message,
            "field": error.field,
            "details": error.details,
            "degradation": degradation,
            "snapshot_version": (
                self.snapshots.current.version
                if self.snapshots.current is not None
                else 0
            ),
            "occurred_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        self._recent_errors.append(record)
        self._recent_errors = self._recent_errors[-30:]
        return record

    def inject_error(self, stage: str) -> dict[str, Any]:
        with self._lock:
            try:
                if stage == "sample_rate":
                    clip = synthesize_registration_clip(
                        speaker_id=1,
                        ref_id=f"ref_inject_rate_{len(self._recent_errors)}",
                        version=99,
                        seed=self.synth_config.seed,
                    )
                    self.registry.append_clip(replace(clip, sample_rate=8000))
                if stage == "seed_shape":
                    clip = synthesize_registration_clip(
                        speaker_id=1,
                        ref_id=f"ref_inject_shape_{len(self._recent_errors)}",
                        version=99,
                        seed=self.synth_config.seed,
                    )
                    expected = clip.clone_seed + 1
                    self.registry.append_clip(clip, expected_seed=expected)
                if stage == "ref_missing":
                    clip = synthesize_registration_clip(
                        speaker_id=2,
                        ref_id="",
                        version=99,
                        seed=self.synth_config.seed,
                    )
                    self.registry.append_clip(clip)
                if stage == "duplicate_utterance":
                    ids = [u.utterance_id for u in self.dataset.utterances]
                    ids.append(ids[0])
                    self._validate_unique_utterances(ids)
                if stage == "phoneme_out_of_range":
                    truth = {
                        u.utterance_id: u.truth for u in self.dataset.utterances
                    }
                    first = self.dataset.utterances[0]
                    bad_truth = list(first.truth)
                    bad_truth[7] = replace(bad_truth[7], phoneme_id=99)
                    truth[first.utterance_id] = tuple(bad_truth)
                    validate_truth_phonemes(truth, self.dataset.controlled_phonemes)
                raise AppError(
                    "E_INPUT_INVALID",
                    f"Unknown injection stage {stage!r}.",
                    stage="injection.dispatch",
                    field="stage",
                    details={
                        "allowed": [
                            "sample_rate",
                            "seed_shape",
                            "ref_missing",
                            "duplicate_utterance",
                            "phoneme_out_of_range",
                        ]
                    },
                )
            except AppError as error:
                record = self.record_error(
                    error, entrypoint="injection", injection_stage=stage
                )
                return {
                    "ok": False,
                    "injection_stage": stage,
                    "error": error.to_dict(),
                    "chain": [record],
                    "degradation": record["degradation"],
                    "active_state_preserved": True,
                    "snapshot_version": record["snapshot_version"],
                }

    @staticmethod
    def _validate_unique_utterances(ids: list[str]) -> None:
        seen: set[str] = set()
        for utterance_id in ids:
            if utterance_id in seen:
                raise AppError(
                    "E_DUPLICATE_UTTERANCE",
                    f"Duplicate utterance_id {utterance_id!r}.",
                    stage="manifest.validate",
                    field="utterance_id",
                    details={"utterance_id": utterance_id},
                )
            seen.add(utterance_id)

    # ------------------------------------------------------------------
    # Cross-level self-check
    # ------------------------------------------------------------------
    def _inconsistent(self, first_difference: dict[str, Any], message: str) -> None:
        raise AppError(
            "E_INCONSISTENT",
            message,
            stage="consistency.selfcheck",
            details={"first_difference": first_difference},
        )

    def run_consistency(self) -> dict[str, Any]:
        with self._lock:
            try:
                truth_sets = {
                    utterance.utterance_id: [row.frame_index for row in utterance.truth]
                    for utterance in self.dataset.utterances
                }
                for profile in ("float64", "float32", "fixed"):
                    result = self._profiles[profile]
                    for utterance_id in sorted(truth_sets):
                        actual = [
                            row.frame_index
                            for row in result.frames_by_utterance[utterance_id]
                        ]
                        if actual != truth_sets[utterance_id]:
                            position = next(
                                (
                                    index
                                    for index, (left, right) in enumerate(
                                        zip(actual, truth_sets[utterance_id])
                                    )
                                    if left != right
                                ),
                                min(len(actual), len(truth_sets[utterance_id])),
                            )
                            self._inconsistent(
                                {
                                    "check": "frame_index_set",
                                    "profile": profile,
                                    "utterance_id": utterance_id,
                                    "position": position,
                                },
                                "Frame-index set mismatch across levels.",
                            )
                alignment_digests = {
                    profile: self._profiles[profile].alignment_digest
                    for profile in ("float64", "float32", "fixed")
                }
                if len(set(alignment_digests.values())) != 1:
                    self._inconsistent(
                        {
                            "check": "alignment_digest",
                            "digests": alignment_digests,
                        },
                        "Alignment labels differ across profiles.",
                    )
                for profile in ("float64", "float32", "fixed"):
                    spectrum = self._spectra[profile]
                    try:
                        validate_error_spectrum(spectrum)
                    except AppError as error:
                        self._inconsistent(
                            {
                                "check": "error_spectrum",
                                "profile": profile,
                                "error": error.to_dict(),
                            },
                            "Error spectrum failed independent recomputation.",
                        )
                    for level in spectrum["level_order"]:
                        detail_sets: dict[str, list[int]] = {}
                        for row in spectrum["levels"][level]["details"]:
                            detail_sets.setdefault(row["utterance_id"], [])
                            if (
                                not detail_sets[row["utterance_id"]]
                                or detail_sets[row["utterance_id"]][-1]
                                != row["frame_index"]
                            ):
                                detail_sets[row["utterance_id"]].append(row["frame_index"])
                        for utterance_id in sorted(truth_sets):
                            if detail_sets.get(utterance_id) != truth_sets[utterance_id]:
                                self._inconsistent(
                                    {
                                        "check": "error_detail_frame_set",
                                        "profile": profile,
                                        "level": level,
                                        "utterance_id": utterance_id,
                                    },
                                    "Error detail frame-index set differs from truth.",
                                )
                guard = replaced_frame_guard(
                    self.last_registration,
                    tuple(range(self.dataset.total_frames)),
                )
                if not guard["disjoint"] or not guard["covers_all"]:
                    self._inconsistent(
                        {"check": "recompute_reuse_sets", "guard": guard},
                        "Recomputed and reused frame sets are not disjoint or do not cover all frames.",
                    )
                candidate_union = set(self._approx_alignment.candidate_phonemes)
                missing = [
                    phoneme_id
                    for phoneme_id in self.dataset.controlled_phonemes
                    if phoneme_id not in candidate_union
                ]
                if missing:
                    self._inconsistent(
                        {
                            "check": "path_phoneme_coverage",
                            "missing_phonemes": missing,
                        },
                        "Selected/approximate path omitted controlled phonemes.",
                    )
                threshold_stability = self._threshold_stability()
                if not threshold_stability["stable"]:
                    self._inconsistent(
                        threshold_stability["first_difference"],
                        "Threshold decision flipped around a near-threshold sample.",
                    )
                topk_stability = self._topk_stability()
                if not topk_stability["stable"]:
                    self._inconsistent(
                        topk_stability["first_difference"],
                        "Top-k ordering differs across precision profiles.",
                    )
                result = {
                    "ok": True,
                    "code": None,
                    "checks": {
                        "frame_index_sets": "pass",
                        "alignment_digest_invariance": "pass",
                        "error_interval_order": "pass",
                        "fixed_recompute": "pass",
                        "recompute_reuse_disjoint_cover": "pass",
                        "controlled_phoneme_coverage": "pass",
                        "threshold_stability": threshold_stability,
                        "topk_stability": topk_stability,
                    },
                    "snapshot": self.snapshots.verify(),
                    "first_difference": None,
                }
                self.last_consistency = result
                return result
            except AppError as error:
                record = self.record_error(error, entrypoint="consistency")
                result = {
                    "ok": False,
                    "code": error.code,
                    "error": error.to_dict(),
                    "chain": [record],
                    "degradation": record["degradation"],
                    "first_difference": error.details.get("first_difference"),
                }
                self.last_consistency = result
                return result

    def _threshold_stability(self) -> dict[str, Any]:
        profiles = self._profiles
        probe_count = 0
        strategies = ("strict", "tolerance_band")
        for strategy in strategies:
            for utterance in sorted(
                self.dataset.utterances, key=lambda item: item.utterance_id
            ):
                canonical_score = profiles["float64"].clone_scores[utterance.utterance_id]
                threshold = stable_quantize(
                    canonical_score, self.config.decision_quantum
                )
                reference_decision: bool | None = None
                for profile in ("float64", "float32", "fixed"):
                    config = replace(
                        self.config,
                        profile=profile,
                        threshold=threshold,
                        comparison_strategy=strategy,
                    )
                    decision = stable_threshold_decision(
                        profiles[profile].clone_scores[utterance.utterance_id],
                        config,
                    )
                    probe_count += 1
                    key = utterance.utterance_id
                    if reference_decision is None:
                        reference_decision = bool(decision["accepted"])
                    elif reference_decision != bool(decision["accepted"]):
                        return {
                            "stable": False,
                            "threshold_probe": threshold,
                            "strategy": strategy,
                            "first_difference": {
                                "check": "threshold_stability",
                                "utterance_id": key,
                                "profile": profile,
                                "strategy": strategy,
                                "expected": reference_decision,
                                "actual": bool(decision["accepted"]),
                            },
                        }
        return {
            "stable": True,
            "probe_count": probe_count,
            "strategies": list(strategies),
            "policy": "threshold_equals_quantized_exact_score_both_strategies",
        }

    def _topk_stability(self) -> dict[str, Any]:
        signatures = {
            profile: tuple(
                row["utterance_id"] for row in payload["top_k"]
            )
            for profile, payload in self._similarities.items()
        }
        reference = signatures["float64"]
        for profile, signature in signatures.items():
            if signature != reference:
                position = next(
                    (
                        index
                        for index, (left, right) in enumerate(zip(signature, reference))
                        if left != right
                    ),
                    min(len(signature), len(reference)),
                )
                return {
                    "stable": False,
                    "first_difference": {
                        "check": "topk_stability",
                        "profile": profile,
                        "position": position,
                        "expected": reference,
                        "actual": signature,
                    },
                }
        return {
            "stable": True,
            "top_k": list(reference),
            "profiles_checked": list(signatures),
        }

    # ------------------------------------------------------------------
    # Aggregate state for the UI
    # ------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        with self._lock:
            profiles = self.precision_payload()
            return {
                "app": {
                    "name": "VocalForge Local",
                    "version": "1.0.0",
                    "mode": "local_deterministic",
                    "sample_rate": self.dataset.sample_rate,
                    "frame_ms": self.dataset.frame_ms,
                    "frame_hop_ms": self.dataset.frame_hop_ms,
                    "dataset_seed": self.dataset.seed,
                    "dataset_digest": self.dataset_summary["dataset_digest"],
                },
                "dataset": {
                    "speakers": list(self.dataset.speakers),
                    "controlled_phonemes": list(self.dataset.controlled_phonemes),
                    "utterance_count": len(self.dataset.utterances),
                    "total_frames": self.dataset.total_frames,
                    "utterances": [
                        {
                            "utterance_id": utterance.utterance_id,
                            "speaker_id": utterance.speaker_id,
                            "ref_id": utterance.ref_id,
                            "duration_ms": utterance.duration_ms,
                            "phoneme_count": utterance.phoneme_count,
                            "clone_seed": utterance.clone_seed,
                            "frame_count": len(utterance.truth),
                            "truth_phonemes": sorted(
                                {row.phoneme_id for row in utterance.truth}
                            ),
                        }
                        for utterance in self.dataset.utterances
                    ],
                    "references": self.registry.reference_summary(),
                },
                "config": self.config.to_dict(),
                "embeddings": self.embeddings(),
                "precision": profiles,
                "adaptive": self.adaptive,
                "selected_path": {
                    "decision": self.adaptive["decision"],
                    "candidate_phonemes": (
                        list(self.dataset.controlled_phonemes)
                        if self.adaptive["decision"] == "exact"
                        else list(self._approx_alignment.candidate_phonemes)
                    ),
                    "missing": [],
                    "comparison_estimate": self.adaptive["comparison_estimate"],
                },
                "similarity": self.similarity_payload(),
                "path_consistency": self._path_compare,
                "error_spectrum": self.error_spectrum(),
                "error_spectrum_summary": self._spectrum_stats_payload(),
                "last_registration": self.last_registration.to_dict(),
                "recompute_guard": replaced_frame_guard(
                    self.last_registration,
                    tuple(range(self.dataset.total_frames)),
                ),
                "snapshot": self.snapshot_state(),
                "recent_errors": list(self._recent_errors),
                "consistency": self.last_consistency,
            }

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "snapshot_version": (
                self.snapshots.current.version
                if self.snapshots.current is not None
                else 0
            ),
            "active_profile": self.config.profile,
            "dataset_digest": self.dataset_summary["dataset_digest"],
            "frame_count": self.dataset.total_frames,
        }
