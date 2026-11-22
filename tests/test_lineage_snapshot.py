import copy
import json
import os
import threading

import pytest

from tts_app.dag import Engine
from tts_app.errors import (E_LINEAGE_BROKEN, E_SNAPSHOT_ROLLBACK,
                            E_STAGE_BUSY, TtsError)
from tts_app.lineage import verify_lineage
from tts_app.snapshot import SnapshotStore


def test_lineage_replay_ok(engine, tasks):
    engine.run_all(tasks)
    v = verify_lineage(engine.lineage_dir, "utt_001", engine, tasks[0],
                       engine.cache)
    assert v["ok"]
    assert v["config_version"] == "v2"


def test_lineage_missing(engine, tasks, tmp_path):
    engine.run_all(tasks)
    with pytest.raises(TtsError) as ei:
        verify_lineage(str(tmp_path / "nowhere"), "utt_404", engine,
                       tasks[0], engine.cache)
    assert ei.value.code == E_LINEAGE_BROKEN
    assert ei.value.details["reason"] == "lineage_missing"


def test_lineage_stale_dependency(engine, tasks, speakers):
    engine.run_all(tasks)
    spk2 = copy.deepcopy(speakers)
    spk2["spk_alpha"]["f0_base"] += 7.0
    stale = Engine(engine.cache, spk2, engine.config,
                   lineage_dir=engine.lineage_dir)
    with pytest.raises(TtsError) as ei:
        verify_lineage(engine.lineage_dir, "utt_001", stale, tasks[0],
                       engine.cache)
    assert ei.value.code == E_LINEAGE_BROKEN
    assert ei.value.details["utterance_id"] == "utt_001"
    deps = [d["dependency"] for d in ei.value.details["changed_dependencies"]]
    assert "speaker_params_hash" in deps


def test_lineage_replay_mismatch(engine, tasks):
    engine.run_all(tasks)
    path = os.path.join(engine.lineage_dir, "utt_001.lineage.json")
    with open(path, encoding="utf-8") as fh:
        rec = json.load(fh)
    rec["pcm_sha256"] = "0" * 64
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    with pytest.raises(TtsError) as ei:
        verify_lineage(engine.lineage_dir, "utt_001", engine, tasks[0],
                       engine.cache)
    assert ei.value.code == E_LINEAGE_BROKEN
    assert ei.value.details["reason"] == "replay_mismatch"


def test_snapshot_versioned_replace(tmp_path):
    store = SnapshotStore(str(tmp_path / "snap"))
    store.commit({"a": 1}, 1)
    store.commit({"a": 2}, 2)
    current = store.current()
    assert current["version"] == 2
    assert current["checksum_valid"]
    assert current["data"] == {"a": 2}
    files = [f for f in os.listdir(store.root) if f.startswith("snapshot_v")]
    assert files == ["snapshot_v2.json"]


def test_snapshot_rollback(tmp_path):
    store = SnapshotStore(str(tmp_path / "snap"))
    store.commit({"a": 1}, 2)
    with pytest.raises(TtsError) as ei:
        store.commit({"a": 0}, 1)
    assert ei.value.code == E_SNAPSHOT_ROLLBACK
    assert ei.value.details["current_version"] == 2
    with pytest.raises(TtsError):
        store.commit({"a": 1}, 2)


def test_stage_busy_concurrent(tmp_path):
    store = SnapshotStore(str(tmp_path / "snap"))
    errors = []

    def commit(version):
        try:
            store.commit({"v": version}, version)
        except TtsError as exc:
            errors.append(exc.code)

    with store.stage_lock:
        t = threading.Thread(target=commit, args=(1,))
        t.start()
        t.join()
    assert errors == [E_STAGE_BUSY]
