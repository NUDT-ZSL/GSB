from __future__ import annotations

import copy

import numpy as np
import pytest

from audio_qc.codes import BusinessCode
from audio_qc.detect import detect_drift, replay_event
from audio_qc.dsp import frame_starts
from audio_qc.errors import QCError
from audio_qc.hashing import canonical_bytes
from audio_qc.samples import generate_bundle
from audio_qc.thresholds import (
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    read_thresholds,
)
from audio_qc.verify import verify_migration_gates, verify_serial_vs_oneshot
from audio_qc.dsp import estimate_envelope_serial


@pytest.fixture(scope="module")
def bundle():
    return generate_bundle()


@pytest.fixture(scope="module")
def v1_doc(bundle):
    return read_thresholds(bundle["v1"])


@pytest.fixture(scope="module")
def v2_doc(bundle):
    return read_thresholds(bundle["v2"])


def test_sample_generation_is_byte_deterministic():
    first = generate_bundle()
    second = generate_bundle()
    assert first["pcm_bytes"] == second["pcm_bytes"]
    assert canonical_bytes(first["v1"]) == canonical_bytes(second["v1"])
    assert canonical_bytes(first["v2"]) == canonical_bytes(second["v2"])
    assert first["metadata"]["pcm_sha256"] == second["metadata"]["pcm_sha256"]


def test_frame_boundaries_use_exact_sample_indices():
    starts = frame_starts(1000, frame_length=320, frame_hop=160)
    assert starts.tolist() == [0, 160, 320, 480, 640]
    assert (starts + 320).max() <= 1000
    assert frame_starts(319, 320, 160).size == 0


def test_envelope_values_are_byte_identical(bundle):
    from audio_qc.dsp import estimate_envelope_serial

    first = estimate_envelope_serial(bundle["pcm"])
    second = estimate_envelope_serial(bundle["pcm"])
    assert canonical_bytes(first) == canonical_bytes(second)
    assert first[0]["start_sample"] == 0
    assert first[1]["start_sample"] == 160
    assert first[0]["end_sample"] == 320


def test_version_dispatch_and_field_set_dispatch(bundle):
    assert read_thresholds(bundle["v1"])["source_version"] == "v1"
    assert read_thresholds(bundle["v2"])["source_version"] == "v2"

    no_version_v1 = copy.deepcopy(bundle["v1"])
    no_version_v1.pop("version")
    assert read_thresholds(no_version_v1)["source_version"] == "v1"

    no_version_v2 = copy.deepcopy(bundle["v2"])
    no_version_v2.pop("version")
    assert read_thresholds(no_version_v2)["source_version"] == "v2"


def test_unknown_version_returns_specific_code():
    with pytest.raises(QCError) as exc:
        read_thresholds({"version": 9, "table": []})
    assert exc.value.code == BusinessCode.E_VERSION_UNSUPPORTED

    with pytest.raises(QCError) as exc:
        read_thresholds({"version": "planet-nine", "table": []})
    assert exc.value.code == BusinessCode.E_VERSION_UNSUPPORTED


def test_missing_and_unparseable_fields_return_format_corrupt(bundle):
    missing = copy.deepcopy(bundle["v1"])
    missing["table"][1].pop("energy_gate_db")
    with pytest.raises(QCError) as exc:
        read_thresholds(missing)
    assert exc.value.code == BusinessCode.E_FORMAT_CORRUPT

    bad_number = copy.deepcopy(bundle["v1"])
    bad_number["table"][1]["target_rms_db"] = "not-a-number"
    with pytest.raises(QCError) as exc:
        read_thresholds(bad_number)
    assert exc.value.code == BusinessCode.E_FORMAT_CORRUPT


def test_timeline_overlap_returns_specific_code(bundle):
    overlap = copy.deepcopy(bundle["v1"])
    overlap["table"][2]["start_sample"] = 40_000
    with pytest.raises(QCError) as exc:
        read_thresholds(overlap)
    assert exc.value.code == BusinessCode.E_TIMELINE_OVERLAP
    assert exc.value.details["first_overlap"]["right_segment_id"] == "gain_jump"


