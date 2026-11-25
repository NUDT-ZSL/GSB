from __future__ import annotations

import sqlite3
import unittest

from topodiag.engine import build_workspace
from topodiag.sample import generate_samples
from topodiag.storage import runtime_db_path

from .helpers import make_workdir, run_cli, status, verify


class RecoveryTests(unittest.TestCase):
    def _prepare(self, prefix: str):
        workdir = make_workdir(prefix)
        generate_samples(workdir)
        build_workspace(workdir)
        return workdir

    def test_resource_transaction_crash_recovery_matches_uninterrupted(self) -> None:
        baseline = self._prepare("topodiag-baseline-")
        crashed = self._prepare("topodiag-crash-")
        run_cli(
            "schedule",
            "--workdir",
            str(baseline),
            "--round-budget-ms",
            "250",
        )
        expected = status(baseline)
        crash = run_cli(
            "schedule",
            "--workdir",
            str(crashed),
            "--round-budget-ms",
            "250",
            "--crash-at",
            "resource",
            check=False,
        )
        self.assertEqual(crash.returncode, 79)
        recovered = run_cli(
            "recover",
            "--workdir",
            str(crashed),
            "--round-budget-ms",
            "250",
        )
        self.assertEqual(recovered.returncode, 0)
        actual = status(crashed)
        self.assertEqual(expected["semantic_digest"], actual["semantic_digest"])
        self.assertEqual(expected["request_outcomes"], actual["request_outcomes"])
        self.assertEqual(expected["resources"], actual["resources"])
        self.assertTrue(verify(crashed)["ok"])

    def test_policy_activation_crash_recovery_matches_uninterrupted(self) -> None:
        baseline = self._prepare("topodiag-policy-baseline-")
        crashed = self._prepare("topodiag-policy-crash-")
        run_cli(
            "build",
            "--workdir",
            str(baseline),
            "--policy-package",
            str(baseline / "samples" / "policies" / "v2-update"),
        )
        run_cli(
            "schedule",
            "--workdir",
            str(baseline),
            "--round-budget-ms",
            "250",
        )
        expected = status(baseline)
        crash = run_cli(
            "build",
            "--workdir",
            str(crashed),
            "--policy-package",
            str(crashed / "samples" / "policies" / "v2-update"),
            "--crash-at",
            "policy-stage",
            check=False,
        )
        self.assertEqual(crash.returncode, 78)
        run_cli(
            "recover",
            "--workdir",
            str(crashed),
            "--round-budget-ms",
            "250",
        )
        actual = status(crashed)
        self.assertEqual(expected["semantic_digest"], actual["semantic_digest"])
        self.assertEqual(expected["invalidations"], actual["invalidations"])
        self.assertEqual(expected["request_outcomes"], actual["request_outcomes"])
        self.assertTrue(verify(crashed)["ok"])

    def test_recover_repairs_half_committed_ledger(self) -> None:
        workdir = self._prepare("topodiag-ledger-repair-")
        run_cli(
            "schedule",
            "--workdir",
            str(workdir),
            "--round-budget-ms",
            "250",
        )
        expected = status(workdir)
        db = runtime_db_path(workdir)
        with sqlite3.connect(str(db)) as connection:
            connection.execute(
                "UPDATE resource_ledger SET used=999999 WHERE node_id='node-a'"
            )
            connection.commit()
        recovered = run_cli(
            "recover",
            "--workdir",
            str(workdir),
            "--round-budget-ms",
            "250",
        )
        self.assertEqual(recovered.returncode, 0)
        actual = status(workdir)
        self.assertEqual(expected["semantic_digest"], actual["semantic_digest"])
        self.assertEqual(expected["resources"], actual["resources"])
        self.assertTrue(verify(workdir)["ok"])

    def test_recover_rebuilds_interrupted_initial_policy_activation(self) -> None:
        workdir = make_workdir("topodiag-initial-build-crash-")
        generate_samples(workdir)
        crash = run_cli(
            "build",
            "--workdir",
            str(workdir),
            "--crash-at",
            "policy-stage",
            check=False,
        )
        self.assertEqual(crash.returncode, 78)
        recovered = run_cli(
            "recover",
            "--workdir",
            str(workdir),
            "--round-budget-ms",
            "250",
        )
        self.assertEqual(recovered.returncode, 0)
        payload = status(workdir)
        self.assertEqual(payload["policy_pointer"]["policy_generation"], 2)
        self.assertEqual(len(payload["request_outcomes"]), 18)
        self.assertTrue(verify(workdir)["ok"])


if __name__ == "__main__":
    unittest.main()
