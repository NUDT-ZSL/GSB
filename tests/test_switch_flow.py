from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from diagnostic_workbench.engine import Workbench
from diagnostic_workbench.errors import BusinessError
from diagnostic_workbench.util import canonical_json_text, content_digest


def _run_sequence(workbench: Workbench) -> list[dict]:
    result = []
    for round_id, clock in ((1, 130), (2, 900), (3, 1300), (4, 1700)):
        result.append(
            workbench.switch(
                {
                    "logical_time_ms": clock,
                    "round_id": round_id,
                }
            )
        )
    return result


def test_full_switch_flow_is_consistent_and_replayable(
    workbench: Workbench,
    state_dir: Path,
    sample: dict,
) -> None:
    round_one = workbench.switch(
        {"round_id": 1, "logical_time_ms": 130}
    )
    assert round_one["status"] == "committed"
    assert round_one["frozen_domains"] == [
        "zone-core",
        "zone-east",
        "zone-west",
    ]
    switched = {item["service"]: item for item in round_one["switches"]}
    assert set(switched) == {"auth", "orders", "payments", "catalog"}
    assert switched["auth"]["from_instance"] == "auth-core-a"
    assert switched["auth"]["to_instance"] == "auth-core-b"
    assert all(item["equal"] for item in round_one["consistency"])

    repeated = workbench.switch(
        {"round_id": 1, "logical_time_ms": 130}
    )
    assert canonical_json_text(repeated) == canonical_json_text(round_one)
    assert workbench.ledger()["count"] == 4

    status = workbench.status()
    assert status["pointer_version"] == 2
    assert status["frozen_domains"] == [
        "zone-core",
        "zone-east",
        "zone-west",
    ]
    assert status["last_event_seq"] == 4
    assert (state_dir / "snapshots" / "CURRENT").read_text(
        encoding="ascii"
    ).strip() == "0000000002"

    verification = workbench.verify({"round_id": 1})
    assert verification["verified"] is True
    assert verification["status"] == "consistent"
    assert verification["pointer_versions"] == {"before": 1, "after": 2}
    for report in verification["services"]:
        assert report["before"]["query_digest"] == report["after"]["query_digest"]
        assert report["committed_summary_match"] is True
        assert all(item["equal_to_primary"] for item in report["replica_checks"])

    round_two = workbench.switch(
        {"round_id": 2, "logical_time_ms": 900}
    )
    assert round_two["switches"] == []
    assert {item["service"] for item in round_two["failback_steps"]} == {
        "auth",
        "orders",
        "payments",
        "catalog",
    }
    assert workbench.status()["frozen_domains"] == [
        "zone-core",
        "zone-east",
        "zone-west",
    ]

    # A second, fresh database produces byte-identical deterministic results.
    other = Workbench(state_dir.parent / "other-state")
    other.load({"sample": copy.deepcopy(sample)})
    other_results = _run_sequence(other)
    first_results = [round_one, round_two, *_run_sequence(workbench)[2:]]
    assert [content_digest(item) for item in first_results] == [
        content_digest(item) for item in other_results
    ]
    assert other.status()["snapshot_digest"] == workbench.status()["snapshot_digest"]


def test_query_snapshot_is_isolated_from_concurrent_switch(
    workbench: Workbench,
) -> None:
    pinned = workbench.storage.current_snapshot()
    assert pinned is not None
    old_query = workbench.query(
        "auth",
        pointer_version=pinned.pointer_version,
    )
    assert old_query["primary"] == "auth-core-a"
    assert old_query["frozen"] is False

    workbench.switch({"round_id": 1, "logical_time_ms": 130})

    pinned_again = workbench.query(
        "auth",
        pointer_version=pinned.pointer_version,
    )
    current = workbench.query("auth")
    assert pinned_again["primary"] == "auth-core-a"
    assert pinned_again["frozen"] is False
    assert pinned_again["query"] == old_query["query"]
    assert current["primary"] == "auth-core-b"
    assert current["frozen"] is True
    assert current["pointer_version"] == pinned.pointer_version + 1


def test_clock_regression_and_idempotency_conflict(
    workbench: Workbench,
) -> None:
    workbench.switch({"round_id": 1, "logical_time_ms": 130})
    with pytest.raises(BusinessError) as clock_error:
        workbench.switch({"round_id": 2, "logical_time_ms": 100})
    assert clock_error.value.code == "CLOCK_REGRESSION"
    assert clock_error.value.context["round_id"] == 2
    assert clock_error.value.context["tolerance_ms"] == 2

    with pytest.raises(BusinessError) as idempotency_error:
        workbench.switch({"round_id": 1, "logical_time_ms": 131})
    assert idempotency_error.value.code == "IDEMPOTENCY_CONFLICT"
    assert workbench.status()["pointer_version"] == 2


