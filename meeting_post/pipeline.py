"""End-to-end deterministic processing pipeline."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .config import MeetingConfig, canonical_json_bytes, checksum
from .errors import timebase_mismatch
from .features import FeatureResult, compute_features, registration_embeddings
from .reid import IdentityAssignment, assign_identities
from .segmentation import SeparationResult, separate_speakers
from .selfcheck import run_self_check
from .snapshots import SnapshotStore
from .synthesis import SynthesisResult, synthesize_meeting
from .timebase import assert_no_overlap, verify_span_timebase


@dataclass
class PipelineResult:
    config: MeetingConfig
    session: SynthesisResult
    features: FeatureResult
    profiles: dict[int, dict[str, Any]]
    separation: SeparationResult
    identities: list[IdentityAssignment]
    snapshot_digest: str
    config_digest: str
    result_digest: str
    segment_output: list[dict[str, Any]]
    self_check: dict[str, Any]

    def identity_for(self, segment_id: str) -> IdentityAssignment:
        for identity in self.identities:
            if identity.segment_id == segment_id:
                return identity
        raise KeyError(segment_id)

    def summary(self) -> dict[str, Any]:
        return {
            "config_version": self.config.version,
            "config_digest": self.config_digest,
            "snapshot_digest": self.snapshot_digest,
            "result_digest": self.result_digest,
            "pcm_sha256": self.session.pcm_sha256,
            "feature_sha256": self.features.feature_sha256,
            "mask_sha256": self.separation.mask_sha256,
            "segment_sha256": self.separation.segment_sha256,
            "frame_count": self.session.frame_count,
            "duration_ms": self.session.duration_ms,
            "speaker_ids": sorted(self.profiles),
            "candidate_count": len(self.separation.candidates),
            "segment_count": len(self.separation.segments),
            "accepted_identity_count": sum(1 for item in self.identities if item.accepted),
            "self_check_ok": self.self_check["ok"],
        }


def process_config(
    config: MeetingConfig,
    snapshots: SnapshotStore,
    *,
    seed: int = 20261123,
    check_snapshot_freshness: bool = True,
) -> PipelineResult:
    expected_snapshots = {
        "segmentation": config.segmentation_payload(),
        "separation": config.separation_payload(),
        "reid": config.reid_payload(),
    }
    for item, payload in expected_snapshots.items():
        record = snapshots.read(item)
        if record is None:
            from .errors import MeetingError

            raise MeetingError(
                "E_RESULT_STALE",
                f"{item} snapshot is missing",
                {"item": item, "expected_checksum": checksum(payload)},
            )
        if record.checksum != checksum(payload):
            from .errors import MeetingError

            raise MeetingError(
                "E_RESULT_STALE",
                f"{item} snapshot does not match the processing configuration",
                {
                    "item": item,
                    "snapshot_version": record.version,
                    "snapshot_checksum": record.checksum,
                    "expected_checksum": checksum(payload),
                },
            )
    session = synthesize_meeting(config, seed=seed)
    features = compute_features(config, session)
    profiles = registration_embeddings(config, session, features)
    separation = separate_speakers(config, features, profiles)
    if separation.segments:
        assert_no_overlap(separation.segments, sample_rate=config.sample_rate)
    for segment in separation.segments:
        verify_span_timebase(
            segment,
            sample_rate=config.sample_rate,
            frame_samples=config.frame_samples,
            hop_samples=config.hop_samples,
        )
        if segment.end_sample > config.total_samples:
            raise timebase_mismatch(
                "segment extends beyond audio duration",
                segment_id=segment.segment_id,
                start_frame=segment.start_frame,
                end_frame=segment.end_frame,
                end_sample=segment.end_sample,
                duration_samples=config.total_samples,
            )

    identities = assign_identities(
        config,
        features,
        profiles,
        separation.segments,
        session_truth=session.truth,
        session_speaker_ids={spec.speaker_id for spec in session.speaker_specs},
    )
    identity_by_id = {identity.segment_id: identity for identity in identities}
    segment_output: list[dict[str, Any]] = []
    for segment in separation.segments:
        identity = identity_by_id[segment.segment_id]
        segment_output.append(
            {
                "segment_id": segment.segment_id,
                "mask_speaker_id": segment.speaker_id,
                "start_frame": segment.start_frame,
                "end_frame": segment.end_frame,
                "start_sample": segment.start_sample,
                "end_sample": segment.end_sample,
                "start_ms": segment.start_ms(config.sample_rate),
                "end_ms": segment.end_ms(config.sample_rate),
                "mask_score": segment.mask_score,
                "identity": identity.to_dict(),
            }
        )

    snapshot_digest = snapshots.combined_digest()
    config_digest = config.semantic_digest()
    result_payload = {
        "config_digest": config_digest,
        "snapshot_digest": snapshot_digest,
        "pcm_sha256": session.pcm_sha256,
        "feature_sha256": features.feature_sha256,
        "mask_sha256": separation.mask_sha256,
        "segment_sha256": separation.segment_sha256,
        "segment_output": segment_output,
    }
    result_digest = hashlib.sha256(canonical_json_bytes(result_payload)).hexdigest()
    result = PipelineResult(
        config=config,
        session=session,
        features=features,
        profiles=profiles,
        separation=separation,
        identities=identities,
        snapshot_digest=snapshot_digest,
        config_digest=config_digest,
        result_digest=result_digest,
        segment_output=segment_output,
        self_check={},
    )
    result.self_check = run_self_check(result, raise_on_error=False)
    if check_snapshot_freshness and snapshots.combined_digest() != result.snapshot_digest:
        from .errors import MeetingError

        raise MeetingError(
            "E_RESULT_STALE",
            "snapshot changed while results were being processed",
            {
                "processed_snapshot_digest": result.snapshot_digest,
                "current_snapshot_digest": snapshots.combined_digest(),
            },
        )
    return result


def assert_result_fresh(result: PipelineResult, snapshots: SnapshotStore) -> None:
    current = snapshots.combined_digest()
    if current != result.snapshot_digest:
        from .errors import MeetingError

        raise MeetingError(
            "E_RESULT_STALE",
            "persisted snapshots no longer match the produced result; reprocessing is required",
            {
                "result_snapshot_digest": result.snapshot_digest,
                "current_snapshot_digest": current,
            },
        )
