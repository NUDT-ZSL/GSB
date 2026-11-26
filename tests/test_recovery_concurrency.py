import json
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

from tests._util import ROOT, fresh_state_dir, run_cli, run_cli_json, samples_dir


def _decision_digests(state: Path):
    conn = sqlite3.connect(state / "gsb_state.sqlite")
    try:
        return dict(conn.execute(
            "SELECT segment_id, decision_digest FROM segment_decisions").fetchall())
    finally:
        conn.close()


class RecoveryTests(unittest.TestCase):
    def _init_state(self):
        state = fresh_state_dir("recovery")
        run_cli(["load"], state=state)
        run_cli(["merge"], state=state)
        return state

    def test_segment_crash_and_resume_matches_clean_run(self):
        crashed = self._init_state()
        proc = run_cli(["judge", "--crash-after", "2"], state=crashed, expect_ok=False)
        self.assertEqual(proc.returncode, 137)

        conn = sqlite3.connect(crashed / "gsb_state.sqlite")
        decided = [r[0] for r in conn.execute(
            "SELECT segment_id FROM segment_decisions ORDER BY segment_id")]
        pending = [r[0] for r in conn.execute(
            "SELECT item_key FROM commit_journal WHERE state='pending'")]
        conn.close()
        self.assertEqual(decided, [1, 2])
        self.assertEqual(pending, ["seg:1:3"])

        resume = run_cli_json(["resume"], state=crashed)
        self.assertEqual(resume["half_committed"][0]["segment_id"], 3)
        self.assertEqual(resume["replayed_segments"], [3, 4, 5, 6])

        verify = run_cli_json(["verify"], state=crashed)
        self.assertTrue(verify["ok"], verify["checks"])

        clean = self._init_state()
        run_cli(["judge"], state=clean)
        self.assertEqual(_decision_digests(crashed), _decision_digests(clean))

    def test_merge_phase_crash_replayed_by_resume(self):
        state = fresh_state_dir("mcrash")
        run_cli(["load"], state=state)
        proc = run_cli(["merge", "--crash-phase", "merge"], state=state, expect_ok=False)
        self.assertEqual(proc.returncode, 137)
        resume = run_cli_json(["resume"], state=state)
        self.assertEqual(resume["rev_no"], 1)
        self.assertTrue(run_cli_json(["verify"], state=state)["ok"])

    def test_delay_phase_crash_recovered(self):
        state = self._init_state()
        proc = run_cli(["judge", "--crash-phase", "delay"], state=state, expect_ok=False)
        self.assertEqual(proc.returncode, 137)
        self.assertTrue(run_cli_json(["resume"], state=state)["ok"])
        self.assertTrue(run_cli_json(["verify"], state=state)["ok"])

    def test_sigkill_during_slow_judge_recovers(self):
        state = self._init_state()
        cmd = [sys.executable, "-m", "gsb", "--samples", str(samples_dir()),
               "--state", str(state), "judge", "--all", "--slow-ms", "400"]
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=2)  # 通常不会这么快结束
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        self.assertTrue(run_cli_json(["resume"], state=state)["ok"])
        self.assertTrue(run_cli_json(["verify"], state=state)["ok"])


class ConcurrencyTests(unittest.TestCase):
    def test_eight_concurrent_judge_all_exactly_once(self):
        state = fresh_state_dir("mp")
        run_cli(["load"], state=state)
        run_cli(["merge"], state=state)
        procs = []
        for i in range(8):
            cmd = [sys.executable, "-m", "gsb", "--samples", str(samples_dir()),
                   "--state", str(state), "judge", "--all", f"--worker-id=mp{i}"]
            procs.append(subprocess.Popen(cmd, cwd=str(ROOT),
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          text=True))
        results = []
        for p in procs:
            out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err)
            results.append(json.loads(out))

        conn = sqlite3.connect(state / "gsb_state.sqlite")
        rows = conn.execute("SELECT segment_id, COUNT(*) FROM segment_decisions "
                            "GROUP BY segment_id").fetchall()
        self.assertEqual(sorted(sid for sid, _ in rows), [1, 2, 3, 4, 5, 6])
        self.assertTrue(all(c == 1 for _, c in rows))
        claims = conn.execute("SELECT COUNT(*) FROM decision_claims").fetchone()[0]
        conn.close()
        self.assertEqual(claims, 6)

        committed = sum(len(r["committed"]) for r in results)
        idempotent = sum(len(r["idempotent"]) for r in results)
        self.assertEqual(committed, 6)
        self.assertEqual(idempotent, 42)
        self.assertTrue(run_cli_json(["verify"], state=state)["ok"])

    def test_same_segment_hammer_single_commit(self):
        state = fresh_state_dir("hammer")
        run_cli(["load"], state=state)
        run_cli(["merge"], state=state)
        procs = []
        for i in range(6):
            cmd = [sys.executable, "-m", "gsb", "--samples", str(samples_dir()),
                   "--state", str(state), "judge", "--segment", "4",
                   "--slow-ms", "300", f"--worker-id=h{i}"]
            procs.append(subprocess.Popen(cmd, cwd=str(ROOT),
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          text=True))
        committed = idempotent = 0
        for p in procs:
            out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err)
            body = json.loads(out)
            committed += len(body["committed"])
            idempotent += len(body["idempotent"])
        self.assertEqual((committed, idempotent), (1, 5))


if __name__ == "__main__":
    unittest.main()
