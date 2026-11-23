import json

import pytest

from mt_pipeline.errors import Codes, PipelineError
from mt_pipeline.snapshots import SnapshotStore


def test_create_noop_replace_cycle(tmp_path):
    store = SnapshotStore(tmp_path)
    assert store.save("vocab", 1, ["a", "b"]) == "created"
    assert store.save("vocab", 1, ["a", "b"]) == "noop"
    assert store.save("vocab", 2, ["a", "b", "c"]) == "replaced"
    assert store.load("vocab") == ["a", "b", "c"]
    assert store.version("vocab") == 2


def test_checksum_change_same_version_triggers_full_replace(tmp_path):
    store = SnapshotStore(tmp_path)
    store.save("filter", 1, {"noise_max": 0.2})
    assert store.save("filter", 1, {"noise_max": 0.3}) == "replaced"
    assert store.load("filter") == {"noise_max": 0.3}


def test_rollback_rejected(tmp_path):
    store = SnapshotStore(tmp_path)
    store.save("vocab", 3, ["x"])
    with pytest.raises(PipelineError) as exc:
        store.save("vocab", 2, ["x"])
    assert exc.value.code == Codes.SNAPSHOT_ROLLBACK
    assert store.version("vocab") == 3  # old version must not overwrite


def test_corrupt_snapshot_unparseable(tmp_path):
    store = SnapshotStore(tmp_path)
    (tmp_path / "vocab.snapshot.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(PipelineError) as exc:
        store.load("vocab")
    assert exc.value.code == Codes.SNAPSHOT_CORRUPT


def test_corrupt_snapshot_checksum_mismatch(tmp_path):
    store = SnapshotStore(tmp_path)
    store.save("mix", 1, {"news": 0.5})
    path = tmp_path / "mix.snapshot.json"
    snap = json.loads(path.read_text(encoding="utf-8"))
    snap["content"]["news"] = 0.9  # tamper without updating checksum
    path.write_text(json.dumps(snap), encoding="utf-8")
    with pytest.raises(PipelineError) as exc:
        store.load("mix")
    assert exc.value.code == Codes.SNAPSHOT_CORRUPT


def test_restart_load_detects_version_change(tmp_path):
    store = SnapshotStore(tmp_path)
    store.save("vocab", 1, ["a"])
    reloaded = SnapshotStore(tmp_path)  # simulate restart
    assert reloaded.version("vocab") == 1
    assert reloaded.save("vocab", 2, ["a", "b"]) == "replaced"
    assert SnapshotStore(tmp_path).load("vocab") == ["a", "b"]
