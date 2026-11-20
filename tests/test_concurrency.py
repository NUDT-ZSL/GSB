import threading

from graymesh.clock import LogicalClock
from graymesh.controlplane import ControlPlane
from graymesh.errors import BusinessError, ErrorCode
from graymesh.store import RuleStore

from tests.conftest import bootstrap


def make_objects(f):
    return [
        f["member"]("m1", 9001, 1),
        f["group"]("g1", ["m1"]),
        f["route"]("r1", "g1", gray_percent=100),
        f["virtual_service"]("vs1", ["r1"]),
    ]


def test_concurrent_distinct_patches_get_unique_versions(tmp_path, object_factory):
    store = RuleStore(str(tmp_path / "db.sqlite"))
    control = ControlPlane(store, LogicalClock())
    bootstrap(control, make_objects(object_factory))

    results, errors = [], []

    def submit(i):
        try:
            patch = {"patch_id": "p-conc-%d" % i, "expected_version": None, "ops": [
                {"op": "replace", "kind": "upstream_member", "id": "m1",
                 "object": object_factory["member"]("m1", 9001, i + 1)}]}
            results.append(control.submit_patch(patch)["version"])
        except BusinessError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert sorted(results) == list(range(2, 10))  # 8 distinct versions, no gaps
    assert store.current_version() == 9


def test_duplicate_patch_id_commits_once(tmp_path, object_factory):
    store = RuleStore(str(tmp_path / "db.sqlite"))
    control = ControlPlane(store, LogicalClock())
    bootstrap(control, make_objects(object_factory))

    patch = {"patch_id": "p-dup", "expected_version": None, "ops": [
        {"op": "replace", "kind": "upstream_member", "id": "m1",
         "object": object_factory["member"]("m1", 9001, 3)}]}
    outcomes = []

    def submit():
        outcomes.append(control.submit_patch(patch))

    threads = [threading.Thread(target=submit) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    versions = {o["version"] for o in outcomes}
    applied = [o["applied"] for o in outcomes]
    assert versions == {2}
    assert applied.count(True) == 1
    assert applied.count(False) == 5
    assert store.current_version() == 2


def test_expected_version_conflict_detected(tmp_path, object_factory):
    store = RuleStore(str(tmp_path / "db.sqlite"))
    control = ControlPlane(store, LogicalClock())
    bootstrap(control, make_objects(object_factory))

    barrier = threading.Barrier(2)
    outcomes = {}

    def submit(name, weight):
        patch = {"patch_id": name, "expected_version": 1, "ops": [
            {"op": "replace", "kind": "upstream_member", "id": "m1",
             "object": object_factory["member"]("m1", 9001, weight)}]}
        barrier.wait()
        try:
            outcomes[name] = ("ok", control.submit_patch(patch)["version"])
        except BusinessError as exc:
            outcomes[name] = ("error", exc.code)

    t1 = threading.Thread(target=submit, args=("p-a", 2))
    t2 = threading.Thread(target=submit, args=("p-b", 3))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    results = sorted(outcomes.values())
    assert ("ok", 2) in results
    assert ("error", ErrorCode.VERSION_MISMATCH) in results
    assert store.current_version() == 2


def test_decision_recorded_once_per_seq_version(tmp_path, object_factory):
    store = RuleStore(str(tmp_path / "db.sqlite"))
    inserted = [store.record_decision(7, 1, {"request_seq": 7}) for _ in range(5)]
    assert inserted.count(True) == 1
    assert len(store.list_decisions()) == 1
