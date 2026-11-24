from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from pathlib import Path
from typing import Any

from sessionfix.adjudication import RevocationRequest, adjudicate, compute_revocations
from sessionfix.parsing import parse_sample
from sessionfix.samplegen import generate_sample

from conftest import load_json, save_json


def test_generator_is_deterministic_and_uses_sample_clock(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    summary_first = generate_sample(first)
    summary_second = generate_sample(second)

    assert summary_first["digest"] == summary_second["digest"]
    for relative in (
        "manifest.json",
        "sessions.json",
        "events.json",
        "policies/v1.json",
        "policies/v2/index.json",
        "policies/v2/shard-000.json",
        "policies/v2/shard-001.json",
    ):
        assert (first / relative).read_bytes() == (second / relative).read_bytes()

    def query(path: Path) -> list[tuple[Any, ...]]:
        connection = sqlite3.connect(path)
        try:
            return (
                connection.execute(
                    "SELECT key, value FROM policy_metadata ORDER BY key"
                ).fetchall()
                + connection.execute(
                    "SELECT path, sha256, record_count "
                    "FROM policy_shards ORDER BY path"
                ).fetchall()
            )
        finally:
            connection.close()

    assert query(first / "policies/v2/policy_meta.sqlite3") == query(
        second / "policies/v2/policy_meta.sqlite3"
    )
    parsed_first = parse_sample(first)
    parsed_second = parse_sample(second)
    assert parsed_first.sample_digest == parsed_second.sample_digest
    assert parsed_first.manifest.observed_clock_ms == summary_first[
        "observed_clock_ms"
    ]


def test_policy_merge_conversion_tombstone_and_conflict(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    parsed = parse_sample(sample_dir)
    observed = parsed.manifest.observed_clock_ms
    default = parsed.policy_views["default"]
    assert default.policy_generation == 2
    assert default.authority_source == "v2"
    assert default.rotation_mode == "enforce"
    assert default.binding_required is True
    assert default.min_rotation_interval_ms == 120_000

    legacy = parsed.policy_views["legacy"]
    assert legacy.authority_source == "v1"
    assert legacy.effective_at_ms == observed + 15_000

    retired = parsed.policy_views["retired"]
    assert retired.tombstone is True
    assert retired.policy_generation == 5
    retired_session = next(
        item
        for item in adjudicate(parsed).sessions
        if item.session_id == "retired-0000"
    )
    assert retired_session.repair_state == "inactive"
    assert "E_POLICY_TOMBSTONED" in retired_session.codes

    conflict_dir = tmp_path / "conflict"
    generate_sample(conflict_dir)
    v1_path = conflict_dir / "policies" / "v1.json"
    v1 = load_json(v1_path)
    for record in v1["records"]:
        if record["policy_key"] == "default":
            record["rotation_mode"] = "observe"
    save_json(v1_path, v1)
    conflict_sample = parse_sample(conflict_dir)
    conflict_view = conflict_sample.policy_views["default"]
    assert conflict_view.rotation_mode is None
    assert [(item.policy_key, item.field) for item in conflict_view.conflicts] == [
        ("default", "rotation_mode")
    ]
    conflict_result = adjudicate(conflict_sample)
    vulnerable = next(
        item
        for item in conflict_result.sessions
        if item.session_id == "sf-vuln-0000"
    )
    assert vulnerable.repair_state == "indeterminate"
    assert "E_POLICY_CONFLICT" in vulnerable.codes
    assert "E_SESSION_FIXATION" not in vulnerable.codes


def test_parse_rejections_continue_and_are_order_independent(
    sample_dir: Path,
) -> None:
    sessions_path = sample_dir / "sessions.json"
    sessions = load_json(sessions_path)
    sessions["sessions"].append(
        {
            "session_id": "bad-time",
            "user_id": "user-bad",
            "issued_at_ms": 200,
            "expires_at_ms": 100,
            "device_id": "device-bad",
            "generation": 0,
            "parent_session_id": None,
            "window_start_ms": 0,
            "window_end_ms": 1000,
        }
    )
    events_path = sample_dir / "events.json"
    events = load_json(events_path)
    events["events"].append(
        {
            "event_seq": 999,
            "session_id": "sf-vuln-0000",
            "event_time_ms": 100,
            "attempt_result": "unknown",
            "privilege_escalated": False,
        }
    )
    v1_path = sample_dir / "policies" / "v1.json"
    v1 = load_json(v1_path)
    v1["records"].append(
        {
            "policy_key": "invalid-v1",
            "relative_start_seconds": -1,
            "rotation_mode": "enforce",
            "min_rotation_interval_seconds": 1,
            "bind_device_required": True,
        }
    )
    save_json(sessions_path, sessions)
    save_json(events_path, events)
    save_json(v1_path, v1)

    first = parse_sample(sample_dir)
    assert len(first.sessions) == 13
    assert len(first.events) == 6
    categories = {item.source_category for item in first.rejections}
    assert categories == {"sessions", "events", "policy_v1"}
    assert all(item.record_index >= 0 for item in first.rejections)

    random.Random(7).shuffle(sessions["sessions"])
    random.Random(8).shuffle(events["events"])
    random.Random(9).shuffle(v1["records"])
    save_json(sessions_path, sessions)
    save_json(events_path, events)
    save_json(v1_path, v1)
    second = parse_sample(sample_dir)
    assert second.sample_digest == first.sample_digest
    assert [item.to_dict() for item in second.rejections] == [
        item.to_dict() for item in first.rejections
    ]


def test_chain_validation_and_cycle_reporting(
    sample_dir: Path,
) -> None:
    path = sample_dir / "sessions.json"
    document = load_json(path)
    document["sessions"].extend(
        [
            {
                "session_id": "cycle-a",
                "user_id": "user-cycle",
                "issued_at_ms": 1000,
                "expires_at_ms": 2000,
                "device_id": "device-cycle",
                "generation": 1,
                "parent_session_id": "cycle-b",
                "window_start_ms": 0,
                "window_end_ms": 3000,
            },
            {
                "session_id": "cycle-b",
                "user_id": "user-cycle",
                "issued_at_ms": 1000,
                "expires_at_ms": 2000,
                "device_id": "device-cycle",
                "generation": 1,
                "parent_session_id": "cycle-a",
                "window_start_ms": 0,
                "window_end_ms": 3000,
            },
            {
                "session_id": "bad-generation",
                "user_id": "user-fixed",
                "issued_at_ms": 300,
                "expires_at_ms": 400,
                "device_id": "device-fixed",
                "generation": 3,
                "parent_session_id": "sf-fixed-0000",
                "window_start_ms": 0,
                "window_end_ms": 1000,
            },
        ]
    )
    save_json(path, document)
    parsed = parse_sample(sample_dir)
    assert [item.session_ids for item in parsed.cycles] == [
        ("cycle-a", "cycle-b")
    ]
    assert any(
        item.code == "E_SESSION_CHAIN" for item in parsed.rejections
    )
    result = adjudicate(parsed)
    assert "E_SESSION_CYCLE" in result.business_codes
    cycle_finding = next(
        item for item in result.findings if item.code == "E_SESSION_CYCLE"
    )
    assert cycle_finding.detail["session_ids"] == ["cycle-a", "cycle-b"]
    cycle_revocation = compute_revocations(
        parsed,
        [RevocationRequest("session", "cycle-a")],
        len(parsed.events),
    )[0]
    assert cycle_revocation.closure == ("cycle-a", "cycle-b")
    assert cycle_revocation.cycle_session_ids == ("cycle-a", "cycle-b")
    assert cycle_revocation.codes == ("E_SESSION_CYCLE",)


def test_v2_record_validation_and_shard_hash(
    sample_dir: Path,
) -> None:
    shard_path = sample_dir / "policies" / "v2" / "shard-000.json"
    shard = load_json(shard_path)
    shard["records"][0]["effective_at_ms"] = -1
    shard_bytes = (
        json.dumps(shard, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    shard_path.write_bytes(shard_bytes)
    digest = hashlib.sha256(shard_bytes).hexdigest()
    index_path = sample_dir / "policies" / "v2" / "index.json"
    index = load_json(index_path)
    for entry in index["shards"]:
        if entry["path"] == "shard-000.json":
            entry["sha256"] = digest
    save_json(index_path, index)
    db_path = sample_dir / "policies" / "v2" / "policy_meta.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "UPDATE policy_shards SET sha256=? WHERE path=?",
            (digest, "shard-000.json"),
        )
        connection.commit()
    finally:
        connection.close()

    parsed = parse_sample(sample_dir)
    v2_rejections = [
        item
        for item in parsed.rejections
        if item.source_category == "policy_v2_shard"
    ]
    assert len(v2_rejections) == 1
    assert v2_rejections[0].code == "E_POLICY_V2_RECORD"
    assert parsed.policy_views["default"].authority_source == "v1"
    assert parsed.policy_views["default"].policy_generation == 1


def test_event_time_falls_back_to_injected_sample_clock(
    sample_dir: Path,
) -> None:
    path = sample_dir / "events.json"
    document = load_json(path)
    for event in document["events"]:
        event.pop("event_time_ms", None)
    save_json(path, document)
    parsed = parse_sample(sample_dir)
    assert len(parsed.events) == 6
    assert {
        event.event_time_ms - parsed.manifest.observed_clock_ms
        for event in parsed.events
    } == {1, 2, 3, 4, 5, 6}
