import threading

import pytest

from hotcfg.center import ConfigCenter
from hotcfg.errors import BizError, E_COMMIT, E_SUB, E_VERSION
from hotcfg.loader import ConfigItem


def make_center():
    items = {
        "a": ConfigItem("a", "int", 1, 1.0),
        "b": ConfigItem("b", "float", 0.5, 2.0),
        "c": ConfigItem("c", "int", 10, 1.0),
    }
    return ConfigCenter(items)


def test_version_chain_monotonic_with_parent():
    center = make_center()
    v2 = center.commit_changes({"a": 2})
    v3 = center.commit_changes({"b": 1.5})
    assert (v2, v3) == (2, 3)
    records = center.records()
    assert [r.version for r in records] == [1, 2, 3]
    assert records[1].parent == 1 and records[2].parent == 2
    assert records[1].changed_keys == ("a",)
    assert records[2].changed_keys == ("b",)


def test_atomic_read_full_effective_set():
    center = make_center()
    center.commit_changes({"a": 5, "b": 2.5})
    eff = center.get_effective(2)
    assert eff["a"]["value"] == 5 and eff["b"]["value"] == 2.5
    assert eff["c"]["value"] == 10  # 未变更键保持原值, 完整集合
    old = center.get_effective(1)
    assert old["a"]["value"] == 1  # 历史版本不受新提交影响


def test_commit_failure_keeps_original_version():
    center = make_center()
    with pytest.raises(BizError) as ei:
        center.stage({"a": "not-an-int"})
    assert ei.value.code == E_COMMIT
    assert center.current_version == 1
    with pytest.raises(BizError) as ei:
        center.stage({"ghost": 1})
    assert ei.value.code == E_COMMIT
    with pytest.raises(BizError) as ei:
        center.commit()  # 无暂存内容
    assert ei.value.code == E_COMMIT
    assert center.current_version == 1


def test_no_mixed_state_under_concurrent_commits():
    center = make_center()
    errors = []

    def committer():
        for i in range(2, 30):
            try:
                center.commit_changes({"a": i, "b": float(i)})
            except BizError as exc:  # pragma: no cover
                errors.append(exc)

    def reader():
        for _ in range(200):
            v = center.current_version
            eff = center.get_effective(v)
            av, bv = eff["a"]["value"], eff["b"]["value"]
            if v >= 2 and av != int(bv):
                errors.append(AssertionError(f"mixed state at v{v}: a={av} b={bv}"))

    threads = [threading.Thread(target=committer)] + [
        threading.Thread(target=reader) for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


def test_unknown_version_read_rejected():
    center = make_center()
    with pytest.raises(BizError) as ei:
        center.get_effective(99)
    assert ei.value.code == E_VERSION


def test_subscribe_unknown_key():
    center = make_center()
    with pytest.raises(BizError) as ei:
        center.subscribe("t", ["a", "ghost"], 1)
    assert ei.value.code == E_SUB
