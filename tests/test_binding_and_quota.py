from __future__ import annotations

from sso_ticket_guard.engine import RenewalEngine


def _reports(dataset) -> dict[str, dict]:
    engine = RenewalEngine(dataset)
    reports, _, _ = engine.adjudicate_all(dataset.renewal_order())
    return {report["request_id"]: report for report in reports}


def test_all_truth_labels_match_engine(dataset, truth):
    reports = _reports(dataset)
    assert set(reports) == set(truth["requests"])
    for request_id, expected in truth["requests"].items():
        actual = reports[request_id]
        assert (actual["decision"], actual["code"]) == (
            expected["decision"],
            expected["code"],
        )
        if "first_rejection" in expected:
            assert (
                actual["checks"]["quota"]["first_rejection"]
                == expected["first_rejection"]
            )


def test_device_binding_mismatch_and_expiry(dataset):
    reports = _reports(dataset)
    mismatch = reports["r-bind-mismatch"]
    assert mismatch["code"] == "E_DEVICE_MISMATCH"
    assert mismatch["failure_stage"] == "fingerprint"
    assert reports["r-expired"]["code"] == "E_TICKET_EXPIRED"
    assert reports["r-generation"]["code"] == "E_GENERATION_EXCEEDED"


def test_cross_platform_ambiguous_fingerprint_preserves_raw_values(dataset):
    reports = _reports(dataset)
    report = reports["r-fp-ambiguous"]
    fingerprint = report["checks"]["fingerprint"]
    assert report["decision"] == "ambiguous"
    assert report["code"] == "E_FP_AMBIGUOUS"
    assert fingerprint["result"] == "ambiguous"
    assert fingerprint["comparison_threshold"] == 0
    assert fingerprint["original_components"] == {
        "request": {
            "platform": "android",
            "decimals": 4,
            "components": [1234, 4560, 7890],
        },
        "bound": {
            "platform": "ios",
            "decimals": 3,
            "components": [123, 456, 789],
        },
    }
    assert fingerprint["request"]["canonical_key"] != fingerprint["bound"]["canonical_key"]


def test_dual_quota_conflict_marks_count_as_first_rejection(dataset):
    report = _reports(dataset)["r-count-both-fail"]
    quota = report["checks"]["quota"]
    assert report["code"] == "E_QUOTA_COUNT"
    assert report["failure_stage"] == "count_window"
    assert quota["count"]["exceeded"] is True
    assert quota["count"]["margin_slots"] == 0
    assert quota["interval"]["exceeded"] is True
    assert quota["interval"]["margin_ms"] == -30_000
    assert quota["first_rejection"] == "count_window"


def test_interval_only_rejection(dataset):
    report = _reports(dataset)["r-interval-reject"]
    quota = report["checks"]["quota"]
    assert report["code"] == "E_QUOTA_INTERVAL"
    assert quota["count"]["exceeded"] is False
    assert quota["interval"]["exceeded"] is True
    assert quota["interval"]["margin_ms"] == -1_000
    assert quota["first_rejection"] == "min_interval"


def test_boundary_uncertainty_reports_margins_and_error_derivation(dataset):
    reports = _reports(dataset)
    interval = reports["r-edge-ambiguous"]["checks"]["quota"]["interval"]
    assert reports["r-edge-ambiguous"]["code"] == "E_QUOTA_AMBIGUOUS"
    assert interval["margin_ms"] == 30
    assert interval["error_bound_ms"] == 50
    assert interval["ambiguous"] is True

    window = reports["r-daybound-ambiguous"]["checks"]["quota"]["count"]
    assert reports["r-daybound-ambiguous"]["code"] == "E_QUOTA_AMBIGUOUS"
    assert window["boundary_distance_ms"] == 300
    assert window["seconds_conversion_step_ms"] == 1_000
    assert window["seconds_conversion_error_ms"] == 1_000
    assert window["clock_alignment_error_ms"] == 0
    assert window["error_bound_ms"] == 1_000
    assert window["ambiguous"] is True


def test_quota_margins_are_present_for_every_adjudication(dataset):
    reports = _reports(dataset)
    for report in reports.values():
        margins = report["quota_margins"]
        assert set(margins) == {
            "count_slots",
            "interval_ms",
            "count_window_boundary_ms",
        }
        assert all(value is None or isinstance(value, int) for value in margins.values())

