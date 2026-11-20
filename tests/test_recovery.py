"""Requirements 6/7: crash recovery consistency, idempotency and
multi-process concurrency."""
import json
import os
import sqlite3
import subprocess
import sys

from gateway.engine import Engine
from gateway.store import Store


def _cli_run(samples_dir, db, out, extra=None):
    cmd = [sys.executable, "-m", "gateway.cli", "run",
           "--samples", samples_dir, "--db", str(db),
           "--out", str(out)]
    if extra:
        cmd += extra
    return subprocess.run(cmd, capture_output=True, text=True)


def test_crash_mid_round_then_recovers_identically(samples_dir, tmp_path):
    clean_db = tmp_path / "clean.db"
    clean_out = tmp_path / "clean.json"
    assert _cli_run(samples_dir, clean_db, clean_out).returncode == 0

    crash_db = tmp_path / "crash.db"
    crash_out = tmp_path / "crash.json"
    proc = _cli_run(samples_dir, crash_db, crash_out,
                    extra=["--crash-after", "5"])
    assert proc.returncode == 3  # hard kill, no exit report promised
    assert not os.path.exists(crash_out)

    recovered_out = tmp_path / "recovered.json"
    proc = _cli_run(samples_dir, crash_db, recovered_out)
    assert proc.returncode == 0
    with open(clean_out, "rb") as fh:
        clean = fh.read()
    with open(recovered_out, "rb") as fh:
        recovered = fh.read()
    assert clean == recovered  # bit-identical decision chain after replay


def test_crash_does_not_double_charge_budgets(samples_dir, tmp_path):
    crash_db = tmp_path / "crash.db"
    proc = _cli_run(samples_dir, crash_db, tmp_path / "x.json",
                    extra=["--crash-after", "3"])
    assert proc.returncode == 3
    store = Store(str(crash_db))
    try:
        # the interrupted round left no committed residue
        assert store.current_version() == -1
        assert store.committed_rounds() == []
    finally:
        store.close()
    proc = _cli_run(samples_dir, crash_db, tmp_path / "y.json")
    assert proc.returncode == 0
    store = Store(str(crash_db))
    try:
        rounds = store.committed_rounds()
        for rnd in rounds:
            for d in rnd["decisions"]:
                consumed = rnd["budgets"][d["service"]]
                assert consumed <= d["token_budget"]
    finally:
        store.close()


def test_idempotent_recommit_same_version(samples_dir, tmp_path):
    db = tmp_path / "idem.db"
    out1 = tmp_path / "1.json"
    out2 = tmp_path / "2.json"
    assert _cli_run(samples_dir, db, out1).returncode == 0
    assert _cli_run(samples_dir, db, out2).returncode == 0
    assert open(out1, "rb").read() == open(out2, "rb").read()
    # each (version, request_id) pair exists exactly once
    conn = sqlite3.connect(str(db))
    try:
        dup = conn.execute(
            "SELECT version, request_id, COUNT(*) c FROM requests"
            " GROUP BY version, request_id HAVING c > 1").fetchall()
        assert dup == []
    finally:
        conn.close()


def test_multi_process_same_database(samples_dir, tmp_path):
    db = tmp_path / "mp.db"
    outs = [tmp_path / f"mp{i}.json" for i in range(2)]
    procs = [subprocess.Popen(
        [sys.executable, "-m", "gateway.cli", "run",
         "--samples", samples_dir, "--db", str(db), "--out", str(o)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE) for o in outs]
    for p in procs:
        assert p.wait() == 0
    payloads = [open(o, "rb").read() for o in outs]
    assert payloads[0] == payloads[1]
    # no oversell: per round, per card VRAM and per-node instances within
    # the committed capacity
    report = json.loads(payloads[0])
    for rnd in report["rounds"]:
        for snap in rnd["node_states"]:
            assert all(r >= 0 for r in snap["cards_remaining_mib"])
            assert snap["instances_used"] <= snap["max_instances"]
    # every round committed exactly once
    conn = sqlite3.connect(str(db))
    try:
        versions = [r[0] for r in conn.execute(
            "SELECT version FROM rounds WHERE committed = 1"
            " ORDER BY version")]
        assert versions == sorted(set(versions))
    finally:
        conn.close()


def test_readers_only_see_committed_rounds(loaded, tmp_path):
    services, nodes, trace = loaded
    store = Store(str(tmp_path / "r.db"))
    try:
        engine = Engine(services, nodes, trace, store)
        engine.run()
        rounds = store.committed_rounds()
        assert len(rounds) == len(trace.windows)
        assert store.current_version() == len(trace.windows) - 1
    finally:
        store.close()
