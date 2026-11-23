from __future__ import annotations
import json
import multiprocessing as mp

import pytest

from tlsdiag.engine import DiagnosticEngine, SampleLayout
from tlsdiag.errors import DiagError
from tlsdiag.storage import Storage, canonical_hash


def _make_engine(sample_dir, db_path):
    layout = SampleLayout.load(sample_dir)
    storage = Storage(db_path)
    return layout, storage, DiagnosticEngine(layout, storage)


def test_apply_is_idempotent_for_identical_patch(sample_dir, db_path):
    _, storage, engine = _make_engine(sample_dir, db_path)
    first = engine.apply_snapshot(1)
    second = engine.apply_snapshot(1)
    assert first["action"] == "committed"
    assert second["action"] == "idempotent_replay"
    assert storage.current_version() == 1
    storage.close()


def test_version_rollback_rejected(sample_dir, db_path):
    _, storage, engine = _make_engine(sample_dir, db_path)
    engine.apply_snapshot(5)
    with pytest.raises(DiagError) as exc:
        engine.apply_snapshot(2)
    assert exc.value.code == "E3001"
    storage.close()


def test_chain_conclusion_unique_per_hostname_purpose_generation(sample_dir, db_path):
    _, storage, engine = _make_engine(sample_dir, db_path)
    engine.apply_snapshot(1)
    engine.verify_all()
    conclusions = storage.chain_conclusions(1)
    keys = [(c["hostname"], c["purpose"], c["generation"]) for c in conclusions]
    assert len(keys) == len(set(keys))
    storage.close()


def test_resume_replays_only_unfinished_job(sample_dir, db_path):
    _, storage, engine = _make_engine(sample_dir, db_path)
    engine.apply_snapshot(1)
    request = {"hostname": "good.example.com", "purpose": "serverAuth", "tick": 6, "generation": 3}
    request_hash = canonical_hash(request)
    storage.start_job("verify:orphan", "verify", 1, request_hash, request)
    unfinished_before = storage.unfinished_jobs("verify")
    assert len(unfinished_before) == 1

    report = engine.resume()
    assert report["remaining_unfinished"] == []
    assert storage.job_result(request_hash) is not None
    storage.close()


def test_crash_mid_verify_leaves_resumable_started_job(sample_dir, db_path):
    # Simulate a forced termination: a job row is 'started' but never completed.
    _, storage, engine = _make_engine(sample_dir, db_path)
    engine.apply_snapshot(1)
    request = {"hostname": "expiring.example.com", "purpose": "serverAuth", "tick": 6, "generation": 3}
    storage.start_job("verify:crashed", "verify", 1, canonical_hash(request), request)
    storage.close()

    _, storage, engine = _make_engine(sample_dir, db_path)
    engine.resume()
    assert storage.unfinished_jobs("verify") == []
    storage.close()


def _worker_apply(sample_dir: str, db_path: str, version: int, queue: mp.Queue) -> None:
    try:
        layout = SampleLayout.load(sample_dir)
        storage = Storage(db_path)
        engine = DiagnosticEngine(layout, storage)
        queue.put(engine.apply_snapshot(version))
        storage.close()
    except Exception as exc:  # pragma: no cover - surfaced via queue
        queue.put({"error": str(exc)})


def test_concurrent_apply_commits_snapshot_once(sample_dir, db_path):
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=_worker_apply, args=(str(sample_dir), str(db_path), 1, queue))
        for _ in range(4)
    ]
    for proc in procs:
        proc.start()
    outcomes = [queue.get() for _ in procs]
    for proc in procs:
        proc.join()
    committed = [o for o in outcomes if o.get("action") == "committed"]
    replays = [o for o in outcomes if o.get("action") == "idempotent_replay"]
    assert len(committed) == 1
    assert len(replays) == 3

    storage = Storage(db_path)
    assert storage.current_version() == 1
    storage.close()


def test_status_json_is_stable_sorted(sample_dir, db_path):
    _, storage, engine = _make_engine(sample_dir, db_path)
    engine.apply_snapshot(1)
    engine.verify_all()
    status = engine.status()
    again = engine.status()
    assert json.dumps(status, sort_keys=True) == json.dumps(again, sort_keys=True)
    keys = [(c["hostname"], c["purpose"], c["generation"]) for c in status["chain_conclusions"]]
    assert keys == sorted(keys)
    storage.close()
