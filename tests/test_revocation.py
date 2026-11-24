from __future__ import annotations

import json
from pathlib import Path

from sso_ticket_guard.normalize import load_dataset, normalize_json_dataset
from sso_ticket_guard.revocation import revoke_all


def _by_request(results):
    return {item["request_id"]: item for item in results}


def test_revocation_truth_and_stable_order(dataset, truth):
    results = _by_request(revoke_all(dataset))
    for request_id, expected in truth["revocations"].items():
        actual = results[request_id]
        assert actual["decision"] == expected["decision"]
        assert actual["code"] == expected["code"]
        if "ticket_ids" in expected:
            assert actual["ticket_ids"] == expected["ticket_ids"]
        if "cycle_ticket_ids" in expected:
            assert actual["cycle_ticket_ids"] == expected["cycle_ticket_ids"]
    for result in results.values():
        entities = result["closure"]
        assert entities == sorted(entities, key=lambda item: (item["type"], item["id"]))


def test_cycle_is_reported_with_member_ticket_ids(dataset):
    result = _by_request(revoke_all(dataset))["rev-cycle"]
    assert result["code"] == "E_TICKET_CYCLE"
    assert result["cycle_ticket_ids"] == ["cycle-0", "cycle-1"]
    assert result["cycles"] == [["cycle-0", "cycle-1"]]


def test_closure_is_independent_of_declaration_import_order(samples_dir: Path):
    payload = {
        "platforms": json.loads((samples_dir / "platforms.json").read_text()),
        "fingerprints": json.loads((samples_dir / "fingerprints.json").read_text()),
        "tickets": json.loads((samples_dir / "tickets.json").read_text()),
        "renewals": json.loads((samples_dir / "renewals.json").read_text()),
        "quotas": json.loads((samples_dir / "quotas.json").read_text()),
        "revocations": json.loads((samples_dir / "revocations.json").read_text()),
    }
    forward = _by_request(revoke_all(normalize_json_dataset(payload)))
    for key in ("platforms", "fingerprints", "tickets", "renewals", "revocations"):
        payload[key] = list(reversed(payload[key]))
    reverse = _by_request(revoke_all(normalize_json_dataset(payload)))
    assert forward == reverse

