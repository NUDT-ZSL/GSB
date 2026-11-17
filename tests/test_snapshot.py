import json
import os
import tempfile

from ocr_workbench.config import ConfigBundle
from ocr_workbench.snapshot import SnapshotStore

CFG = ConfigBundle.load("configs/detection.json", "configs/recognition.json")


class _Result:
    def __init__(self, name, fingerprint):
        self.name = name
        self.fingerprint = fingerprint
        self.reused_stages = ["detection"]
        self.recomputed_stages = ["recognition"]
        self.lines = [{"bbox": [1, 2, 3, 4], "text": "AB",
                       "chars": [{"x": 1}, {"x": 2}]}]


def test_commit_is_atomic_and_replaces_whole_snapshot():
    workdir = tempfile.mkdtemp()
    store = SnapshotStore(workdir)
    results = [_Result("a", "fp1")]
    store.commit(1, CFG, ["a"], results, {"a": {"detection": "k"}})
    first = store.load()
    assert first["global_version"] == 1

    store.commit(2, CFG, ["a"], results, {"a": {"detection": "k2"}})
    second = store.load()
    assert second["global_version"] == 2
    assert second["images"][0]["cache_keys"]["detection"] == "k2"
    # No leftover temp files; only the committed names remain.
    names = set(os.listdir(workdir))
    assert not any(name.startswith(".tmp-") for name in names)


def test_corrupt_snapshot_raises_business_code():
    from ocr_workbench.errors import E_SNAPSHOT_CORRUPT, WorkbenchError
    workdir = tempfile.mkdtemp()
    store = SnapshotStore(workdir)
    with open(store.snapshot_path, "w") as handle:
        handle.write("{not valid json")
    try:
        store.load()
        assert False, "expected WorkbenchError"
    except WorkbenchError as exc:
        assert exc.code == E_SNAPSHOT_CORRUPT


def test_partial_write_keeps_previous_snapshot():
    workdir = tempfile.mkdtemp()
    store = SnapshotStore(workdir)
    results = [_Result("a", "fp1")]
    store.commit(1, CFG, ["a"], results, {"a": {"detection": "k"}})
    # Simulate a crash during write: temp file left, committed file intact.
    tmp = os.path.join(workdir, ".tmp-crash.json")
    with open(tmp, "w") as handle:
        handle.write("partial")
    loaded = store.load()
    assert loaded["global_version"] == 1


def test_recovery_reports_recovered_version():
    from ocr_workbench.engine import Engine
    from ocr_workbench.loader import load_directory
    workdir = tempfile.mkdtemp()
    records, failures = load_directory(
        "samples", CFG.detection["image_size"])
    engine = Engine(records, failures, CFG, workdir)
    engine.wait_idle()
    engine.request_exit()
    engine.wait_exit()

    restarted = Engine(records, failures, CFG, workdir)
    snapshot = restarted.recover_from_snapshot()
    assert snapshot is not None
    assert restarted.recovered_version == snapshot["global_version"]
    restarted.request_exit()
    restarted.wait_exit()
