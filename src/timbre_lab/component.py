"""Small object facade for embedding the domain logic in another application."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .config import FrameConfig, PipelineConfig, PrecisionConfig, SimilarityConfig
from .decision import CloneCandidateScore, adjudicate_clone_similarity
from .diagnostics import diagnose_records
from .embedding import (
    TimbreEstimate,
    align_units,
    candidate_catalog_and_bases,
    estimate_timbre,
    frame_alignment_vectors,
)
from .error_spectrum import compute_error_spectrum
from .features import FeatureTable, extract_features
from .pipeline import PipelineResult, run_pipeline
from .precision import (
    PrecisionExecution,
    execute_precision,
    verify_fixed_recomputation,
)
from .selfcheck import run_selfcheck
from .snapshots import SnapshotManager
from .synthesis import Dataset, TruthFrame


class TimbreCloneComponent:
    """Stable, explicit entry points over the pure-function implementation."""

    def __init__(self, config: PipelineConfig | None = None) -> None:
        self.config = config or PipelineConfig()
        self.config.validate()

    def extract_features(self, pcm: Any, utterance_id: str) -> FeatureTable:
        return extract_features(
            pcm,
            utterance_id=utterance_id,
            config=self.config.frame,
        )

    def estimate_reference_timbre(
        self,
        features: FeatureTable,
        *,
        ref_id: str,
        speaker_id: int,
    ) -> TimbreEstimate:
        return estimate_timbre(
            features,
            owner_id=ref_id,
            speaker_id=speaker_id,
        )

    def execute_precision(
        self,
        frame_vectors: Any,
        candidate_vectors: Any,
        candidates: Any,
        tier: str,
    ) -> PrecisionExecution:
        return execute_precision(
            frame_vectors,
            candidate_vectors,
            candidates,
            self.config.precision(tier),
        )

    def alignment_catalog(
        self,
        estimates: Iterable[TimbreEstimate],
    ) -> tuple[Any, Any]:
        return candidate_catalog_and_bases(
            estimates,
            self.config.frame.num_phonemes,
        )

    def align_frames(
        self,
        features: FeatureTable,
        estimates: Iterable[TimbreEstimate],
    ) -> dict[str, Any]:
        frame_vectors = frame_alignment_vectors(features)
        catalog, candidate_vectors = self.alignment_catalog(estimates)
        reference = self.execute_precision(
            frame_vectors,
            candidate_vectors,
            catalog,
            "float64",
        )
        return {
            "alignment": reference.alignment,
            "reference_execution": reference,
        }

    def error_spectrum(
        self,
        reference: PrecisionExecution,
        observed: PrecisionExecution,
        truth_observation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return compute_error_spectrum(
            reference,
            observed,
            truth_observation=truth_observation,
            epsilon=self.config.epsilon,
        )

    def adjudicate(
        self,
        candidates: Iterable[CloneCandidateScore],
        *,
        trace: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return adjudicate_clone_similarity(
            candidates,
            self.config.similarity,
            trace=trace,
        )

    def diagnose(
        self,
        entries: Iterable[Mapping[str, Any]],
        audios: Mapping[str, Any],
        truths: Mapping[str, Iterable[TruthFrame | Mapping[str, Any]]],
        references: Any,
    ) -> dict[str, Any]:
        return diagnose_records(
            entries,
            audios,
            truths,
            references,
            config=self.config.frame,
        )

    def run(self, dataset: Dataset) -> PipelineResult:
        return run_pipeline(dataset, self.config)

    def selfcheck(self, result: PipelineResult) -> dict[str, Any]:
        return run_selfcheck(result)

    def verify_fixed(self, execution: PrecisionExecution) -> dict[str, Any]:
        return verify_fixed_recomputation(execution)

    def publish(self, result: PipelineResult, manager: SnapshotManager) -> dict[str, Any]:
        return result.publish_snapshots(manager)
