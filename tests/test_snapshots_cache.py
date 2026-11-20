from __future__ import annotations

import pytest

from mtstream.cache import prefix_cache_key
from mtstream.errors import (
    E_CACHE_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    MTStreamError,
)
from mtstream.models import DecodeConfig
from mtstream.session import StreamingSession


def test_snapshots_are_monotonic_and_whole_object_replaced(case, config):
    session = StreamingSession(case, config)
    before = session.snapshots.get("block_size")
    content = dict(before.content)
    content["block_size"] = 2
    after = session.snapshots.replace_snapshot("block_size", content)
    assert after.version == before.version + 1
    assert after.object_id != before.object_id
    assert session.snapshots.get("block_size").checksum == after.checksum


def test_snapshot_rollback_and_same_version_mutation_are_rejected(case, config):
    session = StreamingSession(case, config)
    current = session.snapshots.get("block_size")
    with pytest.raises(MTStreamError) as old_version:
        session.snapshots.replace_snapshot(
            "block_size",
            {"block_size": 99},
            version=current.version - 1,
        )
    assert old_version.value.code == E_SNAPSHOT_ROLLBACK
    assert old_version.value.field == "block_size"

    with pytest.raises(MTStreamError) as same_version:
        session.snapshots.replace_snapshot(
            "block_size",
            {"block_size": 99},
            version=current.version,
        )
    assert same_version.value.code == E_SNAPSHOT_ROLLBACK


def test_snapshot_corruption_is_detected_on_read(case, config):
    session = StreamingSession(case, config)
    snapshot = session.snapshots.get("vocabulary")
    session.snapshots.tamper_snapshot(
        "vocabulary",
        lambda content: content["source_vocab"].append("tampered"),
    )
    with pytest.raises(MTStreamError) as caught:
        session.snapshots.get("vocabulary")
    assert caught.value.code == E_CACHE_CORRUPT
    assert caught.value.data["entry_key"] == "snapshot:vocabulary"
    assert caught.value.data["version"] == snapshot.version


def test_cache_key_contains_required_dependencies_and_is_verifiable(case, config):
    session = StreamingSession(case, config)
    session.request_block(0)
    entries = session.cache_entries()
    assert entries
    required = {
        "source_encoding_digest",
        "align_version",
        "block_size",
        "context_window",
        "upstream_node_hash",
    }
    for entry in entries:
        assert required <= set(entry.metadata)
        assert prefix_cache_key(entry.metadata) == entry.key
        assert entry.semantic_hash() == entry.payload_hash
        assert entry.state().state_hash == entry.payload["state"]["state_hash"]


def test_cache_payload_corruption_contains_entry_key_and_version(case, config):
    session = StreamingSession(case, config)
    session.request_block(0)
    entry = next(
        item for item in session.cache_entries() if item.boundary_step == 2
    )
    session.prefix_cache_store().tamper_entry(
        entry.key,
        lambda payload: payload["state"].update(
            {"normalizer_sum": payload["state"]["normalizer_sum"] + 1.0}
        ),
    )
    with pytest.raises(MTStreamError) as caught:
        session.prefix_cache_store().get(entry.key)
    assert caught.value.code == E_CACHE_CORRUPT
    assert caught.value.data["entry_key"] == entry.key
    assert caught.value.data["version"] == entry.version
    assert caught.value.data["category"] == "payload"


def test_cache_key_metadata_mutation_is_detected(case, config):
    session = StreamingSession(case, config)
    session.request_block(0)
    entry = session.cache_entries()[1]
    payload = entry.to_dict()
    payload["metadata"]["context_window"] += 1
    # Reconstructing a valid dataclass with broken metadata still must fail key
    # verification on read.
    broken = type(entry)(
        key=entry.key,
        boundary_step=entry.boundary_step,
        metadata=payload["metadata"],
        payload=payload["payload"],
        payload_hash=entry.payload_hash,
        version=entry.version,
    )
    session.prefix_cache_store()._entries[entry.key] = broken
    with pytest.raises(MTStreamError) as caught:
        session.prefix_cache_store().get(entry.key)
    assert caught.value.code == E_CACHE_CORRUPT
    assert caught.value.data["category"] == "key"