def test_domain_failure_does_not_stop_other_domains(
    state_dir: Path,
    sample: dict,
) -> None:
    broken = copy.deepcopy(sample)
    orders = next(
        service
        for service in broken["topology"]["services"]
        if service["name"] == "orders"
    )
    orders["instances"] = orders["instances"][:1]
    for event in broken["events"]:
        if event["instance_id"] == "orders-east-b":
            event["instance_id"] = "orders-east-a"

    workbench = Workbench(state_dir)
    workbench.load({"sample": broken})
    result = workbench.switch({"round_id": 1, "logical_time_ms": 130})
    domain_status = {item["domain"]: item for item in result["domains"]}
    assert domain_status["zone-east"]["status"] == "failed"
    assert domain_status["zone-east"]["error"]["code"] == "SWITCH_TARGET_MISSING"
    assert domain_status["zone-east"]["error"]["context"]["service"] == "orders"
    assert domain_status["zone-core"]["status"] == "completed"
    assert domain_status["zone-west"]["status"] == "completed"
    assert {item["service"] for item in result["switches"]} == {
        "auth",
        "catalog",
    }
    assert workbench.status()["services"][1]["service"] == "catalog"
    assert workbench.status()["services"][1]["switched_away"] is True


def test_replica_divergence_reports_first_field_and_blocks_domain(
    state_dir: Path,
    sample: dict,
) -> None:
    divergent = copy.deepcopy(sample)
    catalog = next(
        service
        for service in divergent["topology"]["services"]
        if service["name"] == "catalog"
    )
    catalog["instances"][1]["data_variant"] = 1

    workbench = Workbench(state_dir)
    workbench.load({"sample": divergent})
    result = workbench.switch({"round_id": 1, "logical_time_ms": 130})
    domain_status = {item["domain"]: item for item in result["domains"]}
    failure = domain_status["zone-west"]
    assert failure["status"] == "failed"
    assert failure["error"]["code"] == "QUERY_CONSISTENCY_VIOLATION"
    divergence = failure["error"]["context"]["first_divergence"]
    assert divergence["field_path"].startswith("queries.")
    assert divergence["primary_value"] != divergence["standby_value"]
    assert divergence["primary_pointer_version"] == 1
    assert divergence["standby_pointer_version"] == 2
    assert "auth" in {item["service"] for item in result["switches"]}
    assert "catalog" not in {item["service"] for item in result["switches"]}


def test_budget_outcomes_are_distinct_and_never_retroactively_labelled(
    state_dir: Path,
    sample: dict,
) -> None:
    budgeted = copy.deepcopy(sample)
    budgeted["policy"]["time_budget_ms"] = 15
    budgeted["policy"]["freeze_batch_size"] = 3
    budgeted["policy"]["max_concurrency"] = 1
    budgeted["policy"]["probe_cost_ms"] = {"freeze": 10, "switch": 10}
    workbench = Workbench(state_dir)
    workbench.load({"sample": budgeted})
    result = workbench.switch({"round_id": 1, "logical_time_ms": 130})

    probes = [
        probe
        for domain in result["domains"]
        for probe in domain["probes"]
    ]
    statuses = {probe["status"] for probe in probes}
    assert "abandoned_budget" in statuses
    assert "not_started_within_budget" in statuses
    assert all(
        probe["status"] != "completed"
        for domain in result["domains"]
        if domain["domain"] == "zone-core"
        for probe in domain["probes"]
    )
    assert all(domain["status"] == "abandoned" for domain in result["domains"])
    assert all(
        probe["end_offset_ms"] is None
        for probe in probes
        if probe["status"] == "abandoned_budget"
    )
    assert result["switches"] == []
    assert workbench.status()["pointer_version"] == 2


def test_policy_rollback_and_duplicate_policy_version(
    workbench: Workbench,
    sample: dict,
) -> None:
    higher = copy.deepcopy(sample)
    higher["policy"]["version"] = 2
    higher["policy"]["services"]["auth"]["cooldown_ms"] = 601
    loaded = workbench.load({"sample": higher})
    assert loaded["policy_version"] == 2
    assert loaded["pointer_version"] == 2

    with pytest.raises(BusinessError) as rollback:
        workbench.load({"sample": copy.deepcopy(sample)})
    assert rollback.value.code == "POLICY_VERSION_ROLLBACK"
    assert rollback.value.context["incoming_policy_version"] == 1

    same_version = copy.deepcopy(higher)
    same_version["policy"]["services"]["auth"]["cooldown_ms"] = 999
    with pytest.raises(BusinessError) as conflicting:
        workbench.load({"sample": same_version})
    assert conflicting.value.code == "POLICY_VERSION_CONFLICT"

