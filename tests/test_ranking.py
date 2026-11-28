from __future__ import annotations

from pathlib import Path

from app.config import SCORE_TOLERANCE
from app.ranking import rank_candidates
from app.reference import rank_request_reference
from app.scoring import score_candidate
from app.service import create_request, get_request, load_request_inputs, rank_request
from app.verifier import _sampling_variant, verify_all


def test_full_reference_verification_passes(generated_db: Path) -> None:
    report = verify_all(db_path=generated_db, write_artifacts=False)
    assert report["passed"] is True
    assert report["request_count"] == 16
    assert report["score_mismatch_count"] == 0
    assert report["breakdown_mismatch_count"] == 0
    assert report["first_rank_deviation"] is None
    assert report["invariance"]["case_count"] == 64
    assert report["invariance"]["failure_count"] == 0
    assert report["consistency"]["overall"]["pairwise_agreement"] == 1.0


def test_engine_and_reference_agree_on_three_routes(generated_db: Path) -> None:
    inputs = load_request_inputs("R-A001-BASELINE", generated_db)
    engine = rank_request("R-A001-BASELINE", db_path=generated_db)
    reference = rank_request_reference(
        request_id=inputs["request_id"],
        athlete_id=inputs["athlete_id"],
        phase=inputs["phase"],
        candidates=inputs["candidates"],
        profile=inputs["profile"],
        weights=inputs["weights"],
        prior_ranks=inputs["prior_ranks"],
    )
    assert engine["ranked_order"] == reference["ranked_order"]
    reference_scores = {
        item["clip_id"]: item["scores"] for item in reference["ranking"]
    }
    for item in engine["ranking"]:
        for key in ("quality", "load_risk", "profile_match", "fused"):
            assert (
                abs(item["scores"][key] - reference_scores[item["clip_id"]][key])
                <= SCORE_TOLERANCE
            )
    focus_ids = {item["clip_id"] for item in engine["focus_review"]}
    assert set(engine["injury_uplift_candidates"]) <= focus_ids
    dropped = {
        item["clip_id"]
        for item in engine["ranking"]
        if item["rank_delta"] >= engine["rank_drop_threshold"]
    }
    assert dropped <= focus_ids


def test_sampling_and_label_order_invariance(generated_db: Path) -> None:
    inputs = load_request_inputs("R-A001-DELOAD", generated_db)
    clip = inputs["candidates"][0]
    profile = {
        **inputs["profile"],
        "injury_tags": list(reversed(inputs["profile"]["injury_tags"])),
    }
    original = score_candidate(
        clip, profile, inputs["phase"], inputs["weights"]
    )
    variant = score_candidate(
        _sampling_variant(clip), profile, inputs["phase"], inputs["weights"]
    )
    assert original["statistics"] == variant["statistics"]
    assert original["alignment"]["signature"] == variant["alignment"]["signature"]
    for key in ("quality", "load_risk", "profile_match", "fused"):
        assert abs(original["scores"][key] - variant["scores"][key]) <= SCORE_TOLERANCE


def test_injury_tags_on_same_body_part_uplift_once(generated_db: Path) -> None:
    inputs = load_request_inputs("R-A001-BASELINE", generated_db)
    high_load = max(
        inputs["candidates"],
        key=lambda clip: score_candidate(
            clip, inputs["profile"], inputs["phase"], inputs["weights"]
        )["statistics_summary"]["peak_load"],
    )
    profile = {**inputs["profile"], "injury_tags": ["左膝前叉韧带", "右膝半月板", "髌腱炎"]}
    result = score_candidate(
        high_load, profile, inputs["phase"], inputs["weights"]
    )
    adjustments = result["injury_risk_uplift"]
    assert len([item for item in adjustments if item["body_part"] == "knee"]) <= 1


def test_equal_scores_break_ties_by_clip_id() -> None:
    profile = {
        "athlete_id": "TIE",
        "name": "tie",
        "specialty": "短跑",
        "training_years": 5,
        "injury_tags": [],
    }
    channels = {
        "ax": [0.0, 1.0, 0.0, -1.0],
        "ay": [0.0, 0.5, 0.0, -0.5],
        "az": [0.0, 0.2, 0.0, -0.2],
        "gx": [0.0, 0.4, 0.0, -0.4],
        "gy": [0.0, 0.3, 0.0, -0.3],
        "gz": [0.0, 0.2, 0.0, -0.2],
    }
    clip = {
        "athlete_id": "TIE",
        "action_category": "sprint_run",
        "repetition_count": 8,
        "duration_sec": 6.0,
        "manual_quality_score": 80.0,
        "acceleration": {key: channels[key] for key in ("ax", "ay", "az")},
        "angular_velocity": {key: channels[key] for key in ("gx", "gy", "gz")},
    }
    result = rank_candidates(
        request_id="TIE",
        athlete_id="TIE",
        phase="baseline",
        candidates=[
            {**clip, "clip_id": "TIE-C"},
            {**clip, "clip_id": "TIE-A"},
            {**clip, "clip_id": "TIE-B"},
        ],
        profile=profile,
    )
    assert result["ranked_order"] == ["TIE-A", "TIE-B", "TIE-C"]
    assert result["tie_groups"][0]["clip_ids"] == ["TIE-A", "TIE-B", "TIE-C"]


def test_create_request_with_selected_candidates(generated_db: Path) -> None:
    inputs = load_request_inputs("R-A001-BASELINE", generated_db)
    selected = [clip["clip_id"] for clip in inputs["candidates"][:5]]
    detail = create_request(
        athlete_id="A001",
        phase="build",
        candidate_clip_ids=selected,
        db_path=generated_db,
    )
    assert detail["candidate_ids"] == selected
    assert detail["result"]["candidate_count"] == 5
    assert detail["result"]["phase"] == "build"
    assert get_request(detail["request_id"], db_path=generated_db)["result"]
