from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from smugglebench.codes import E_INPUT
from smugglebench.normalize import load_workspace
from smugglebench.rules import merge_rule_view
from smugglebench.util import BusinessError

from .conftest import write_flow, write_json


def test_fixed_seed_workspace_normalizes(sample_workspace: Path) -> None:
    data = load_workspace(sample_workspace)
    assert data.rejections == ()
    assert data.observed_clock_ms == 1_700_000_000_000
    assert data.sample_seed == 20261125
    assert len(data.flows) == 13
    assert len(data.mutations) == 10
    v1 = next(
        rule
        for rule in data.rules
        if rule.canonical_name == "x-trace-observer" and rule.source_kind == "v1"
    )
    assert v1.effective_from_ms == data.observed_clock_ms


def test_invalid_records_are_rejected_with_source_and_continue(
    tmp_path: Path,
) -> None:
    write_json(
        tmp_path / "sample_meta.json",
        {"observed_clock_ms": 5000, "seed": 7},
    )
    valid = {
        "connection_id": "c1",
        "direction": "C2S",
        "seq": 1,
        "logical_ms": 0,
        "payload_hex": "00ff",
    }
    rows = [
        valid,
        {**valid, "seq": 2, "direction": "sideways"},
        {**valid, "seq": 3, "payload_hex": "abc"},
        {**valid, "seq": 4, "logical_ms": -1},
        {**valid, "seq": True},
        {**valid, "seq": 5, "payload_hex": "zz"},
    ]
    write_flow(tmp_path / "flows.jsonl", rows)
    write_json(
        tmp_path / "rules_v1.json",
        {
            "rules": [
                {
                    "rule_name": "clock-conversion",
                    "relative_effective_seconds": 1.5,
                    "match": "state=HEADERS;event=HEADERS_DONE;header=host;action=FLAG",
                }
            ]
        },
    )
    write_json(tmp_path / "mutations.json", {"mutations": []})
    data = load_workspace(tmp_path)
    assert len(data.flows) == 1
    assert len(data.rejections) == 5
    assert all(item.source_file.endswith("flows.jsonl") for item in data.rejections)
    assert [item.line_number for item in data.rejections] == [2, 3, 4, 5, 6]
    assert data.rules[0].effective_from_ms == 6500


def test_relative_v1_clock_does_not_use_system_time(tmp_path: Path) -> None:
    declared = 123_456
    write_json(tmp_path / "sample_meta.json", {"observed_clock_ms": declared})
    write_flow(
        tmp_path / "flows.jsonl",
        [
            {
                "connection_id": "c",
                "direction": "C2S",
                "seq": 1,
                "logical_ms": 0,
                "payload_hex": "",
            }
        ],
    )
    write_json(
        tmp_path / "rules_v1.json",
        {
            "rules": [
                {
                    "rule_name": "relative",
                    "relative_effective_seconds": 2,
                    "match": "state=HEADERS;event=HEADERS_DONE;header=host;action=FLAG",
                }
            ]
        },
    )
    write_json(tmp_path / "mutations.json", {"mutations": []})
    before = int(time.time() * 1000)
    data = load_workspace(tmp_path)
    after = int(time.time() * 1000)
    assert data.rules[0].effective_from_ms == declared + 2000
    assert not (before <= data.rules[0].effective_from_ms <= after)


def test_tombstone_masks_older_rule(sample_workspace: Path) -> None:
    data = load_workspace(sample_workspace)
    active, audit = merge_rule_view(data.rules, data.observed_clock_ms)
    assert "duplicate-cl-observation" not in active
    assert "obsolete-rule" not in active
    assert "te-cl-observation" in active
    assert active["te-cl-observation"].generation == 2
    reasons = {item["canonical_name"]: item["reason"] for item in audit}
    assert reasons["duplicate-cl-observation"] == "TOMBSTONE"
    assert reasons["obsolete-rule"] == "TOMBSTONE"


