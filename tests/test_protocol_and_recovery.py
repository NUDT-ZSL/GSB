from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from replica_ledger.protocol import NodeClient, NodeServer
from replica_ledger.spec import load_replica_group

from tests.common import PROJECT_ROOT, SampleWorkspace


class ProtocolTests(SampleWorkspace):
    def test_digest_get_put_and_cold_restart(self) -> None:
        root = self.copy_scenario("single")
        spec = load_replica_group(root / "spec.json")
        log_path = Path(self._tempdir.name) / "protocol.jsonl"
        first = NodeServer(spec, "n1", port=0, log_path=log_path).start()
        try:
            with NodeClient(*first.endpoint) as client:
                digest = client.query_digest("B-SINGLE")
                payload, observed_digest, byte_size = client.get_block("B-SINGLE")
                self.assertEqual(digest["digest"], observed_digest)
                self.assertEqual(byte_size, 1536)
                put = client.put_block(
                    "B-SINGLE",
                    payload,
                    expected_digest=observed_digest,
                    expected_byte_size=byte_size,
                )
                self.assertTrue(put["committed"])
        finally:
            first.stop()
        node_dir = root / "nodes" / "n1"
        self.assertFalse(list(node_dir.glob("*.tmp-*")))
        second = NodeServer(spec, "n1", port=0, log_path=log_path).start()
        try:
            with NodeClient(*second.endpoint) as client:
                after = client.query_digest("B-SINGLE")
            self.assertEqual(after["digest"], digest["digest"])
        finally:
            second.stop()
        recovery_events = [
            json.loads(line)
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if json.loads(line)["event"] == "NODE_RECOVERY_SCAN"
        ]
        self.assertTrue(recovery_events)
        self.assertFalse(recovery_events[-1]["context"]["exit_report"])

    def test_forced_kill_then_restart_uses_persistent_files(self) -> None:
        root = self.copy_scenario("single")
        spec_path = root / "spec.json"
        run_dir = Path(self._tempdir.name) / "kill-run"
        log_path = Path(self._tempdir.name) / "kill-node.jsonl"
        pid_name = f"node-{hashlib.sha256(b'n1').hexdigest()[:32]}.json"
        pid_path = run_dir / pid_name
        command = [
            sys.executable,
            "-m",
            "replica_ledger.cli",
            "serve",
            "--spec",
            str(spec_path),
            "--node-id",
            "n1",
            "--port",
            "0",
            "--run-dir",
            str(run_dir),
            "--log",
            str(log_path),
        ]

        def wait_ready(old_pid: int | None = None) -> dict:
            for _ in range(240):
                try:
                    info = json.loads(pid_path.read_text(encoding="utf-8"))
                    if old_pid is not None and info["pid"] == old_pid:
                        time.sleep(0.025)
                        continue
                    with NodeClient("127.0.0.1", info["port"], timeout=0.5) as client:
                        client.ping()
                    return info
                except Exception:
                    time.sleep(0.025)
            raise AssertionError("node did not become ready")

        first = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        second = None
        try:
            info = wait_ready()
            with NodeClient("127.0.0.1", info["port"]) as client:
                before = client.query_digest("B-SINGLE")["digest"]
            first.kill()
            first.wait(timeout=10)
            second = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            info2 = wait_ready(old_pid=info["pid"])
            with NodeClient("127.0.0.1", info2["port"]) as client:
                after = client.query_digest("B-SINGLE")["digest"]
            self.assertEqual(before, after)
        finally:
            for process in (first, second):
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
