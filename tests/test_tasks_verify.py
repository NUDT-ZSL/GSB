import threading

import pytest

from hotcfg.center import ConfigCenter
from hotcfg.errors import BizError, E_VERIFY, E_VERSION
from hotcfg.loader import ConfigItem, TaskSpec
from hotcfg.tasks import ComputeTask, RESULT_SCALE
from hotcfg.verify import verify_results


def make(expected_version=1):
    items = {
        "a": ConfigItem("a", "int", 2, 1.5),
        "b": ConfigItem("b", "float", 0.5, 1.0),
    }
    series = {"a": [1.0, 3.0], "b": [2.0, 4.0]}
    center = ConfigCenter(items)
    spec = TaskSpec(id="t1", keys=("a", "b"), aggregation="weighted_sum",
                    expected_version=expected_version)
    task = ComputeTask(spec, series)
    center.subscribe("t1", spec.keys, expected_version, handler=task)
    return center, task


def test_result_matches_declared_version():
    center, task = make(expected_version=1)
    result = task.run(center, 1)
    assert result["version"] == 1
    assert result["keys"] == ["a", "b"]
    assert result["inputs"]["a"] == {"value": 2, "weight": 1.5}
    # weighted_sum = 1.5*2*(1+3) + 1.0*0.5*(2+4) = 12 + 3 = 15
    assert result["value_fp"] == int(round(15.0 * RESULT_SCALE))
    assert result["scale"] == RESULT_SCALE


def test_version_mismatch_rejected_no_silent_latest():
    center, task = make(expected_version=1)
    center.commit_changes({"a": 99})
    with pytest.raises(BizError) as ei:
        task.run(center, 2)  # 声明 v1, 要求跑 v2 -> 拒绝
    assert ei.value.code == E_VERSION
    assert ei.value.details["expected_version"] == 1
    assert ei.value.details["requested_version"] == 2


def test_results_single_version_under_concurrent_commits():
    items = {"a": ConfigItem("a", "int", 1, 1.0)}
    series = {"a": [1.0, 1.0]}
    center = ConfigCenter(items)
    results, errors = [], []

    def committer():
        for i in range(2, 20):
            center.commit_changes({"a": i})

    def worker(tid):
        spec = TaskSpec(id=tid, keys=("a",), aggregation="sum")
        task = ComputeTask(spec, series)
        for _ in range(50):
            v = center.current_version
            try:
                digest = task.compute_digest(center.get_effective(v, keys=("a",)), v)
                results.append(digest)
            except BizError as exc:  # pragma: no cover
                errors.append(exc)

    threads = [threading.Thread(target=committer)] + [
        threading.Thread(target=worker, args=(f"t{i}",)) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    # 每个结果只引用单一完整版本: 重算必须全部一致
    tasks = {}
    for r in results:
        tid = r["task_id"]
        if tid not in tasks:
            tasks[tid] = ComputeTask(TaskSpec(id=tid, keys=("a",), aggregation="sum"), series)
    report = verify_results(center, tasks, results)
    assert report["checked"] == len(results) > 0


def test_verify_detects_tampered_result():
    center, task = make(expected_version=1)
    result = task.run(center, 1)
    tampered = dict(result, value_fp=result["value_fp"] + 1)
    center.commit_changes({"a": 5})
    with pytest.raises(BizError) as ei:
        verify_results(center, {"t1": task}, [tampered])
    assert ei.value.code == E_VERIFY
    assert ei.value.details["task_id"] == "t1"
    assert ei.value.details["expected_version"] == 1
    assert ei.value.details["actual_version"] == 2
