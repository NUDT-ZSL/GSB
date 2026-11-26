"""Multi-process merge consistency (req 8): several merge instances run
concurrently against the same workdir; each batch must be committed
exactly once and each vote counted exactly once."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from coord_workbench.engine import Samples
from coord_workbench.persistence import Ledger


def test_concurrent_processes_single_commit(sample_dir: Path, tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parent.parent
    workdir = tmp_path / "shared"
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "coord_workbench.cli", "merge",
             "--samples", str(sample_dir), "--workdir", str(workdir)],
            cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(4)
    ]
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err

    ledger = Ledger(workdir)
    conn = sqlite3.connect(str(ledger.db_path))
    try:
        committed = conn.execute(
            "SELECT COUNT(*) FROM batch_journal WHERE status='COMMITTED'"
        ).fetchone()[0]
        started = conn.execute("SELECT COUNT(*) FROM batch_journal").fetchone()[0]
        votes = conn.execute("SELECT COUNT(*) FROM votes").fetchone()[0]
    finally:
        conn.close()

    samples = Samples(sample_dir)
    assert committed == 8  # every batch committed exactly once
    assert started == 8  # no duplicate journal rows across processes
    assert votes == len(samples.events)  # no vote counted twice

    # per-domain applied seqs are disjoint and cover the whole stream
    applied = set()
    for st in ledger.list_domain_states():
        seqs = set(json.loads(st["applied_seqs"]))
        assert applied.isdisjoint(seqs)
        applied |= seqs
    assert applied == {e.seq for e in samples.events}
