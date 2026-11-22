from __future__ import annotations

import pytest

from slo_workbench.errors import BusinessError
from slo_workbench.loader import load_sample_bundle
from slo_workbench.models import parse_event, parse_event_list, parse_slo, parse_slo_list


def base_slo(**overrides):
    payload = {
        "name": "s1",
        "service": "svc-a",
        "sli_type": "availability",
        "target": 99.0,
        "window_length_ms": 2000,
        "window_step_ms": 1000,
        "compliance_period_ms": 5000,
        "burn_alert_threshold": 2.0,
    }
    payload.update(overrides)
    return payload


def test_valid_slo_parses():
    slo = parse_slo(base_slo())
    assert slo.name == "s1"
    assert str(slo.target) == "99.0"


@pytest.mark.parametrize(
    "overrides",
    [
        {"sli_type": "bogus"},
        {"target": 0},
        {"target": 100},
        {"target": -1},
        {"window_length_ms": 0},
        {"window_step_ms": 0},
        {"window_step_ms": 5000},
        {"compliance_period_ms": 100},
        {"burn_alert_threshold": 0},
        {"name": ""},
        {"service": ""},
    ],
)
def test_invalid_slo_raises_e_bad_slo(overrides):
    with pytest.raises(BusinessError) as exc:
        parse_slo(base_slo(**overrides))
    assert exc.value.code == "E_BAD_SLO"


def test_duplicate_slo_and_service_names_rejected():
    payload = [base_slo(), base_slo()]
    with pytest.raises(BusinessError) as exc:
        parse_slo_list(payload)
    assert exc.value.code == "E_BAD_SLO"

    second = base_slo(name="s2", service="svc-a")
    with pytest.raises(BusinessError) as exc:
        parse_slo_list([base_slo(), second])
    assert exc.value.code == "E_BAD_SLO"


def test_latency_slo_requires_threshold_fields():
    with pytest.raises(BusinessError) as exc:
        parse_slo(base_slo(sli_type="latency"))
    assert exc.value.code == "E_BAD_SLO"

    slo = parse_slo(
        base_slo(
            sli_type="latency",
            latency_threshold_ms=120,
            latency_percentile=95,
        )
    )
    assert str(slo.latency_threshold_ms) == "120"


def base_event(**overrides):
    payload = {
        "seq": 1,
        "service": "svc-a",
        "clock_ms": 10,
        "success": True,
        "latency_ms": 25,
        "valid": True,
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "overrides",
    [
        {"seq": "x"},
        {"clock_ms": -5},
        {"success": "yes"},
        {"valid": 1},
        {"latency_ms": -1},
        {"service": ""},
    ],
)
def test_invalid_event_raises_e_bad_event(overrides):
    with pytest.raises(BusinessError) as exc:
        parse_event(base_event(**overrides))
    assert exc.value.code == "E_BAD_EVENT"


def test_event_sequence_must_strictly_increase():
    events = [base_event(seq=1), base_event(seq=1)]
    with pytest.raises(BusinessError) as exc:
        parse_event_list(events)
    assert exc.value.code == "E_BAD_EVENT"


def test_invalid_bundle_does_not_write_state(workspace, tmp_path):
    sample_dir = tmp_path / "bad-samples"
    sample_dir.mkdir()
    (sample_dir / "slos.json").write_text("[{}]")
    (sample_dir / "events.json").write_text("[]")
    (sample_dir / "clock.json").write_text(
        '{"ticks_ms": [1000], "rollback_tolerance_ms": 0}'
    )
    with pytest.raises(BusinessError) as exc:
        load_sample_bundle(workspace, sample_dir, skip_manifest=True)
    assert exc.value.code == "E_BAD_SLO"

    conn = workspace.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM slo_def").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == 0
    finally:
        conn.close()


def test_manifest_tampering_detected(sample_dir, workspace):
    target = sample_dir / "slos.json"
    target.write_text(target.read_text() + " ")
    from slo_workbench.errors import E_SAMPLE_MISMATCH

    with pytest.raises(BusinessError) as exc:
        load_sample_bundle(workspace, sample_dir)
    assert exc.value.code == E_SAMPLE_MISMATCH
