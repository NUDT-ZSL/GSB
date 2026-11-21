"""Requirement 1: deterministic frame-level seam localization."""

from meeting_align.seams import detect_seam_candidates
from meeting_align.util import canonical_json


def test_detection_is_byte_deterministic(sample_data, config):
    pcm = sample_data["batches"]()["A"].pcm
    r1 = detect_seam_candidates(pcm, config)
    r2 = detect_seam_candidates(pcm, config)
    assert canonical_json(r1) == canonical_json(r2)


def test_frame_boundaries_are_sample_exact(config):
    from meeting_align.features import frame_bounds

    assert frame_bounds(3, config.frame_len, config.frame_hop) == (
        3 * config.frame_hop,
        3 * config.frame_hop + config.frame_len,
    )


def test_detected_gaps_match_insertion_truth(sample_data, config):
    pcm = sample_data["batches"]()["A"].pcm
    result = detect_seam_candidates(pcm, config)
    truth_gaps = sample_data["truth"]["A"]["gaps"]
    detected = result["gaps"]
    assert len(detected) >= len(truth_gaps)
    for t0, t1 in truth_gaps:
        # every inserted gap is found within one frame of tolerance
        assert any(
            abs(d0 - t0) <= config.frame_len and abs(d1 - t1) <= 2 * config.frame_len
            for d0, d1 in detected
        ), f"gap {[t0, t1]} not detected: {detected}"


def test_detected_splices_near_insertion_truth(sample_data, config):
    pcm = sample_data["batches"]()["A"].pcm
    result = detect_seam_candidates(pcm, config)
    splices = [c["sample_index"] for c in result["candidates"] if c["kind"] == "splice"]
    truth = sample_data["truth"]["A"]["splices"]
    tol = config.match_tolerance_ms * config.sample_rate / 1000.0
    # at least one detected splice near each inserted splice boundary pair
    for s0, s1 in truth:
        assert any(abs(s - s0) <= tol or abs(s - s1) <= tol for s in splices), (
            f"splice {[s0, s1]} not detected: {splices}"
        )


def test_candidates_sorted_and_capped(sample_data, config):
    config.max_seam_candidates = 5
    pcm = sample_data["batches"]()["A"].pcm
    result = detect_seam_candidates(pcm, config)
    idx = [c["sample_index"] for c in result["candidates"]]
    assert idx == sorted(idx)
    assert len(idx) <= 5
    assert result["truncated"]
