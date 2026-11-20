import os
import sqlite3
import subprocess
import sys

from graymesh.clock import LogicalClock
from graymesh.controlplane import ControlPlane
from graymesh.store import RuleStore

from tests.conftest import bootstrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_objects(f):
    return [
        f["member"]("m1", 9001, 1),
        f["group"]("g1", ["m1"]),
        f["route"]("r1", "g1", gray_percent=100),
        f["virtual_service"]("vs1", ["r1"]),
    ]


def run_crash_child(mode, db_path):
    env = dict(os.environ, PYTHONPATH=ROOT)
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "crash_child.py"), mode, db_path],
        env=env, capture_output=True, timeout=30).returncode


def test_torn_commit_removed_and_version_not_rolled_back(tmp_path, object_factory):
    db = str(tmp_path / "db.sqlite")
    store = RuleStore(db)
    control = ControlPlane(store, LogicalClock())
    bootstrap(control, make_objects(object_factory))
    control.submit_patch({"patch_id": "p2", "expected_version": 1, "ops": [
        {"op": "replace", "kind": "upstream_member", "id": "m1",
         "object": object_factory["member"]("m1", 9001, 7)}]})
    assert store.current_version() == 2

    rc = run_crash_child("torn-commit", db)
    assert rc != 0

    recovered = RuleStore(db)
    assert recovered.current_version() == 2  # no rollback
    with sqlite3.connect(db) as conn:
        versions = [r[0] for r in conn.execute("SELECT version FROM versions")]
        orphans = conn.execute(
            "SELECT COUNT(*) FROM objects WHERE version NOT IN "
            "(SELECT version FROM versions)").fetchone()[0]
    assert versions == [1, 2]  # torn v3 gone
    assert orphans == 0
    # recovery is idempotent
    report = recovered.recover()
    assert report["torn_versions_removed"] == []


def test_interrupted_mirror_marked_not_recounted(tmp_path, object_factory):
    db = str(tmp_path / "db.sqlite")
    store = RuleStore(db)
    control = ControlPlane(store, LogicalClock())
    bootstrap(control, make_objects(object_factory))

    rc = run_crash_child("mid-mirror", db)
    assert rc != 0

    recovered = RuleStore(db)
    mirrors = recovered.list_mirrors()
    assert len(mirrors) == 1
    assert mirrors[0]["state"] == "interrupted"
    # a late finish attempt must not rewrite the interrupted record
    assert recovered.mirror_finish(mirrors[0]["mirror_seq"], "completed", {}) is False
    assert recovered.list_mirrors()[0]["state"] == "interrupted"


def test_uncommitted_transaction_invisible(tmp_path, object_factory):
    """A crash before COMMIT leaves nothing behind at all."""
    db = str(tmp_path / "db.sqlite")
    store = RuleStore(db)
    control = ControlPlane(store, LogicalClock())
    bootstrap(control, make_objects(object_factory))

    conn = sqlite3.connect(db)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO versions(version, patch_id, committed, created_ms) "
                 "VALUES (2, 'never-committed', 0, 0)")
    conn.close()  # close without commit -> rollback

    recovered = RuleStore(db)
    assert recovered.current_version() == 1
    with sqlite3.connect(db) as check:
        rows = check.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
    assert rows == 1
