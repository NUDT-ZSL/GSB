from __future__ import annotations

import json
import multiprocessing
import subprocess
import sys
from pathlib import Path

import pytest

from slo_workbench.engine import Engine
from slo_workbench.errors import BusinessError
from slo_workbench.loader import load_sample_bundle, read_bundle
from slo_workbench.query import QueryService
from slo_workbench.repository import Repository, payload_digest
from slo_workbench.storage import Workspace, dumps_canonical, transaction


def _run_all(workspace: Workspace, sample_dir: Path):
    _, _, clock = read_bundle(sample_dir)
    return Engine(workspace).run(clock)


def test_end_to_end_window_commit_and_late_isolation(loaded_workspace, sample_dir):
    summary = _run_all(loaded_workspace, sample_dir)
    assert summary["late_events"] == {"checkout-availability": 1, "search-latency": 1}

    conn = loaded_workspace.connect()
    try:
        with transaction(conn):
            repo = Repository(conn)
            slo = repo.get_slo("checkout-availability")
            windows = repo.list_windows("checkout-availability", slo.version)
            # 11 windows 0..10, window 7 marked empty.
            assert [w["window_no"] for w in windows] == list(range(11))
            empty = [w for w in windows if w.get("status") == "empty"]
            assert [w["window_no"] for w in empty] == [7]

            late = repo.list_late_events("checkout-availability", slo.version)
            assert late and late[0]["window_no"] == 1
            # Late event must not have mutated the committed window 1.
            window_one = repo.get_window_payload(
                "checkout-availability", slo.version, 1
            )
            before_digest = payload_digest(window_one)
    finally:
        conn.close()

    # Re-running over the same ticks changes nothing (idempotent replay).
    _run_all(loaded_workspace, sample_dir)
    conn = loaded_workspace.connect()
    try:
        with transaction(conn):
            repo = Repository(conn)
            slo = repo.get_slo("checkout-availability")
            again = repo.get_window_payload("checkout-availability", slo.version, 1)
            assert payload_digest(again) == before_digest
    finally:
        conn.close()


def test_clock_rollback_beyond_tolerance_is_isolated(loaded_workspace, sample_dir):
    summary = _run_all(loaded_workspace, sample_dir)
    codes = {error["code"] for error in summary["errors"]}
    assert "E_CLOCK_ROLLBACK" in codes
    # Other SLO still advanced despite the rollback diagnostic.
    conn = loaded_workspace.connect()
    try:
        with transaction(conn):
            repo = Repository(conn)
            assert repo.list_windows("search-latency", 1)
    finally:
        conn.close()


def test_cache_hit_and_verify_match(loaded_workspace, sample_dir):
    _run_all(loaded_workspace, sample_dir)
    conn = loaded_workspace.connect()
    try:
        with transaction(conn):
            service = QueryService(Repository(conn))
            first = service.query_window("checkout-availability", 3)
            second = service.query_window("checkout-availability", 3)
            assert first["report"] == second["report"]
            verified = service.query_window(
                "checkout-availability", 3, verify=True
            )
            assert verified["verification"]["status"] == "match"
            burn = service.query_burn("checkout-availability", verify=True)
            assert burn["verification"]["status"] == "match"
    finally:
        conn.close()


def test_cache_mismatch_detects_first_diff_field(loaded_workspace, sample_dir):
    _run_all(loaded_workspace, sample_dir)
    conn = loaded_workspace.connect()
    try:
        with transaction(conn):
            repo = Repository(conn)
            service = QueryService(repo)
            good = service.query_window("checkout-availability", 3)
            tampered = json.loads(json.dumps(good["report"]))
            tampered["budget"]["consumed"]["value"] = "999"
            watermark = repo.get_watermark("checkout-availability")
            from slo_workbench.query import _cache_key

            key = _cache_key(
                "window",
                "checkout-availability",
                1,
                watermark["committed_seq"],
                3,
            )
            repo.cache_put(
                key,
                "checkout-availability",
                1,
                3,
                watermark["committed_seq"],
                tampered,
                watermark["committed_clock"],
            )
            with pytest.raises(BusinessError) as exc:
                service.query_window("checkout-availability", 3, verify=True)
            assert exc.value.code == "E_CACHE_MISMATCH"
            assert (
                exc.value.fields["first_diff_field"]
                == "budget.consumed.value"
            )
    finally:
        conn.close()


