from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from spkvc.config import PipelineConfig
from spkvc.errors import BusinessError
from spkvc.models import canonical_bytes
from spkvc.snapshots import NonBlockingLock, SnapshotStore

from .support import DatasetTestCase


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _payload(stage_index: int, utterance_ids: list[str]) -> dict:
    return {
        "stage": ["features", "timbre", "alignment", "params", "synthesis"][stage_index],
        "stage_index": stage_index,
        "completed_utterance_ids": utterance_ids,
        "artifacts": {},
        "node_records": {},
    }


class SnapshotSafetyTests(unittest.TestCase):
    def test_rollback_checksum_and_corruption_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "run")
            store.commit_stage(
                stage_index=0,
                completed_utterance_ids=["utt-1"],
                payload=_payload(0, ["utt-1"]),
                signature="sig",
                proposed_version=1,
            )
            with self.assertRaises(BusinessError) as caught:
                store.commit_stage(
                    stage_index=1,
                    completed_utterance_ids=["utt-1"],
                    payload=_payload(1, ["utt-1"]),
                    signature="sig",
                    proposed_version=1,
                )
            self.assertEqual(caught.exception.code, "E_SNAPSHOT_ROLLBACK")
            with self.assertRaises(BusinessError) as caught:
                store.commit_stage(
                    stage_index=1,
                    completed_utterance_ids=["utt-1"],
                    payload=_payload(1, ["utt-1"]),
                    signature="sig",
                    proposed_version=2,
                    expected_content_checksum="0" * 64,
                )
            self.assertEqual(caught.exception.code, "E_SNAPSHOT_CORRUPT")
            self.assertEqual(len(store.load().entries), 1)
            entry = store.commit_stage(
                stage_index=1,
                completed_utterance_ids=["utt-1"],
                payload=_payload(1, ["utt-1"]),
                signature="sig",
                proposed_version=2,
            )
            self.assertEqual(entry.version, 2)
            payload_path = store.payload_path(entry)
            payload_path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(BusinessError) as caught:
                store.load()
            self.assertEqual(caught.exception.code, "E_SNAPSHOT_CORRUPT")

    def test_bundle_checksum_corruption_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "run")
            store.commit_stage(
                stage_index=0,
                completed_utterance_ids=["utt-1"],
                payload=_payload(0, ["utt-1"]),
                signature="sig",
            )
            bundle = json.loads(store.snapshot_path.read_text(encoding="utf-8"))
            bundle["version_counter"] = 99
            store.snapshot_path.write_bytes(canonical_bytes(bundle) + b"\n")
            with self.assertRaises(BusinessError) as caught:
                store.load()
            self.assertEqual(caught.exception.code, "E_SNAPSHOT_CORRUPT")

    def test_non_blocking_lock_returns_stage_busy_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            first = NonBlockingLock(path)
            with first:
                started = time.monotonic()
                with self.assertRaises(BusinessError) as caught:
                    with NonBlockingLock(path):
                        pass
                elapsed = time.monotonic() - started
            self.assertEqual(caught.exception.code, "E_STAGE_BUSY")
            self.assertLess(elapsed, 0.25)

    def test_non_blocking_lock_conflicts_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            code = (
                "import sys,time\n"
                "from spkvc.snapshots import NonBlockingLock\n"
                "lock=NonBlockingLock(sys.argv[1])\n"
                "lock.__enter__()\n"
                "print('locked', flush=True)\n"
                "time.sleep(10)\n"
            )
            holder = subprocess.Popen(
                [sys.executable, "-c", code, str(path)],
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(holder.stdout.readline().strip(), "locked")
                with self.assertRaises(BusinessError) as caught:
                    with NonBlockingLock(path):
                        pass
                self.assertEqual(caught.exception.code, "E_STAGE_BUSY")
            finally:
                holder.terminate()
                holder.communicate(timeout=5)

    def test_whole_snapshot_replacement_and_replacement_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SnapshotStore(Path(tmp) / "run")
            stages = [
                (index, ["utt-1"], _payload(index, ["utt-1"]))
                for index in range(5)
            ]
            entries = store.replace_all(
                stages=stages,
                signature="sig",
                proposed_versions=[10, 11, 12, 13, 14],
            )
            self.assertEqual(
                [entry.version for entry in entries], [10, 11, 12, 13, 14]
            )
            self.assertEqual(store.load().version_counter, 14)
            with self.assertRaises(BusinessError) as caught:
                store.replace_all(
                    stages=stages,
                    signature="sig",
                    proposed_versions=[15, 16, 17, 18, 18],
                )
            self.assertEqual(caught.exception.code, "E_SNAPSHOT_ROLLBACK")
            self.assertEqual(store.load().version_counter, 14)


class RecoveryTests(DatasetTestCase):
    def _cli(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "spkvc", *args],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    def _run(self, state: str) -> dict:
        completed = self._cli(
            "run",
            "--data",
            str(self.dataset_root),
            "--state-dir",
            str(self.tmp / state),
            "--out",
            str(self.tmp / state / "outputs"),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        return json.loads(completed.stdout)

    def test_forced_termination_after_committed_stage_resumes_next_stage(self):
        baseline = self._run("recovery-baseline")
        state = self.tmp / "recovery-stage"
        crashed = self._cli(
            "run",
            "--data",
            str(self.dataset_root),
            "--state-dir",
            str(state),
            "--out",
            str(state / "outputs"),
            "--crash-after-stage",
            "1",
        )
        self.assertEqual(crashed.returncode, 86)
        snapshots = json.loads(
            self._cli(
                "snapshot",
                "--data",
                str(self.dataset_root),
                "--state-dir",
                str(state),
            ).stdout
        )
        self.assertEqual(
            [entry["stage"] for entry in snapshots["entries"]],
            ["features", "timbre"],
        )
        resumed = self._run("recovery-stage")
        self.assertEqual(resumed["resumed_from_stage"], 2)
        self.assertEqual(
            resumed["executed_stages"], ["alignment", "params", "synthesis"]
        )
        self.assertFalse(
            any(node.startswith("features:") for node in resumed["recomputed_nodes"])
        )
        self.assertEqual(baseline["pcm_sha256"], resumed["pcm_sha256"])

    def test_forced_termination_mid_stage_discards_uncommitted_work(self):
        baseline = self._run("mid-baseline")
        state = self.tmp / "recovery-mid"
        crashed = self._cli(
            "run",
            "--data",
            str(self.dataset_root),
            "--state-dir",
            str(state),
            "--out",
            str(state / "outputs"),
            "--crash-mid-stage",
            "2",
            "--crash-after-utterances",
            "3",
        )
        self.assertEqual(crashed.returncode, 87)
        snapshots = json.loads(
            self._cli(
                "snapshot",
                "--data",
                str(self.dataset_root),
                "--state-dir",
                str(state),
            ).stdout
        )
        self.assertEqual(
            [entry["stage"] for entry in snapshots["entries"]],
            ["features", "timbre"],
        )
        resumed = self._run("recovery-mid")
        self.assertEqual(resumed["resumed_from_stage"], 2)
        self.assertEqual(
            resumed["executed_stages"], ["alignment", "params", "synthesis"]
        )
        self.assertEqual(baseline["pcm_sha256"], resumed["pcm_sha256"])

    def test_incremental_is_atomic_under_forced_termination(self):
        engine = self.engine("increment-ok")
        engine.run()
        value = engine.config.to_dict()
        value["mapping_version"] = "map-v2"
        value["mapping_params"] = {
            **value["mapping_params"],
            "f0_strength": 0.55,
        }
        uninterrupted = engine.incremental(
            new_config=PipelineConfig.from_dict(value)
        )

        crash_state = self.tmp / "increment-crash"
        self._run("increment-crash")
        crashed = self._cli(
            "incremental",
            "--data",
            str(self.dataset_root),
            "--state-dir",
            str(crash_state),
            "--mapping-version",
            "map-v2",
            "--mapping-param",
            "f0_strength=0.55",
            "--crash-mid-stage",
            "4",
            "--crash-after-utterances",
            "3",
        )
        self.assertEqual(crashed.returncode, 87)
        snapshots = json.loads(
            self._cli(
                "snapshot",
                "--data",
                str(self.dataset_root),
                "--state-dir",
                str(crash_state),
            ).stdout
        )
        self.assertEqual(snapshots["version_counter"], 5)
        resumed = self._cli(
            "incremental",
            "--data",
            str(self.dataset_root),
            "--state-dir",
            str(crash_state),
            "--mapping-version",
            "map-v2",
            "--mapping-param",
            "f0_strength=0.55",
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr + resumed.stdout)
        resumed_payload = json.loads(resumed.stdout)
        self.assertEqual(
            uninterrupted.summary()["pcm_sha256"],
            resumed_payload["pcm_sha256"],
        )
        self.assertEqual(
            resumed_payload["snapshot_versions"],
            uninterrupted.summary()["snapshot_versions"],
        )

    def test_run_resumes_committed_incremental_config_and_entries(self):
        engine = self.engine("increment-committed")
        engine.run()
        value = engine.config.to_dict()
        value["mapping_version"] = "map-v2"
        value["mapping_params"] = {
            **value["mapping_params"],
            "f0_strength": 0.55,
        }
        mapping = engine.incremental(
            new_config=PipelineConfig.from_dict(value)
        )
        resumed = self._run("increment-committed")
        self.assertEqual(resumed["executed_stages"], [])
        self.assertEqual(
            resumed["pcm_sha256"], mapping.summary()["pcm_sha256"]
        )


if __name__ == "__main__":
    unittest.main()

