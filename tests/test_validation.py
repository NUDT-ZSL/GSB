from __future__ import annotations

import copy
from pathlib import Path

import pytest

from h2diag.errors import DiagnosticError
from h2diag.service import DiagnosticService
from h2diag.validation import normalize_scenario, validate_patch_document

from conftest import seal_patch


def _minimal_scenario() -> dict:
    return {
        "format": "h2diag.scenario/v1",
        "scenario_id": "minimal",
        "connection_id": "minimal",
        "connection": {
            "initial_send_window": 100,
            "initial_recv_window": 100,
            "max_send_window": 200,
            "max_recv_window": 200,
            "settings": {"initial_window_size": 100},
        },
        "streams": [
            {
                "stream_id": 1,
                "initiator": "client",
                "initial_send_window": 100,
                "initial_recv_window": 100,
                "weight": 16,
            }
        ],
        "events": [
            {
                "seq": 1,
                "clock_ms": 0,
                "type": "HEADERS",
                "direction": "send",
                "stream_id": 1,
                "end_headers": False,
                "fragment_hex": "00",
            }
        ],
    }


def test_invalid_stream_parity(main_scenario: dict) -> None:
    scenario = copy.deepcopy(main_scenario)
    scenario["streams"][0]["initiator"] = "server"
    with pytest.raises(DiagnosticError) as exc:
        normalize_scenario(scenario)
    assert exc.value.code == "STREAM_PARITY_001"


def test_non_monotonic_clock(main_scenario: dict) -> None:
    scenario = copy.deepcopy(main_scenario)
    scenario["events"][3]["clock_ms"] = 0
    with pytest.raises(DiagnosticError) as exc:
        normalize_scenario(scenario)
    assert exc.value.code == "CLOCK_001"


def test_incomplete_headers(main_scenario: dict) -> None:
    scenario = _minimal_scenario()
    with pytest.raises(DiagnosticError) as exc:
        normalize_scenario(scenario)
    assert exc.value.code == "HEADER_INCOMPLETE_001"


def test_continuation_must_be_adjacent(main_scenario: dict) -> None:
    scenario = copy.deepcopy(main_scenario)
    scenario["events"][2]["end_headers"] = False
    scenario["events"][3], scenario["events"][4] = (
        scenario["events"][4],
        scenario["events"][3],
    )
    for index, event in enumerate(scenario["events"], start=1):
        event["seq"] = index
    with pytest.raises(DiagnosticError) as exc:
        normalize_scenario(scenario)
    assert exc.value.code == "HEADER_ORDER_001"


def test_window_update_overflow(main_scenario: dict) -> None:
    scenario = copy.deepcopy(main_scenario)
    scenario["events"][11]["window_increment"] = 999999
    with pytest.raises(DiagnosticError) as exc:
        normalize_scenario(scenario)
    assert exc.value.code == "WINDOW_UPDATE_001"


def test_initial_window_must_be_positive(main_scenario: dict) -> None:
    scenario = copy.deepcopy(main_scenario)
    scenario["streams"][0]["initial_send_window"] = 0
    with pytest.raises(DiagnosticError) as exc:
        normalize_scenario(scenario)
    assert exc.value.code == "FLOW_INITIAL_001"


def test_goaway_cannot_exceed_declared_streams(main_scenario: dict) -> None:
    scenario = copy.deepcopy(main_scenario)
    scenario["events"][-1]["last_stream_id"] = 99
    with pytest.raises(DiagnosticError) as exc:
        normalize_scenario(scenario)
    assert exc.value.code == "GOAWAY_001"


def test_patch_digest_mismatch(main_patch: dict) -> None:
    patch = copy.deepcopy(main_patch)
    patch["content_digest"] = "sha256:" + "0" * 64
    with pytest.raises(DiagnosticError) as exc:
        validate_patch_document(patch)
    assert exc.value.code == "PATCH_DIGEST_001"


def test_invalid_scenario_does_not_create_database(tmp_path: Path) -> None:
    scenario = _minimal_scenario()
    db_path = tmp_path / "never.db"
    service = DiagnosticService(db_path)
    with pytest.raises(DiagnosticError) as exc:
        service.initialize(scenario)
    assert exc.value.code == "HEADER_INCOMPLETE_001"
    assert not db_path.exists()


def test_invalid_patch_reference_does_not_change_state(
    tmp_path: Path, main_scenario: dict
) -> None:
    service = DiagnosticService(tmp_path / "patch-ref.db")
    service.initialize(main_scenario)
    service.run("demo-connection", max_frames=3)
    before = service.status("demo-connection")
    patch = seal_patch(
        {
            "format": "h2diag.patch/v1",
            "patch_id": "bad-ref",
            "connection_id": "demo-connection",
            "base_version": 1,
            "target_version": 2,
            "mode": "incremental",
            "operations": [{"op": "set_weight", "stream_id": 7, "weight": 20}],
        }
    )
    with pytest.raises(DiagnosticError) as exc:
        service.apply_patch("demo-connection", patch)
    assert exc.value.code == "PATCH_REF_001"
    after = service.status("demo-connection")
    assert after["state_digest"] == before["state_digest"]
    assert after["patch_version"] == 1


def test_patch_base_mismatch_is_rejected(tmp_path: Path, main_scenario: dict) -> None:
    service = DiagnosticService(tmp_path / "patch-base.db")
    service.initialize(main_scenario)
    patch = seal_patch(
        {
            "format": "h2diag.patch/v1",
            "patch_id": "wrong-base",
            "connection_id": "demo-connection",
            "base_version": 2,
            "target_version": 3,
            "mode": "incremental",
            "operations": [{"op": "set_weight", "stream_id": 1, "weight": 20}],
        }
    )
    with pytest.raises(DiagnosticError) as exc:
        service.apply_patch("demo-connection", patch)
    assert exc.value.code == "PATCH_BASE_001"
