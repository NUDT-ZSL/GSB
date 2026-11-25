from __future__ import annotations

import json
import threading

from transcript_governance.cache import CacheKey, ContentAddressedCache
from transcript_governance.codes import Code
from transcript_governance.models import Utterance
from transcript_governance.snapshot import SnapshotStore, build_snapshot


def test_content_addressed_cache_hits_misses_and_replay(tmp_path) -> None:
    cache = ContentAddressedCache(tmp_path / "cache")
    parent_key = CacheKey.build("parent", pcm_hash="pcm", config={"a": 1})
    child_key = CacheKey.build("child", transcript_digest="digest", upstream_hashes={"parent": parent_key.key})

    _, first_hit = cache.get_or_compute(parent_key, lambda: {"value": 1})
    cache.put(child_key, {"child": True}, dependencies=[parent_key.key])
    cached, second_hit = cache.get_or_compute(parent_key, lambda: {"value": 2})

    assert first_hit is False
    assert second_hit is True
    assert cached == {"value": 1}
    replay = cache.replay_chain(child_key.key)
    assert replay["ok"]
    assert [item["key"] for item in replay["chain"]] == [parent_key.key, child_key.key]


def test_interval_targeted_invalidation_keeps_unrelated_objects(tmp_path) -> None:
    cache = ContentAddressedCache(tmp_path / "cache")
    target = CacheKey.build("unit", config={"x": 1}, interval=(0, 100))
    far = CacheKey.build("unit", config={"x": 1}, interval=(300, 400))
    global_key = CacheKey.build("global", config={"x": 1})
    cache.put(target, {"id": "target"}, interval=(0, 100))
    cache.put(far, {"id": "far"}, interval=(300, 400))
    cache.put(global_key, {"global": True})

    removed = cache.invalidate_intervals([(80, 130)], namespace="unit")
    assert removed == [target.key]
    assert not cache.has(target.key)
    assert cache.has(far.key)
    assert cache.has(global_key.key)


def test_snapshot_versions_checksums_stage_busy_and_recovery(tmp_path) -> None:
    store = SnapshotStore(tmp_path / "snapshot")
    utterances = [Utterance("A", 0, 10, "a", confidence=1.0)]
    snapshot1 = build_snapshot(1, utterances, config={"k": "v"}, declared_regions=[(0, 10)])
    store.initialize(snapshot1)

    first = store.try_stage()
    second = store.try_stage()
    assert first.ok and first.lease is not None
    assert not second.ok and second.code == Code.E_STAGE_BUSY
    snapshot2 = build_snapshot(2, utterances, config={"k": "v2"}, declared_regions=[(0, 10)])
    busy_commit = store.commit(snapshot2, expected_version=1, expected_checksum=snapshot1.checksum)
    assert busy_commit.code == Code.E_STAGE_BUSY

    committed = first.lease.commit(snapshot2, expected_version=1, expected_checksum=snapshot1.checksum)
    assert committed.ok and committed.snapshot is not None

    stale_attempt = store.try_stage()
    assert stale_attempt.ok and stale_attempt.lease is not None
    stale = stale_attempt.lease.commit(
        build_snapshot(3, utterances, config={"stale": True}),
        expected_version=1,
        expected_checksum=snapshot1.checksum,
    )
    assert stale.code == Code.E_SNAPSHOT_VERSION_CONFLICT

    checksum_attempt = store.try_stage()
    assert checksum_attempt.ok and checksum_attempt.lease is not None
    bad_checksum = checksum_attempt.lease.commit(
        build_snapshot(3, utterances, config={"k": "v3"}),
        expected_version=2,
        expected_checksum="0" * 64,
    )
    assert bad_checksum.code == Code.E_SNAPSHOT_CHECKSUM_MISMATCH

    current = store.load()
    recovery_attempt = store.try_stage()
    assert recovery_attempt.ok and recovery_attempt.lease is not None and current is not None
    recovered = recovery_attempt.lease.commit(
        build_snapshot(3, current.utterances, config={"k": "v3"}, declared_regions=current.declared_regions),
        expected_version=current.version,
        expected_checksum=current.checksum,
    )
    assert recovered.ok
    assert store.load().version == 3


def test_snapshot_concurrent_thread_receives_busy_without_waiting(tmp_path) -> None:
    store = SnapshotStore(tmp_path / "snapshot")
    snapshot = build_snapshot(1, [Utterance("A", 0, 1, "a", confidence=1.0)])
    store.initialize(snapshot)
    held = store.try_stage()
    assert held.ok

    outcome: dict[str, str] = {}

    def attempt() -> None:
        result = store.try_stage()
        outcome["code"] = result.code

    thread = threading.Thread(target=attempt)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert outcome["code"] == Code.E_STAGE_BUSY
    held.lease.release()


def test_diagnostic_report_passes_end_to_end(tmp_path) -> None:
    from transcript_governance.diagnose import run_diagnostic

    report = run_diagnostic(tmp_path / "samples", tmp_path / "reports")
    assert report["overall_ok"] is True
    assert report["summary"]["failed"] == 0
    data = json.loads((tmp_path / "reports" / "diagnostic-report.json").read_text(encoding="utf-8"))
    assert data["overall_ok"] is True
