"""两代快照合并规则与冲突定位测试."""

from castdiag.merge import load_v1_records, merge_records


def _rec(screen, generation, ts, keyframe, play, tombstone=False, source="v2:meta.db#x"):
    return {
        "screen_id": screen,
        "generation": generation,
        "ts_ms": ts,
        "keyframe_seq": keyframe,
        "play_frame_no": play,
        "tolerance_ms": 500,
        "tombstone": tombstone,
        "flags": [],
        "source": source,
    }


def test_newer_generation_wins():
    old = _rec("tv", 1, 1000, 10, 12, source="v1:snapshot_v1.json")
    new = _rec("tv", 2, 2000, 60, 66)
    view, conflicts = merge_records([old, new])
    assert view["tv"]["generation"] == 2
    assert view["tv"]["keyframe_seq"] == 60
    fields = {(c["screen_id"], c["field"]) for c in conflicts}
    assert ("tv", "keyframe_seq") in fields
    assert ("tv", "play_frame_no") in fields
    kept = [c for c in conflicts if c["field"] == "keyframe_seq"][0]["kept"]
    assert kept["source"] == "v2:meta.db#x" and kept["value"] == 60


def test_tombstone_beats_live_same_generation():
    live = _rec("tv", 2, 3000, 60, 66, tombstone=False)
    dead = _rec("tv", 2, 1000, 5, 5, tombstone=True, source="v2:meta.db#tv")
    view, _ = merge_records([live, dead])
    assert view["tv"]["tombstone"] is True


def test_same_generation_newer_time_wins():
    older = _rec("tv", 2, 1000, 10, 10)
    newer = _rec("tv", 2, 2000, 20, 20, source="v2:meta.db#tv2")
    view, _ = merge_records([older, newer])
    assert view["tv"]["ts_ms"] == 2000


def test_v1_rel_seconds_converted(sample_dir):
    records = load_v1_records(sample_dir / "snapshots" / "v1" / "snapshot_v1.json")
    by_screen = {r["screen_id"]: r for r in records}
    assert by_screen["living-tv"]["ts_ms"] == 1485
    assert by_screen["meeting-sub"]["ts_ms"] == 1820
    assert by_screen["bedroom-tv"]["ts_ms"] == 495
    assert all(r["generation"] == 1 for r in records)


def test_merge_generated_snapshots(sample_dir):
    from castdiag.pipeline import prepare

    prep = prepare(sample_dir)
    view, conflicts = prep["view"], prep["conflicts"]
    assert set(view) == {"living-tv", "meeting-sub", "bedroom-tv"}
    assert view["bedroom-tv"]["tombstone"] is True
    assert view["living-tv"]["generation"] == 2
    assert len(conflicts) > 0
    for item in conflicts:
        assert item["kept"]["source"] and item["dropped"]["source"]