def test_recovery_after_simulated_crash_only_fills_missing(
    loaded_workspace, sample_dir
):
    _, _, clock = read_bundle(sample_dir)
    crash_index = 4
    worker = Path(__file__).with_name("_crash_worker.py")
    killed = subprocess.run(
        [
            sys.executable,
            str(worker),
            str(loaded_workspace.root),
            str(sample_dir),
            str(crash_index),
        ],
        capture_output=True,
        text=True,
    )
    assert killed.returncode == 137

    conn = loaded_workspace.connect()
    try:
        before = conn.execute("SELECT COUNT(*) FROM window").fetchone()[0]
    finally:
        conn.close()

    engine = Engine(loaded_workspace)
    report = engine.resume(clock)
    assert report["recovered"]

    # Final state must equal a clean full run.
    clean_ws = Workspace(loaded_workspace.root.parent / "clean")
    clean_ws.bootstrap()
    load_sample_bundle(clean_ws, sample_dir)
    Engine(clean_ws).run(clock)

    def window_digests(ws: Workspace):
        connection = ws.connect()
        try:
            rows = connection.execute(
                "SELECT slo_name, window_no, payload_digest FROM window "
                "ORDER BY slo_name, window_no"
            ).fetchall()
            return [(r["slo_name"], r["window_no"], r["payload_digest"]) for r in rows]
        finally:
            connection.close()

    assert window_digests(loaded_workspace) == window_digests(clean_ws)

    conn = loaded_workspace.connect()
    try:
        after = conn.execute("SELECT COUNT(*) FROM window").fetchone()[0]
        assert after >= before
        # No duplicated window rows.
        duplicates = conn.execute(
            "SELECT slo_name, version, window_no, COUNT(*) c FROM window "
            "GROUP BY slo_name, version, window_no HAVING c > 1"
        ).fetchall()
        assert not duplicates
    finally:
        conn.close()


def _worker(args):
    workspace_path, slo_name, lo, hi = args
    ws = Workspace(workspace_path)
    Engine(ws).process_window_range(slo_name, lo, hi)


def test_concurrent_window_range_is_idempotent(tmp_path, sample_dir):
    workspace = Workspace(tmp_path / "parallel")
    workspace.bootstrap()
    load_sample_bundle(workspace, sample_dir)
    # Ingest events first so accepted_event rows exist.
    _run_all(workspace, sample_dir)

    jobs = [
        (str(workspace.root), "checkout-availability", 0, 5),
        (str(workspace.root), "checkout-availability", 3, 8),
        (str(workspace.root), "checkout-availability", 6, 10),
    ]
    with multiprocessing.Pool(3) as pool:
        pool.map(_worker, jobs)

    conn = workspace.connect()
    try:
        rows = conn.execute(
            "SELECT window_no, COUNT(*) c FROM window "
            "WHERE slo_name='checkout-availability' GROUP BY window_no ORDER BY window_no"
        ).fetchall()
        assert [r["window_no"] for r in rows] == list(range(11))
        assert all(r["c"] == 1 for r in rows)
    finally:
        conn.close()


def test_version_bump_invalidates_old_cache(loaded_workspace, sample_dir):
    _run_all(loaded_workspace, sample_dir)
    conn = loaded_workspace.connect()
    try:
        with transaction(conn):
            repo = Repository(conn)
            QueryService(repo).query_window("checkout-availability", 3)
            assert conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0] > 0
    finally:
        conn.close()

    # Reload a (possibly identical) bundle -> version bump, cache pruned.
    load_sample_bundle(loaded_workspace, sample_dir)
    conn = loaded_workspace.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0
        assert conn.execute("SELECT DISTINCT version FROM slo_def").fetchall()
    finally:
        conn.close()


def test_status_and_verify_emit_stable_json(loaded_workspace, sample_dir):
    _run_all(loaded_workspace, sample_dir)
    conn = loaded_workspace.connect()
    try:
        with transaction(conn):
            service = QueryService(Repository(conn))
            status_a = dumps_canonical(service.status())
            verify_a = dumps_canonical(service.verify_all())
    finally:
        conn.close()
    conn = loaded_workspace.connect()
    try:
        with transaction(conn):
            service = QueryService(Repository(conn))
            assert dumps_canonical(service.status()) == status_a
            assert dumps_canonical(service.verify_all()) == verify_a
    finally:
        conn.close()
