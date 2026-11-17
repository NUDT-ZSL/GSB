from hotcfg.center import ConfigCenter
from hotcfg.errors import E_PUSH
from hotcfg.loader import ConfigItem, TaskSpec
from hotcfg.tasks import ComputeTask


def make():
    items = {
        "a": ConfigItem("a", "int", 1, 1.0),
        "b": ConfigItem("b", "float", 0.5, 2.0),
    }
    series = {"a": [1.0, 2.0, 3.0], "b": [0.5, 1.5]}
    center = ConfigCenter(items)
    spec = TaskSpec(id="t1", keys=("a",), aggregation="sum")
    task = ComputeTask(spec, series)
    center.subscribe("t1", spec.keys, 1, handler=task)
    return center, task


def test_push_carries_version_and_changed_keys():
    center, task = make()
    center.commit_changes({"a": 7})
    notif = task.last_notification
    assert notif["version"] == 2
    assert notif["changed_keys"] == ["a"]
    # 推送后任务已原子取到该版本完整生效值集合
    assert set(notif["effective"]) == {"a", "b"}
    assert notif["effective"]["a"]["value"] == 7
    acks = center.acks
    assert len(acks) == 1 and acks[0]["task_id"] == "t1" and acks[0]["version"] == 2


def test_push_only_to_affected_subscribers():
    center, task = make()
    center.commit_changes({"b": 9.9})  # t1 只订阅 a
    assert task.last_notification is None
    assert center.acks == []


def test_push_failure_recorded_and_retry_skips_intermediate():
    center, task = make()
    center.fail_push_once.add("t1")
    center.commit_changes({"a": 2})  # v2 推送失败
    assert center.push_errors and center.push_errors[0]["error"]["code"] == E_PUSH
    assert task.last_notification is None
    center.commit_changes({"a": 3})  # v3 推送成功
    assert task.last_notification["version"] == 3
    center.fail_push_once.add("t1")
    center.commit_changes({"a": 4})  # v4 推送失败
    center.retry_push("t1")  # 重试只投递最新版本 v4
    assert task.last_notification["version"] == 4
    seen = [a["version"] for a in center.acks]
    assert seen == [3, 4]  # 任务从未观察到中间版本


def test_push_ack_timeout_injection():
    center, task = make()
    center.ack_timeout_ms = 0.5
    center.slow_push_ms["t1"] = 20.0
    center.commit_changes({"a": 5})
    assert center.push_errors[0]["error"]["code"] == E_PUSH
    assert "timeout" in center.push_errors[0]["error"]["message"]
