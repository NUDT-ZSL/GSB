import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def cli(args, cwd=ROOT):
    return subprocess.run(
        [sys.executable, "-m", "pipeline_tool", *args],
        cwd=cwd, capture_output=True, text=True, timeout=120,
    )


def test_forced_kill_then_resume(tmp_path):
    db = tmp_path / "ledger.db"
    out = tmp_path / "results.json"
    common = ["--pipeline", str(DATA / "pipeline_crash.json"),
              "--input", str(DATA / "input_records.json"),
              "--db", str(db), "--out", str(out)]

    # 1. Run and force a hard kill right after fast_sum commits mid-stage.
    crashed = cli(["run", *common, "--crash-after-node", "fast_sum"])
    assert crashed.returncode == 3  # os._exit(3): simulated SIGKILL

    # Ledger shows: fast_sum committed, slow_sum half-committed (RUNNING).
    conn = sqlite3.connect(db)
    states = dict(conn.execute("SELECT name, state FROM nodes").fetchall())
    conn.close()
    assert states["filter_high"] == "COMMITTED"
    assert states["fast_sum"] == "COMMITTED"
    assert states["slow_sum"] == "RUNNING"

    # 2. Resume: only the unfinished node is recomputed.
    resumed = cli(["resume", *common])
    assert resumed.returncode == 0, resumed.stderr
    report = json.loads(resumed.stdout)
    assert report["summary"]["requeued_after_crash"] == ["slow_sum"]

    conn = sqlite3.connect(db)
    rows = dict(conn.execute("SELECT name, state FROM nodes").fetchall())
    attempts = dict(conn.execute("SELECT name, attempt FROM nodes").fetchall())
    conn.close()
    assert all(s == "COMMITTED" for s in rows.values())
    assert attempts["fast_sum"] == 1  # committed node NOT re-executed
    assert attempts["slow_sum"] == 2  # half-committed node replayed exactly once

    # 3. Recovered results must equal an uninterrupted run, byte for byte.
    db2 = tmp_path / "clean.db"
    out2 = tmp_path / "clean.json"
    clean = cli(["run", "--pipeline", str(DATA / "pipeline_crash.json"),
                 "--input", str(DATA / "input_records.json"),
                 "--db", str(db2), "--out", str(out2)])
    assert clean.returncode == 0
    assert out.read_bytes() == out2.read_bytes()

    # 4. Verifier agrees ledger and results describe the same committed set.
    verified = cli(["verify", *common])
    assert verified.returncode == 0
    assert json.loads(verified.stdout)["ok"] is True
