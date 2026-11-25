from __future__ import annotations

from transcript_governance.attribution import attribute_difference
from transcript_governance.audio import (
    FrameConfig,
    align_interval,
    extract_features,
    frame_windows,
    synthesize_sample_pcm,
)
from transcript_governance.models import Utterance


def test_synthesis_is_fixed_seed_deterministic_and_16k_mono() -> None:
    first = synthesize_sample_pcm(seed=421, gain=0.72)
    second = synthesize_sample_pcm(seed=421, gain=0.72)
    other_gain = synthesize_sample_pcm(seed=421, gain=0.50)

    assert first.pcm_bytes == second.pcm_bytes
    assert first.sample_rate == 16_000
    assert len(first.pcm_bytes) % 2 == 0
    assert first.pcm_bytes != other_gain.pcm_bytes
    assert first.speech_segments_ms


def test_frame_grid_uses_configured_frame_length_and_shift() -> None:
    config = FrameConfig(frame_ms=25, hop_ms=10)
    frames = frame_windows(16_000, config)

    assert config.frame_samples == 400
    assert config.hop_samples == 160
    assert frames[1].start_sample - frames[0].start_sample == 160
    assert frames[0].end_ms - frames[0].start_ms == 25
    assert frames[-1].padded is True


def test_features_contain_energy_zcr_and_quantiles() -> None:
    pcm = synthesize_sample_pcm()
    features = extract_features(pcm.pcm_bytes)

    assert len(features.frames) > 100
    assert set(features.energy_quantiles) == {"q10", "q25", "q50", "q75", "q90"}
    assert all(0.0 <= frame.zero_crossing_rate <= 1.0 for frame in features.frames)
    assert any(frame.energy > 0.0 for frame in features.frames)
    assert features.speech_segments_ms


def test_alignment_and_attribution_reuse_frame_grid() -> None:
    pcm = synthesize_sample_pcm()
    features = extract_features(pcm.pcm_bytes)
    alignment = align_interval(features, 240, 1040, "UTT-001")

    assert alignment.first_frame == 22
    assert alignment.last_frame >= 100
    assert 0.0 <= alignment.speech_frame_ratio <= 1.0

    left = Utterance("A", 5740, 6550, "left", 0.9)
    right = Utterance("A", 5740, 7180, "right", 0.9)
    attribution = attribute_difference(features, left, right)
    assert attribution.end_delta_ms == 630
    assert "boundary_shift" in attribution.reasons
    assert attribution.left.frame_indices == tuple(
        range(attribution.left.first_frame, attribution.left.last_frame + 1)
    )
