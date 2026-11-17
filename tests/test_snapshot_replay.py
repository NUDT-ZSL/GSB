import json

import pytest

from hotcfg.center import ConfigCenter
from hotcfg.errors import BizError, E_REPLAY
from hotcfg.loader import ConfigItem, TaskSpec
from hotcfg.replay import replay_snapshot
from hotcfg.snapshot import restore_session, save_snapshot
from hotcfg.tasks import ComputeTask

ITEMS = {
    "a": ConfigItem("a", "int", 1, 1.0),
    "b": ConfigItem("b", "float", 0.5, 2.0),
}
SERIES = {"a": [1.0, 2.0, 3.0], "b": [0.5, 1.5, 2.5]}


def make_session():
    center = ConfigCenter(ITEMS)
    task = ComputeTask(TaskSpec(id="t1", keys=("a", "b"), aggregation="sum"), SERIES)
    center.subscribe("t1", ("a", "b"), 1, handler=task)
    results = []
    for i in range(2, 5):
        v = center.commit_changes({"a": i})
        center.update_subscription("t1", v)
        results.append(task.compute_digest(center.get_effective(v), v))
        for _ in range(3):
            center.query("t1", v, keys=("a", "b"))
    return center, task, results


def test_snapshot_roundtrip_and_full_replace(tmp_path):
    center, task, results = make_session()
    snap_path = tmp_path / "snap.json"
    save_snapshot(center, results, snap_path)

    # 当前会话与快照不一致 (全新 v1 会话) -> 整体替换
    fresh = ConfigCenter(ITEMS)
    restored, restored_results, replaced = restore_session(snap_path, fresh)
    assert replaced is True
    assert restored.chain_digest() == center.chain_digest()
    assert restored.current_version == 4
    assert restored_results == results
    assert restored.subscriptions()["t1"]["expected_version"] == 4

    # 恢复后可继续接受新版本提交
    v5 = restored.commit_changes({"a": 100})
    assert v5 == 5
    assert restored.get_effective(5)["a"]["value"] == 100
    assert restored.get_effective(4)["a"]["value"] == 4  # 历史版本不变


def test_restore_same_digest_reuses_session(tmp_path):
    center, task, results = make_session()
    snap_path = tmp_path / "snap.json"
    save_snapshot(center, results, snap_path)
    same, _, replaced = restore_session(snap_path, center)
    assert replaced is False
    assert same is center


def test_replay_ok(tmp_path):
    center, task, results = make_session()
    snap_path = tmp_path / "snap.json"
    save_snapshot(center, results, snap_path)
    report = replay_snapshot(snap_path, SERIES)
    assert report["ok"] is True
    assert report["versions_checked"] == 4
    assert report["results_checked"] == len(results)


def test_replay_detects_tampered_version_chain(tmp_path):
    center, task, results = make_session()
    snap_path = tmp_path / "snap.json"
    save_snapshot(center, results, snap_path)
    doc = json.loads(snap_path.read_text(encoding="utf-8"))
    doc["versions"][1]["effective"]["a"]["value"] = 999  # 篡改 v2 生效值
    snap_path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(BizError) as ei:
        replay_snapshot(snap_path, SERIES)
    assert ei.value.code == E_REPLAY
    assert ei.value.details["basis"]


def test_replay_detects_tampered_result(tmp_path):
    center, task, results = make_session()
    snap_path = tmp_path / "snap.json"
    save_snapshot(center, results, snap_path)
    doc = json.loads(snap_path.read_text(encoding="utf-8"))
    doc["results"][0]["value_fp"] += 1
    snap_path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(BizError) as ei:
        replay_snapshot(snap_path, SERIES)
    assert ei.value.code == E_REPLAY
    assert "result" in ei.value.details["basis"]
