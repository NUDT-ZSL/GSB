from __future__ import annotations

import json
from pathlib import Path

import pytest

from metric_engine.config import load_configuration
from metric_engine.engine import Engine
from metric_engine.errors import EngineError

from conftest import (
    DEFAULT_PARTITIONS,
    DEFAULT_QUOTAS,
    DEFAULT_STREAMS,
    DEFAULT_TOKENS,
    event,
)


def _exception_code(exc: pytest.ExceptionInfo[EngineError]) -> str:
    return exc.value.code


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        (
            lambda docs: docs["streams"].append(dict(docs["streams"][0])),
            "E_BAD_STREAM",
        ),
        (
            lambda docs: docs["streams"][0].update({"tags": {"bad key": "x"}}),
            "E_BAD_TAG",
        ),
        (
            lambda docs: docs["partitions"].update({"partition_count": 0}),
            "E_BAD_PARTITION_CONFIG",
        ),
        (
            lambda docs: docs["partitions"].update({"buffer_per_partition": 0}),
            "E_BAD_PARTITION_CONFIG",
        ),
        (
            lambda docs: docs["partitions"].update({"flush_round_limit": -1}),
            "E_BAD_PARTITION_CONFIG",
        ),
        (
            lambda docs: docs["streams"][0].update({"sample_period_ms": 0}),
            "E_BAD_STREAM",
        ),
        (
            lambda docs: docs["streams"][0].update({"sample_bytes": 0}),
            "E_BAD_STREAM",
        ),
        (
            lambda docs: docs["tokens"].update({"capacity_tokens": 0}),
            "E_BAD_TOKEN",
        ),
        (
            lambda docs: docs["tokens"].update({"refill_rate_per_second": 0}),
            "E_BAD_TOKEN",
        ),
    ],
)
def test_invalid_config_has_business_error_and_no_state(
    workspace, mutator, expected_code
) -> None:
    docs = {
        "streams": json.loads(json.dumps(DEFAULT_STREAMS)),
        "partitions": json.loads(json.dumps(DEFAULT_PARTITIONS)),
        "quotas": json.loads(json.dumps(DEFAULT_QUOTAS)),
        "tokens": json.loads(json.dumps(DEFAULT_TOKENS)),
    }
    mutator(docs)
    paths = {
        "streams": workspace.root / "invalid-streams.json",
        "partitions": workspace.root / "invalid-partitions.json",
        "quotas": workspace.root / "invalid-quotas.json",
        "tokens": workspace.root / "invalid-tokens.json",
    }
    payloads = {
        "streams": {"streams": docs["streams"]},
        "partitions": docs["partitions"],
        "quotas": docs["quotas"],
        "tokens": docs["tokens"],
    }
    for key, path in paths.items():
        path.write_text(json.dumps(payloads[key]), encoding="utf-8")

    with pytest.raises(EngineError) as exc:
        Engine.load(
            workspace.runtime,
            streams_path=paths["streams"],
            partitions_path=paths["partitions"],
            quotas_path=paths["quotas"],
            tokens_path=paths["tokens"],
        )
    assert _exception_code(exc) == expected_code
    assert not workspace.runtime.exists()


def test_unknown_event_stream_writes_nothing(workspace) -> None:
    engine, _paths = workspace.create()
    event_path = workspace.write_events(
        "bad-events.json",
        [
            event(event_id="valid"),
            event(metric="missing.metric", event_id="bad"),
        ],
    )
    with pytest.raises(EngineError) as exc:
        engine.ingest_file(event_path)
    assert exc.value.code == "E_BAD_STREAM"
    conn = engine.store._connect()
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"]
        version = conn.execute("SELECT version FROM ledger_pointer WHERE id=1").fetchone()[
            "version"
        ]
    finally:
        conn.close()
    assert count == 0
    assert version == 1


def test_configuration_hash_and_route_are_bit_reproducible(tmp_path) -> None:
    first = workspace_paths = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    from conftest import WorkspaceBuilder

    one = WorkspaceBuilder(first)
    two = WorkspaceBuilder(second)
    config_one = one.write_config()
    config_two = two.write_config()
    parsed_one = load_configuration(
        config_one["streams"],
        config_one["partitions"],
        config_one["quotas"],
        config_one["tokens"],
    )
    parsed_two = load_configuration(
        config_two["streams"],
        config_two["partitions"],
        config_two["quotas"],
        config_two["tokens"],
    )
    assert parsed_one.config_hash == parsed_two.config_hash
    assert [
        (stream.partition, stream.route_hash) for stream in parsed_one.streams
    ] == [(stream.partition, stream.route_hash) for stream in parsed_two.streams]


def test_partition_sequence_and_global_manifest_are_stable(workspace) -> None:
    engine, _paths = workspace.create(
        partitions={**DEFAULT_PARTITIONS, "partition_count": 2}
    )
    events = [
        event(logical_time_ms=2000, sample_seq=2, event_id="e2"),
        event(logical_time_ms=1000, sample_seq=3, event_id="e3"),
        event(logical_time_ms=1000, sample_seq=1, event_id="e1"),
    ]
    report = engine.ingest_document({"events": events})
    assert report["count"] == 3
    assert [item["sample_id"] for item in report["decisions"]] == ["e1", "e3", "e2"]
    verify = engine.verify()
    assert verify["ok"] is True
    keys = [entry["global_order_key"] for entry in verify["manifest"]]
    assert keys == sorted(keys)
    conn = engine.store._connect()
    try:
        rows = conn.execute(
            "SELECT partition, partition_arrival_seq FROM samples "
            "ORDER BY partition, partition_arrival_seq"
        ).fetchall()
    finally:
        conn.close()
    by_partition: dict[int, list[int]] = {}
    for row in rows:
        by_partition.setdefault(int(row["partition"]), []).append(
            int(row["partition_arrival_seq"])
        )
    for sequences in by_partition.values():
        assert sequences == list(range(1, len(sequences) + 1))


def test_stale_expected_version_is_rejected(workspace) -> None:
    engine, _paths = workspace.create()
    with pytest.raises(EngineError) as exc:
        with engine.store.transaction("stale", expected_version=999):
            pass
    assert exc.value.code == "E_STALE_VERSION"