def test_v2_shards_and_sqlite_metadata_are_merged(sample_workspace: Path) -> None:
    data = load_workspace(sample_workspace)
    v2 = [
        rule
        for rule in data.rules
        if rule.source_kind == "v2" and rule.canonical_name == "trailer-injection"
    ]
    assert len(v2) == 1
    assert v2[0].generation == 2
    assert v2[0].depends_on == ("te-cl-observation",)
    assert v2[0].transitions[0].event == "TRAILER_FIELD"


def test_missing_declared_clock_is_fatal(tmp_path: Path) -> None:
    write_flow(
        tmp_path / "flows.jsonl",
        [
            {
                "connection_id": "c",
                "direction": "C2S",
                "seq": 1,
                "logical_ms": 0,
                "payload_hex": "",
            }
        ],
    )
    write_json(
        tmp_path / "rules_v1.json",
        {
            "rules": [
                {
                    "rule_name": "relative",
                    "relative_effective_seconds": 0,
                    "match": "state=HEADERS;event=HEADERS_DONE;header=host;action=FLAG",
                }
            ]
        },
    )
    write_json(tmp_path / "mutations.json", {"mutations": []})
    with pytest.raises(BusinessError) as raised:
        load_workspace(tmp_path)
    assert raised.value.code == E_INPUT


def test_v1_single_rule_object_is_accepted(tmp_path: Path) -> None:
    write_json(tmp_path / "sample_meta.json", {"observed_clock_ms": 1000})
    write_flow(
        tmp_path / "flows.jsonl",
        [
            {
                "connection_id": "c",
                "direction": "C2S",
                "seq": 1,
                "logical_ms": 0,
                "payload_hex": "",
            }
        ],
    )
    write_json(
        tmp_path / "rules_v1.json",
        {
            "rule_name": "single-object",
            "relative_effective_seconds": 0,
            "match": "state=HEADERS;event=HEADERS_DONE;header=host;action=FLAG",
        },
    )
    write_json(tmp_path / "mutations.json", {"mutations": []})
    data = load_workspace(tmp_path)
    assert len(data.rules) == 1
    assert data.rules[0].canonical_name == "single-object"


def test_v2_tombstone_without_transitions_masks_old_rule(tmp_path: Path) -> None:
    clock = 10_000
    write_json(tmp_path / "sample_meta.json", {"observed_clock_ms": clock})
    write_flow(
        tmp_path / "flows.jsonl",
        [
            {
                "connection_id": "c",
                "direction": "C2S",
                "seq": 1,
                "logical_ms": 0,
                "payload_hex": "",
            }
        ],
    )
    write_json(
        tmp_path / "rules_v1.json",
        {
            "rules": [
                {
                    "rule_name": "same-name",
                    "relative_effective_seconds": 0,
                    "generation": 1,
                    "match": "state=HEADERS;event=HEADERS_DONE;header=host;action=FLAG",
                }
            ]
        },
    )
    write_json(
        tmp_path / "rules_v2" / "tombstone.json",
        {
            "rules": [
                {
                    "canonical_name": "same-name",
                    "generation": 2,
                    "effective_from_ms": clock,
                    "effective_to_ms": None,
                    "enabled": False,
                    "tombstone": True,
                }
            ]
        },
    )
    connection = sqlite3.connect(tmp_path / "rules_v2.sqlite")
    connection.execute(
        """
        CREATE TABLE rules (
            canonical_name TEXT, display_name TEXT, generation INTEGER,
            effective_from_ms INTEGER, effective_to_ms INTEGER,
            enabled INTEGER, tombstone INTEGER, depends_on TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO rules VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("same-name", "same-name", 2, clock, None, 0, 1, ""),
    )
    connection.commit()
    connection.close()
    write_json(tmp_path / "mutations.json", {"mutations": []})
    data = load_workspace(tmp_path)
    assert data.rejections == ()
    active, audit = merge_rule_view(data.rules, clock)
    assert "same-name" not in active
    assert audit[0]["reason"] == "TOMBSTONE"
