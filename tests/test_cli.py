from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp.name) / "ws"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "meeting_retrieval", "--workspace", str(self.ws), *args],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_demo_json_and_selfcheck(self) -> None:
        demo = self._run("demo", "--json")
        self.assertEqual(demo.returncode, 0, demo.stderr)
        payload = json.loads(demo.stdout)
        self.assertEqual(len(payload["ranking"]["hits"]), 5)
        self.assertEqual(payload["stages"]["retrieval_ranking"]["version"], 1)
        check = self._run("selfcheck", "--json")
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertTrue(json.loads(check.stdout)["ok"])

    def test_crash_exit_code_and_restart_resume(self) -> None:
        self.assertEqual(self._run("demo").returncode, 0)
        crashed = self._run("run", "--crash-after", "text_embeddings")
        self.assertEqual(crashed.returncode, 75)
        self.assertEqual(json.loads(crashed.stderr)["error"]["code"], "E_FORCED_TERMINATION")
        recovered = self._run("recover", "--json")
        self.assertEqual(recovered.returncode, 0)
        recovery = json.loads(recovered.stdout)
        self.assertEqual(recovery["last_committed_stage"], "text_embeddings")
        self.assertEqual(recovery["resume_from_stage"], "alignment_scores")
        resumed = self._run("run", "--json")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        output = json.loads(resumed.stdout)
        self.assertTrue(output["recovery"]["complete"])

    def test_invalid_vocab_term_returns_structured_cli_error(self) -> None:
        self.assertEqual(self._run("demo").returncode, 0)
        failed = self._run("update-text", "p01", "完全不在词表中的词")
        self.assertEqual(failed.returncode, 2)
        error = json.loads(failed.stderr)["error"]
        self.assertEqual(error["code"], "E_VOCAB_MISSING")
        self.assertEqual(error["pair_id"], "p01")
        self.assertEqual(error["field"], "text")


if __name__ == "__main__":
    unittest.main()
