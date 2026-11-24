from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sso_ticket_guard.normalize import normalize_json_dataset
from sso_ticket_guard.samplegen import DEFAULT_SEED, generate_samples


def _issue_tuples(dataset) -> set[tuple[str, int, str]]:
    return {
        (issue.source_type, issue.record_index, issue.code)
        for issue in dataset.issues
    }


def test_invalid_records_are_rejected_with_source_and_index(dataset, truth):
    expected = {
        (source, item["record_index"], item["code"])
        for source, items in truth["normalization_rejections"].items()
        for item in items
    }
    assert _issue_tuples(dataset) == expected
    assert len(dataset.platforms) == 3
    assert len(dataset.fingerprints) == 8
    assert len(dataset.tickets) == 17
    assert len(dataset.renewals) == 13
    assert len(dataset.revocations) == 4


def test_generator_is_deterministic(tmp_path: Path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    generate_samples(left, DEFAULT_SEED)
    generate_samples(right, DEFAULT_SEED)
    left_manifest = json.loads((left / "manifest.json").read_text(encoding="utf-8"))
    right_manifest = json.loads((right / "manifest.json").read_text(encoding="utf-8"))
    assert left_manifest == right_manifest
    for name, digest in left_manifest["files"].items():
        assert hashlib.sha256((left / name).read_bytes()).hexdigest() == digest
        assert (left / name).read_bytes() == (right / name).read_bytes()


def test_canonical_fingerprint_scales_to_six_decimals(dataset):
    alpha = dataset.fingerprints["dev-alpha"].fingerprint
    beta = dataset.fingerprints["dev-beta"].fingerprint
    assert alpha.canonical_components == (123_000, 456_000, 789_000)
    assert alpha.component_error_bounds == (500, 500, 500)
    assert beta.canonical_components == (123_500, 456_100, 789_000)
    assert beta.component_error_bounds == (50, 50, 50)
    assert alpha.canonical_key != beta.canonical_key


def test_external_records_allow_variable_components_and_default_batch():
    payload = {
        "platforms": [{"platform": "ios", "decimals": 3, "clock_quantum_ms": 10}],
        "fingerprints": [
            {"device_id": "dev-two", "platform": "ios", "components": [1, 2]}
        ],
        "tickets": [
            {
                "ticket_id": "ticket-two",
                "device_id": "dev-two",
                "generation": 0,
                "renewal_limit": 1,
                "issued_at_ms": 1000,
                "expires_at_ms": 2000,
                "usage_count": 0,
                "parent_ticket_id": None,
            }
        ],
        "quotas": [
            {
                "kind": "device_window",
                "version": "device-window-seconds/v1",
                "window_seconds": 86400,
                "window_anchor_seconds": 0,
                "max_renewals_per_window": 2,
            },
            {
                "kind": "ticket_interval",
                "version": "ticket-interval-millis/v1",
                "min_interval_ms": 1000,
            },
        ],
        "renewals": [
            {
                "request_id": "request-two",
                "ticket_id": "ticket-two",
                "device_id": "dev-two",
                "observed_at_ms": 1500,
                "fingerprint": {"platform": "ios", "components": [1, 2]},
            }
        ],
        "revocations": [],
    }
    dataset = normalize_json_dataset(payload)
    assert dataset.issues == []
    assert dataset.fingerprints["dev-two"].fingerprint.canonical_components == (
        1_000,
        2_000,
    )
    assert dataset.renewals[0].batch_id == 1

