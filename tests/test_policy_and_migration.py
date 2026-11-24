from __future__ import annotations

import random

from account_verify.adjudication import evaluate_all_users, group_credentials
from account_verify.policy import PolicyView


def test_policy_merge_conflict_tombstone_and_v1_observed_clock(imported_engine, truth):
    bundle = imported_engine.bundle
    view = imported_engine.policy_view(bundle)

    conflict = view.resolve("u-conflict")
    assert conflict["status"] == "CONFLICT"
    assert conflict["code"] == "E_POLICY_CONFLICT"
    assert len(conflict["basis"]) == 2
    assert {item["format"] for item in conflict["basis"]} == {"v1", "v2"}

    tomb = view.resolve("u-tomb")
    assert tomb["status"] == "OK"
    assert tomb["policy_key"] == "password/default"
    assert tomb["direct_tombstone"] is True
    assert tomb["covered_records"]

    v1_default = next(item for item in bundle["policy_v1"] if item["policy_key"] == "password/default")
    converted = imported_engine.observed_clock_ms() + v1_default["effective_relative_seconds"] * 1000
    authoritative = next(item for item in view.authoritative_report() if item["policy_key"] == "password/default")
    assert authoritative["policy_generation"] == 2
    assert converted == truth["observed_clock_ms"]


def test_user_migration_truth_and_credential_generation(adjudicated_engine, truth):
    decisions = adjudicated_engine.ledger.list_user_decisions()
    for user_id, expected_status in truth["expected_user_status"].items():
        assert decisions[user_id]["status"] == expected_status, user_id

    beta = decisions["u-beta"]
    assert beta["credential_generation"] == 2
    assert beta["status"] == "PENDING_MIGRATION"
    assert "E_ALGORITHM_NOT_ALLOWED" in beta["reason_codes"]

    gamma = decisions["u-gamma"]
    assert gamma["credential_generation"] == 2
    assert gamma["status"] == "UP_TO_DATE"


def test_migration_is_order_independent(imported_engine):
    bundle = imported_engine.bundle
    base_credentials = list(bundle["credentials"])
    view = imported_engine.policy_view(bundle)
    user_ids = imported_engine.user_ids(bundle)
    expected = {
        item["user_id"]: item
        for item in evaluate_all_users(user_ids, group_credentials(base_credentials), view)
    }

    for seed in range(20):
        shuffled = list(base_credentials)
        random.Random(seed).shuffle(shuffled)
        decisions = {
            item["user_id"]: item
            for item in evaluate_all_users(user_ids, group_credentials(shuffled), view)
        }
        assert decisions.keys() == expected.keys()
        for user_id, decision in decisions.items():
            assert decision["status"] == expected[user_id]["status"]
            assert decision["code"] == expected[user_id]["code"]
            assert decision["credential_generation"] == expected[user_id]["credential_generation"]


def test_policy_import_order_does_not_change_view(imported_engine):
    bundle = imported_engine.bundle
    observed = bundle["observed_clock_ms"]
    view_a = PolicyView.from_batches(bundle["policy_v1"], bundle["policy_v2"], observed)
    view_b = PolicyView.from_batches(list(reversed(bundle["policy_v1"])), list(reversed(bundle["policy_v2"])), observed)
    assert view_a.fingerprint == view_b.fingerprint
    for user_id in imported_engine.user_ids():
        a = view_a.resolve(user_id)
        b = view_b.resolve(user_id)
        assert a["status"] == b["status"]
        assert a.get("generation") == b.get("generation")
        assert a.get("min_iterations") == b.get("min_iterations")
