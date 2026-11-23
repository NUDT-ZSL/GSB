from __future__ import annotations

import copy
from pathlib import Path

import pytest

from gateway.engine import GatewayEngine
from gateway.errors import (
    E_BACKLOG_LIMIT,
    E_BAD_SAMPLE,
    E_CLOCK_REGRESSION,
    GatewayError,
)

from .conftest import read_sample, one_partition_docs


@pytest.mark.parametrize(
    ("document_name", "position"),
    [
        ("topics_duplicate_name.json", 0),
        ("topics_zero_partition.json", 0),
        ("events_negative_clock.json", 1),
        ("events_zero_generation.json", 1),
        ("subscriptions_duplicate_id.json", 2),
        ("config_bad_backoff.json", 3),
        ("config_zero_scale.json", 3),
    ],
)
def test_invalid_samples_return_bad_sample_without_creating_database(
    tmp_path: Path,
    document_name: str,
    position: int,
) -> None:
    docs = [
        read_sample("topics.json"),
        read_sample("events.json"),
        read_sample("subscriptions.json"),
        read_sample("config.json"),
    ]
    docs[position] = read_sample(f"invalid/{document_name}")
    db = tmp_path / "invalid.db"

    with pytest.raises(GatewayError) as error:
        GatewayEngine(db).load_documents(*docs)

    assert error.value.code == E_BAD_SAMPLE
    assert not db.exists()


def test_clock_regression_is_rejected_before_persistence(tmp_path: Path) -> None:
    topics, events, subscriptions, config = one_partition_docs(event_count=2)
    events["events"][0]["injected_clock_ms"] = 100
    events["events"][1]["injected_clock_ms"] = 0
    db = tmp_path / "clock.db"

    with pytest.raises(GatewayError) as error:
        GatewayEngine(db).load_documents(
            topics,
            events,
            subscriptions,
            config,
        )

    assert error.value.code == E_CLOCK_REGRESSION
    assert not db.exists()


def test_backlog_limit_is_rejected_before_persistence(tmp_path: Path) -> None:
    topics, events, subscriptions, config = one_partition_docs(
        event_count=3,
        backlog_limit=2,
    )
    db = tmp_path / "backlog.db"

    with pytest.raises(GatewayError) as error:
        GatewayEngine(db).load_documents(
            topics,
            events,
            subscriptions,
            config,
        )

    assert error.value.code == E_BACKLOG_LIMIT
    assert not db.exists()


def test_partition_key_normalization_and_bool_rejection(tmp_path: Path) -> None:
    topics, events, subscriptions, config = one_partition_docs()
    events["events"][0]["partition_key"] = "  ｓａｍｅ－ｋｅｙ  "
    engine = GatewayEngine(tmp_path / "normalized.db")
    result = engine.load_documents(topics, events, subscriptions, config)
    assert result["ingested_events"] == 1
    assert result["partition_count"] == 1

    bad_topics, bad_events, bad_subscriptions, bad_config = one_partition_docs()
    bad_topics["topics"][0]["partition_count"] = True
    db = tmp_path / "bool.db"
    with pytest.raises(GatewayError) as error:
        GatewayEngine(db).load_documents(
            bad_topics,
            bad_events,
            bad_subscriptions,
            bad_config,
        )
    assert error.value.code == E_BAD_SAMPLE
    assert not db.exists()


def test_duplicate_produce_content_conflict(tmp_path: Path) -> None:
    topics, events, subscriptions, config = one_partition_docs()
    engine = GatewayEngine(tmp_path / "conflict.db")
    engine.load_documents(topics, events, subscriptions, config)
    changed = copy.deepcopy(events)
    changed["events"][0]["payload_digest"] = "sha256:changed"

    with pytest.raises(GatewayError) as error:
        engine.produce(changed)
    assert error.value.code == "E_EVENT_CONFLICT"
