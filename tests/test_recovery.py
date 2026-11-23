"""业务要求7: 持久化与强制终止后的恢复; 抑制数不重复计入。"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
KILL_REPRO = os.path.join(ROOT, "scripts", "kill_repro.py")


def test_kill_resume_matches_clean(tmp_path):
    db = str(tmp_path / "kill.db")
    clean = subprocess.run([sys.executable, KILL_REPRO, "clean",
                            "--db", db], capture_output=True, text=True)
    assert clean.returncode == 0, clean.stderr
    victim = subprocess.run([sys.executable, KILL_REPRO, "victim",
                             "--db", db], capture_output=True, text=True)
    assert victim.returncode != 0
    resume = subprocess.run([sys.executable, KILL_REPRO, "resume",
                             "--db", db], capture_output=True, text=True)
    assert resume.returncode == 0, resume.stderr
    assert "results-v1-e20" in resume.stdout
    clean_sha = [l for l in clean.stdout.splitlines() if "sha256" in l][0]
    resume_sha = [l for l in resume.stdout.splitlines()
                  if "sha256" in l][0]
    assert clean_sha.split()[-1] == resume_sha.split()[-1]


def test_reingest_idempotent_no_double_suppression(engine, topo_dict,
                                                   events_list):
    engine.load_topology(topo_dict)
    engine.ingest(events_list)
    engine.run()
    first = engine.dump()
    engine.ingest(events_list)
    engine.ingest(events_list[:10])
    engine.run()
    second = engine.dump()
    assert first["sha"] == second["sha"]
    total_suppressed = sum(c["suppressed_count"]
                           for c in second["clusters"].values())
    assert total_suppressed == 2


def test_recover_reports_half_committed(engine, topo_dict, events_list):
    engine.load_topology(topo_dict)
    engine.ingest(events_list)
    topo = engine._topology()
    stored = engine.store.load_events()
    commit_id = "results-v%d-e%d" % (topo.version, len(stored))
    engine.store.begin_commit(commit_id, "results")
    clusters, candidates, decisions = engine._compute_all(topo, stored)
    engine.store.stage_clusters(clusters)
    engine.store.stage_candidates(candidates)
    engine.store.stage_decisions(decisions)
    pending = engine.recover()
    assert [p["commit_id"] for p in pending] == [commit_id]
    assert engine.store.pending_commits() == []
    result = engine.run()
    assert result["clusters"]