def test_v1_to_v2_records_completed_defaults():
    payload = {
        "version": 1,
        "sample_rate": 16_000,
        "frame_length": 320,
        "frame_hop": 160,
        "table": [
            {
                "segment_id": "plain",
                "start_sample": 0,
                "end_sample": 16_000,
                "energy_gate_db": -45,
                "target_rms_db": -16,
                "energy_tolerance_db": 5,
                "subband_limit": 0.2,
            }
        ],
    }
    doc = read_thresholds(payload)
    migrated, report = migrate_v1_to_v2(doc)
    assert migrated["version"] == 2
    sources = [item["source"] for item in report["completed"]]
    assert any(source.startswith("segment_default:") for source in sources)
    assert any(source.startswith("application_default:") for source in sources)
    assert any("subband_limit" in source for source in sources)
    read_thresholds(migrated)


def test_v2_to_v1_flattens_and_discards_nested_profile(v2_doc):
    payload, report = migrate_v2_to_v1(v2_doc)
    assert payload["version"] == 1
    assert report["flattened"]
    assert report["discarded"]
    assert report["discarded"][0]["path"].endswith("adjudication")
    downgraded = read_thresholds(payload)
    assert downgraded["source_version"] == "v1"


def test_migration_preserves_energy_gate_decisions(bundle, v1_doc, v2_doc):
    blocks = estimate_envelope_serial(bundle["pcm"])
    assert verify_migration_gates(blocks, v1_doc)["ok"]
    assert verify_migration_gates(blocks, v2_doc)["ok"]


def test_drift_categories_dual_precision_and_trace(bundle, v1_doc):
    blocks = estimate_envelope_serial(bundle["pcm"])
    result = detect_drift(blocks, v1_doc)
    assert result["dual_consistent"]
    assert result["event_count"] > 400
    codes = {event["business_code"] for event in result["events"]}
    assert BusinessCode.DRIFT_ENERGY in codes
    assert BusinessCode.DRIFT_SUBBAND in codes

    gain_events = [event for event in result["events"] if event["segment_id"] == "gain_jump"]
    spectral_events = [event for event in result["events"] if event["segment_id"] == "spectral_shift"]
    assert gain_events
    assert any(event["business_code"] == BusinessCode.DRIFT_ENERGY for event in gain_events)
    assert spectral_events
    assert all(event["business_code"] == BusinessCode.DRIFT_SUBBAND for event in spectral_events)
    assert max(
        spectral_events,
        key=lambda event: event["float_verdict"]["dominant_contribution"],
    )["dominant_subband"] in {"d1", "d2", "d3", "a3"}

    event = result["events"][0]
    assert event["sample_interval"] == [event["start_sample"], event["end_sample"]]
    assert event["trace"]["pcm_samples"] == f"{event['start_sample']}:{event['end_sample']}"
    assert event["block_hash"] and event["threshold_hash"] and event["precomputed_hash"]
    assert event["dual_numeric_consistency"]["consistent"]
    assert event["float_verdict"]["business_code"] == event["fixed_verdict"]["business_code"]


def test_event_replay_reproduces_category_and_interval(bundle, v1_doc):
    blocks = estimate_envelope_serial(bundle["pcm"])
    event = detect_drift(blocks, v1_doc)["events"][10]
    replay = replay_event(event)
    assert replay["ok"]
    assert replay["business_code"] == event["business_code"]
    assert replay["sample_interval"] == event["sample_interval"]

    tampered = copy.deepcopy(event)
    tampered["business_code"] = BusinessCode.NO_DRIFT
    with pytest.raises(QCError) as exc:
        replay_event(tampered)
    assert exc.value.code == BusinessCode.E_HASH_MISMATCH


def test_serial_vs_oneshot_self_check_passes(bundle, v1_doc):
    result = verify_serial_vs_oneshot(bundle["pcm"], v1_doc)
    assert result["ok"]
    assert result["code"] == BusinessCode.VERIFY_OK
    assert result["first_mismatch"] is None
    assert result["event_count"] > 400


def test_self_check_reports_first_deviation(bundle, v1_doc, monkeypatch):
    import audio_qc.verify as verify_module

    original = verify_module.estimate_envelope_oneshot

    def mutated_oneshot(pcm, frame_length=320, frame_hop=160):
        blocks = original(pcm, frame_length, frame_hop)
        blocks[3]["energy"]["rms_db"] = float(blocks[3]["energy"]["rms_db"]) + 80.0
        return blocks

    monkeypatch.setattr(verify_module, "estimate_envelope_oneshot", mutated_oneshot)
    result = verify_serial_vs_oneshot(bundle["pcm"], v1_doc)
    assert not result["ok"]
    assert result["code"] == BusinessCode.E_INCONSISTENT
    assert result["first_mismatch"]["position"] == 3
    assert result["first_mismatch"]["field"] == "envelope_block"
