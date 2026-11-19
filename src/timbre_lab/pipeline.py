"""End-to-end deterministic component pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import PipelineConfig, PrecisionConfig
from .decision import (
    CloneCandidateScore,
    adjudicate_clone_similarity,
)
from .diagnostics import diagnose_dataset
from .embedding import (
    ALIGNMENT_DIMENSION,
    EMBEDDING_DIMENSION,
    STATISTICS_SPEC,
    TimbreEstimate,
    candidate_catalog_and_bases,
    estimate_timbre,
    frame_alignment_vectors,
    timbre_distance_similarity,
)
from .error_spectrum import (
    aggregate_error_spectra,
    compute_error_spectrum,
)
from .errors import DomainError, E_FRAME_MISMATCH
from .features import (
    FeatureTable,
    extract_and_validate,
    extract_features,
    f0_energy_observation,
)
from .precision import (
    PrecisionExecution,
    execute_precision,
    verify_fixed_recomputation,
)
from .snapshots import SnapshotManager
from .synthesis import Dataset, UtteranceSample


@dataclass
class UtteranceRun:
    sample: UtteranceSample
    features: FeatureTable
    timbre: TimbreEstimate
    truth_observation: dict[str, Any]
    executions: dict[str, PrecisionExecution]
    error_spectra: dict[str, dict[str, Any]]
    clone_decision: dict[str, Any]


@dataclass
class PipelineResult:
    dataset: Dataset
    config: PipelineConfig
    reference_estimates: dict[int, TimbreEstimate]
    utterances: dict[str, UtteranceRun]
    aggregate_error_spectrum: dict[str, Any]
    diagnostics: dict[str, Any]

    @property
    def reference_truth_tier(self) -> str:
        return "float64"

    def embedding_stats_payload(self) -> dict[str, Any]:
        return {
            "statistics_spec": STATISTICS_SPEC,
            "embedding_dimension": EMBEDDING_DIMENSION,
            "alignment_dimension": ALIGNMENT_DIMENSION,
            "reference_count": len(self.reference_estimates),
            "references": {
                str(speaker_id): estimate.snapshot_payload()
                for speaker_id, estimate in sorted(self.reference_estimates.items())
            },
        }

    def precision_config_payload(self) -> dict[str, Any]:
        return {
            "pipeline_config": self.config.to_dict(),
            "tiers": {
                tier: self.config.precision(tier).to_dict()
                for tier in sorted(self.config.tiers)
            },
            "frame_geometry": {
                "frame_samples": self.config.frame.frame_samples,
                "hop_samples": self.config.frame.hop_samples,
                "frame_ms": self.config.frame.frame_ms,
                "hop_ms": self.config.frame.hop_ms,
            },
        }

    def error_spectrum_payload(self) -> dict[str, Any]:
        tiers: dict[str, Any] = {}
        for tier, report in sorted(self.aggregate_error_spectrum["tiers"].items()):
            stages: dict[str, Any] = {}
            for name, stage in sorted(report["stages"].items()):
                stages[name] = {
                    key: value
                    for key, value in stage.items()
                    if key not in ("details",)
                }
            tiers[tier] = {
                "reference_tier": report["reference_tier"],
                "observed_tier": report["observed_tier"],
                "frame_count": report["frame_count"],
                "stages": stages,
                "dominant_source": report["dominant_source"],
                "degraded": report["degraded"],
                "fixed_recompute": report["fixed_recompute"],
            }
        truth = self.aggregate_error_spectrum.get("truth_observation")
        return {
            "reference_tier": "float64",
            "tiers": tiers,
            "truth_observation": truth,
            "definitions": self.aggregate_error_spectrum["definitions"],
        }

    def publish_snapshots(
        self,
        manager: SnapshotManager,
        *,
        version: int | None = None,
    ) -> dict[str, Any]:
        return manager.commit_all(
            {
                "embedding_stats": self.embedding_stats_payload(),
                "precision_config": self.precision_config_payload(),
                "error_spectrum": self.error_spectrum_payload(),
            },
            version=version,
        )

    def report(self, *, include_error_details: bool = True) -> dict[str, Any]:
        spectrum = self.aggregate_error_spectrum
        if not include_error_details:
            spectrum = _strip_details(spectrum)
        tier_summaries: dict[str, Any] = {}
        for tier in sorted(self.config.tiers):
            first_execution = next(
                run.executions[tier] for run in self.utterances.values()
            )
            tier_summaries[tier] = {
                "precision": first_execution.config.to_dict(),
                "utterance_count": len(self.utterances),
                "frame_count_total": sum(
                    len(run.executions[tier].frame_indices)
                    for run in self.utterances.values()
                ),
                "assignment_digests": {
                    uid: run.executions[tier].assignment_digest
                    for uid, run in sorted(self.utterances.items())
                },
                "fixed_recompute_ok": all(
                    verify_fixed_recomputation(run.executions[tier])["ok"]
                    for run in self.utterances.values()
                ),
            }
        clone_decisions = {
            uid: {
                "expected_speaker_id": run.sample.speaker_id,
                "best_candidate_id": (
                    run.clone_decision["top_k_candidates"][0]["candidate_id"]
                    if run.clone_decision["top_k_candidates"]
                    else None
                ),
                "best_matches_expected": run.clone_decision.get("best_matches_expected"),
                "accepted_any": run.clone_decision["accepted_any"],
                "business_decision": run.clone_decision.get("business_decision"),
                "top_k": [
                    {
                        "candidate_id": item["candidate_id"],
                        "similarity_stable": item["similarity_stable"],
                        "accepted": item["accepted"],
                        "near_threshold": item["near_threshold"],
                        "rank": item["rank"],
                        "trace": item["trace"],
                    }
                    for item in run.clone_decision["top_k_candidates"]
                ],
            }
            for uid, run in sorted(self.utterances.items())
        }
        return {
            "ok": True,
            "reference_tier": "float64",
            "dataset": {
                "root": str(self.dataset.root),
                "seed": self.dataset.seed,
                "speaker_count": len(self.dataset.speakers),
                "utterance_count": len(self.dataset.utterances),
                "reference_count": len(self.dataset.references),
                "frame_geometry": {
                    "sample_rate": self.config.frame.sample_rate,
                    "frame_ms": self.config.frame.frame_ms,
                    "hop_ms": self.config.frame.hop_ms,
                    "frame_samples": self.config.frame.frame_samples,
                    "hop_samples": self.config.frame.hop_samples,
                },
            },
            "tiers": tier_summaries,
            "error_spectrum": spectrum,
            "truth_observation": spectrum.get("truth_observation"),
            "clone_decisions": clone_decisions,
            "diagnostics": self.diagnostics,
            "business_codes": self.diagnostics.get("business_codes", []),
            "degraded": bool(
                self.diagnostics.get("degraded")
                or any(
                    report.get("degraded")
                    for report in spectrum.get("tiers", {}).values()
                )
            ),
        }


def _strip_details(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_details(item)
            for key, item in value.items()
            if key != "details"
        }
    if isinstance(value, list):
        return [_strip_details(item) for item in value]
    return value


def _raise_first_diagnostic(diagnostics: dict[str, Any]) -> None:
    findings = diagnostics.get("findings", [])
    if findings:
        finding = findings[0]
        raise DomainError(
            finding["code"],
            finding["message"],
            stage=finding["stage"],
            utterance_id=finding.get("utterance_id"),
            field_name=finding.get("field"),
            details=finding.get("details", {}),
        )


def _reference_estimates(
    dataset: Dataset,
    config: PipelineConfig,
) -> dict[int, TimbreEstimate]:
    estimates: dict[int, TimbreEstimate] = {}
    for ref_id in sorted(dataset.references):
        reference = dataset.references[ref_id]
        features = extract_features(
            reference.pcm,
            utterance_id=reference.ref_id,
            config=config.frame,
        )
        if features.frame_count != reference.frame_count:
            raise DomainError(
                E_FRAME_MISMATCH,
                "reference feature frame count does not match its manifest frame count",
                stage="reference_frames",
                utterance_id=reference.ref_id,
                details={
                    "first_mismatch_frame": min(
                        features.frame_count, reference.frame_count
                    ),
                    "feature_frame_count": features.frame_count,
                    "manifest_frame_count": reference.frame_count,
                },
            )
        estimates[reference.speaker_id] = estimate_timbre(
            features,
            owner_id=reference.ref_id,
            speaker_id=reference.speaker_id,
        )
    return estimates


def _clone_decision(
    run: UtteranceRun,
    reference_estimates: dict[int, TimbreEstimate],
    *,
    config: PipelineConfig,
    error_spectrum: dict[str, Any],
) -> dict[str, Any]:
    candidates: list[CloneCandidateScore] = []
    trace_tier = next(
        (tier for tier in config.tiers if tier != "float64"),
        "float64",
    )
    trace_stage = error_spectrum.get("stages", {}).get("embedding_norm")
    trace = {
        "stage": "embedding_norm",
        "error_spectrum_tier": trace_tier,
        "detail_digest": (
            trace_stage.get("detail_digest") if trace_stage is not None else None
        ),
        "frame_indices": list(run.executions["float64"].frame_indices),
        "frame_abs_deviation_mean": (
            trace_stage.get("frame_abs_deviation_mean")
            if trace_stage is not None
            else 0.0
        ),
    }
    utterance_embedding = run.timbre.raw_embedding
    for speaker_id, estimate in sorted(reference_estimates.items()):
        similarity, distance = timbre_distance_similarity(
            utterance_embedding,
            estimate.raw_embedding,
        )
        candidates.append(
            CloneCandidateScore(
                candidate_id=estimate.owner_id,
                similarity=similarity,
                metadata={
                    "speaker_id": speaker_id,
                    "ref_id": estimate.owner_id,
                    "embedding_dimension": estimate.dimension,
                    "scale_aware_distance": distance,
                },
            )
        )
    decision = adjudicate_clone_similarity(
        candidates,
        config.similarity,
        trace=trace,
    )
    best = decision["top_k_candidates"][0] if decision["top_k_candidates"] else None
    decision["expected_speaker_id"] = run.sample.speaker_id
    decision["best_matches_expected"] = bool(
        best is not None
        and best["metadata"].get("speaker_id") == run.sample.speaker_id
    )
    decision["business_decision"] = (
        "accepted"
        if decision["accepted_any"]
        and decision["best_matches_expected"]
        else "rejected_or_mismatched"
    )
    return decision


def run_pipeline(
    dataset: Dataset,
    config: PipelineConfig | None = None,
    *,
    validate_inputs: bool = True,
) -> PipelineResult:
    pipeline_config = config or PipelineConfig()
    pipeline_config.validate()
    diagnostics = diagnose_dataset(dataset, config=pipeline_config.frame)
    if validate_inputs:
        _raise_first_diagnostic(diagnostics)
    reference_estimates = _reference_estimates(dataset, pipeline_config)
    catalog, candidate_bases = candidate_catalog_and_bases(
        reference_estimates.values(),
        pipeline_config.frame.num_phonemes,
    )
    runs: dict[str, UtteranceRun] = {}
    for sample in sorted(dataset.utterances, key=lambda item: item.utterance_id):
        features = extract_and_validate(
            sample.pcm,
            sample.truth,
            utterance_id=sample.utterance_id,
            config=pipeline_config.frame,
        )
        truth_observation = f0_energy_observation(features, sample.truth)
        truth_observation["utterance_id"] = sample.utterance_id
        frame_bases = frame_alignment_vectors(features)
        executions: dict[str, PrecisionExecution] = {}
        for tier in pipeline_config.tiers:
            executions[tier] = execute_precision(
                frame_bases,
                candidate_bases,
                catalog,
                pipeline_config.precision(tier),
            )
        reference_execution = executions["float64"]
        error_spectra: dict[str, dict[str, Any]] = {}
        for tier in sorted(executions):
            if tier == "float64":
                continue
            spectrum = compute_error_spectrum(
                reference_execution,
                executions[tier],
                truth_observation=truth_observation,
                epsilon=pipeline_config.epsilon,
            )
            spectrum["utterance_id"] = sample.utterance_id
            error_spectra[tier] = spectrum
        timbre = estimate_timbre(
            features,
            owner_id=sample.utterance_id,
            speaker_id=sample.speaker_id,
        )
        run = UtteranceRun(
            sample=sample,
            features=features,
            timbre=timbre,
            truth_observation=truth_observation,
            executions=executions,
            error_spectra=error_spectra,
            clone_decision={},
        )
        trace_tier = next(
            (tier for tier in pipeline_config.tiers if tier != "float64"),
            "",
        )
        trace_spectrum = error_spectra.get(trace_tier, {})
        run.clone_decision = _clone_decision(
            run,
            reference_estimates,
            config=pipeline_config,
            error_spectrum=trace_spectrum,
        )
        runs[sample.utterance_id] = run
    all_spectra = [
        spectrum
        for run in runs.values()
        for _, spectrum in sorted(run.error_spectra.items())
    ]
    aggregate = aggregate_error_spectra(
        all_spectra,
        epsilon=pipeline_config.epsilon,
    )
    return PipelineResult(
        dataset=dataset,
        config=pipeline_config,
        reference_estimates=reference_estimates,
        utterances=runs,
        aggregate_error_spectrum=aggregate,
        diagnostics=diagnostics,
    )
