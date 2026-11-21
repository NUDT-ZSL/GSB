"""Requirement 8 (part): end-to-end CLI example and JSON report."""

import json

from meeting_align.cli import main


def test_cli_produces_complete_report(tmp_path):
    workdir = tmp_path / "demo"
    rc = main(["--workdir", str(workdir)])
    assert rc == 0
    report = json.loads((workdir / "report.json").read_text())
    assert report["business_code"] == "OK"
    for key in (
        "seam_candidates",
        "calibration",
        "segments",
        "confidence",
        "invalidation",
        "degradations",
        "versions",
        "replay",
    ):
        assert key in report
    # seam candidates carry sample indices, kinds and scores
    cand = report["seam_candidates"]["global"][0]
    assert {"sample_index", "kind", "score"} <= set(cand)
    # calibration exposes scale/offset/method/residuals per batch
    cal_b = report["calibration"]["B"]
    assert cal_b["scale"] != 1.0  # declared 15992 vs applied 16000
    assert cal_b["method"] == "least_squares"
    # every global segment carries lineage and a handling category
    for seg in report["segments"]:
        assert seg["handling"] in ("none", "truncated", "shifted")
        assert seg["lineage"]["map_cache_key"]
    # confidence intervals carry the recorded interval model
    conf = report["confidence"][0]
    assert conf["interval_model"]["formula"]
    assert conf["dominant_source"] in (
        "seam_localization",
        "batch_calibration",
        "segment_shift",
    )
    # targeted invalidation lists and full-recompute equivalence
    assert report["invalidation"]["recompute"]
    assert report["invalidation"]["reuse"]
    assert report["invalidation"]["full_recompute_identical"] is True
    assert report["replay"]["ok"] is True


def test_cli_report_is_byte_deterministic(tmp_path):
    d1, d2 = tmp_path / "d1", tmp_path / "d2"
    main(["--workdir", str(d1)])
    main(["--workdir", str(d2)])
    assert (d1 / "report.json").read_bytes() == (d2 / "report.json").read_bytes()
