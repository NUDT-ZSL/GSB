from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from typing import Any

from config_hub.canonical import digest_value
from config_hub.engine import ConfigHub
from config_hub.errors import ConfigHubError
from config_hub.pipeline import compute_pipeline
from config_hub.server import create_app
from config_hub.validation import (
    load_input,
    load_patch,
    load_snapshot,
    normalize_snapshot,
)

ROOT = Path(__file__).resolve().parent.parent
CTL = ROOT / "configctl.py"
SNAPSHOT = ROOT / "samples" / "config_snapshot.json"
PATCH_2 = ROOT / "samples" / "patch_002.json"
PATCH_3 = ROOT / "samples" / "patch_003.json"
INPUT = ROOT / "samples" / "input_records.json"
TOLERANCE = ROOT / "samples" / "tolerance.json"


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return env


def _run_cli(
    *arguments: str,
    env_extra: dict[str, str] | None = None,
    check: bool = True,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    env = _environment()
    if env_extra:
        env.update(env_extra)
    completed = subprocess.run(
        [sys.executable, str(CTL), *arguments],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    payload = None
    if completed.stdout.strip():
        payload = json.loads(completed.stdout)
    if check and completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {' '.join(arguments)}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed, payload


def _common(db: Path) -> list[str]:
    return [
        "--db",
        str(db),
        "--snapshot",
        str(SNAPSHOT),
        "--tolerance-file",
        str(TOLERANCE),
    ]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_digested_patch(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    payload.pop("content_digest", None)
    payload["content_digest"] = digest_value(payload)
    _write_json(path, payload)
    return payload


class ConfigHubTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="config-hub-test-")
        self.base = Path(self.temp.name)
        self.db = self.base / "hub.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _init(self) -> None:
        _run_cli("init", *_common(self.db))

    def _status(self) -> dict[str, Any]:
        _, payload = _run_cli("status", *_common(self.db))
        assert payload is not None
        return payload

    # ------------------------------------------------------------------
    # Validation and non-mutation
    # ------------------------------------------------------------------
    def test_snapshot_validation_rejects_duplicate_namespace(self) -> None:
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        snapshot["namespaces"].append(snapshot["namespaces"][0])
        snapshot.pop("content_digest", None)
        with self.assertRaises(ConfigHubError) as raised:
            normalize_snapshot(snapshot)
        self.assertEqual(raised.exception.code, "E_BAD_CONFIG")

    def test_snapshot_validation_rejects_type_mismatch_and_bad_key(self) -> None:
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        snapshot.pop("content_digest", None)
        snapshot["namespaces"][1]["configs"][0]["value"] = "not-a-number"
        with self.assertRaises(ConfigHubError) as raised:
            normalize_snapshot(snapshot)
        self.assertEqual(raised.exception.code, "E_BAD_CONFIG")

        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        snapshot.pop("content_digest", None)
        snapshot["namespaces"][1]["configs"][0]["key"] = "bad key!"
        with self.assertRaises(ConfigHubError) as raised:
            normalize_snapshot(snapshot)
        self.assertEqual(raised.exception.code, "E_BAD_CONFIG")

    def test_bad_patch_digest_returns_error_and_does_not_mutate(self) -> None:
        self._init()
        bad_patch = json.loads(PATCH_2.read_text(encoding="utf-8"))
        bad_patch["content_digest"] = "sha256:" + "0" * 64
        bad_path = self.base / "bad-patch.json"
        _write_json(bad_path, bad_patch)

        completed, payload = _run_cli(
            "apply",
            *_common(self.db),
            str(bad_path),
            "--now-ms",
            "2000",
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["error_code"], "E_BAD_PATCH")
        status = self._status()
        self.assertEqual(status["current_version"], 1)
        self.assertEqual(status["version_chain"], status["version_chain"][:1])
        self.assertEqual(status["patches"]["prepared"], 0)

    # ------------------------------------------------------------------
    # Patch windows, rollback, chain, and idempotence
    # ------------------------------------------------------------------
    def test_patch_window_rollback_and_idempotence(self) -> None:
        self._init()
        _, first = _run_cli(
            "apply", *_common(self.db), str(PATCH_2), "--now-ms", "2000"
        )
        assert first is not None
        self.assertEqual(first["version"], 2)
        self.assertFalse(first["idempotent"])

        completed, outside = _run_cli(
            "apply",
            *_common(self.db),
            str(PATCH_3),
            "--now-ms",
            "1800",
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        assert outside is not None
        self.assertEqual(outside["error_code"], "E_PATCH_NOT_EFFECTIVE")
        self.assertEqual(self._status()["current_version"], 2)

        _, applied_third = _run_cli(
            "apply", *_common(self.db), str(PATCH_3), "--now-ms", "2600"
        )
        assert applied_third is not None
        self.assertEqual(applied_third["version"], 3)

        _, repeated = _run_cli(
            "apply", *_common(self.db), str(PATCH_2), "--now-ms", "7000"
        )
        assert repeated is not None
        self.assertEqual(repeated["version"], 2)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(
            self._status()["logical_clock_watermark_ms"], 7000
        )

        # A late patch is rejected by the logical clock rollback guard.
        late = json.loads(PATCH_2.read_text(encoding="utf-8"))
        late.update(
            {
                "patch_id": "patch-004-rollback-check",
                "target_version": 4,
                "base_version": 3,
                "changes": [
                    {
                        "key": "aggregate_weight",
                        "op": "set",
                        "type": "number",
                        "value": 1.0,
                    }
                ],
                "effective_from_ms": 0,
                "effective_until_ms": 10000,
            }
        )
        late.pop("description", None)
        late_path = self.base / "late.json"
        _write_digested_patch(late_path, late)
        completed, rejected = _run_cli(
            "apply",
            *_common(self.db),
            str(late_path),
            "--now-ms",
            "1000",
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        assert rejected is not None
        self.assertEqual(rejected["error_code"], "E_CLOCK_ROLLBACK")
        self.assertEqual(self._status()["current_version"], 3)

    def test_out_of_window_patch_is_rejected(self) -> None:
        self._init()
        completed, payload = _run_cli(
            "apply",
            *_common(self.db),
            str(PATCH_2),
            "--now-ms",
            "1000",
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        assert payload is not None
        self.assertEqual(payload["error_code"], "E_PATCH_NOT_EFFECTIVE")
        self.assertEqual(self._status()["current_version"], 1)

    def test_competing_patch_for_same_version_returns_conflict(self) -> None:
        self._init()
        _run_cli("apply", *_common(self.db), str(PATCH_2), "--now-ms", "2000")
        competing = json.loads(PATCH_2.read_text(encoding="utf-8"))
        competing["patch_id"] = "patch-competing-002"
        competing["changes"][0]["value"] = 9.0
        competing.pop("content_digest", None)
        competing_path = self.base / "competing.json"
        _write_digested_patch(competing_path, competing)
        completed, payload = _run_cli(
            "apply",
            *_common(self.db),
            str(competing_path),
            "--now-ms",
            "2100",
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        assert payload is not None
        self.assertEqual(payload["error_code"], "E_VERSION_CONFLICT")
        status = self._status()
        self.assertEqual(status["current_version"], 2)
        self.assertEqual(len(status["version_chain"]), 2)

    # ------------------------------------------------------------------
    # Determinism, binding, replay, and mid-compute behavior
    # ------------------------------------------------------------------
    def test_compute_is_deterministic_and_version_bound(self) -> None:
        self._init()
        output1 = self.base / "result1.json"
        output2 = self.base / "result2.json"
        _, first = _run_cli(
            "compute",
            *_common(self.db),
            "--client",
            "client-a",
            "--round",
            "1",
            "--now-ms",
            "1200",
            "--input",
            str(INPUT),
            "--output",
            str(output1),
        )
        _, duplicate = _run_cli(
            "compute",
            *_common(self.db),
            "--client",
            "client-a",
            "--round",
            "1",
            "--now-ms",
            "1250",
            "--input",
            str(INPUT),
            "--output",
            str(output2),
        )
        assert first is not None and duplicate is not None
        self.assertTrue(duplicate["idempotent"])
        self.assertEqual(
            first["result"]["result_digest"], duplicate["result"]["result_digest"]
        )
        self.assertEqual(output1.read_bytes(), output2.read_bytes())
        self.assertEqual(first["result"]["config_version"], 1)

        _run_cli("apply", *_common(self.db), str(PATCH_2), "--now-ms", "2000")
        _, second = _run_cli(
            "compute",
            *_common(self.db),
            "--client",
            "client-a",
            "--round",
            "2",
            "--now-ms",
            "2500",
            "--input",
            str(INPUT),
        )
        assert second is not None
        self.assertEqual(second["result"]["config_version"], 2)
        status = self._status()
        self.assertEqual(status["clients"][0]["bound_version"], 2)

        _, compact1 = _run_cli("status", *_common(self.db), "--compact")
        _, compact2 = _run_cli("status", *_common(self.db), "--compact")
        self.assertEqual(compact1, compact2)
        _, verify = _run_cli(
            "verify",
            *_common(self.db),
            "--replay-version",
            "1",
            "--input",
            str(INPUT),
        )
        assert verify is not None
        self.assertTrue(verify["ok"])
        self.assertTrue(verify["replay"]["ok"])
        self.assertTrue(verify["replay"]["entries"][0]["exact_match"])

    def test_mid_compute_patch_does_not_change_bound_snapshot(self) -> None:
        self._init()
        hub = ConfigHub(self.db)
        normalized_input = load_input(INPUT)
        binding = hub.begin_compute("client-mid", 1, 1200, normalized_input)
        self.assertEqual(binding.config_version, 1)

        applied = hub.apply_patch(load_patch(PATCH_2), 2000)
        self.assertEqual(applied["version"], 2)
        result = compute_pipeline(
            binding.snapshot,
            normalized_input,
            client_id=binding.client_id,
            round_no=binding.round_no,
        )
        stored, idempotent = hub.finish_compute(binding, result)
        self.assertFalse(idempotent)
        self.assertEqual(stored["config_version"], 1)
        self.assertEqual(hub.status()["current_version"], 2)
        report = hub.verify()
        self.assertTrue(report["ok"], report["issues"])

    def test_verify_detects_tampered_result_binding(self) -> None:
        self._init()
        _run_cli(
            "compute",
            *_common(self.db),
            "--client",
            "client-tamper",
            "--round",
            "1",
            "--now-ms",
            "1200",
            "--input",
            str(INPUT),
        )
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE results SET config_digest = ? WHERE client_id = ?",
                ("sha256:" + "0" * 64, "client-tamper"),
            )
            connection.commit()
        completed, report = _run_cli(
            "verify", *_common(self.db), check=False
        )
        self.assertNotEqual(completed.returncode, 0)
        assert report is not None
        self.assertFalse(report["ok"])
        issue_codes = {issue["code"] for issue in report["issues"]}
        self.assertIn("E_RESULT_BINDING", issue_codes)

    # ------------------------------------------------------------------
    # Crash recovery
    # ------------------------------------------------------------------
    def test_prepared_patch_survives_hard_exit_and_resume(self) -> None:
        self._init()
        completed, _payload = _run_cli(
            "apply",
            *_common(self.db),
            str(PATCH_2),
            "--now-ms",
            "2000",
            env_extra={"CONFIG_HUB_FAILPOINT": "after_patch_prepare"},
            check=False,
        )
        self.assertEqual(completed.returncode, 90)
        status = self._status()
        self.assertEqual(status["current_version"], 1)
        self.assertEqual(status["patches"]["prepared"], 1)

        _, resumed = _run_cli("resume", *_common(self.db))
        assert resumed is not None
        self.assertEqual(resumed["status"]["current_version"], 2)
        self.assertEqual(resumed["status"]["patches"]["prepared"], 0)
        self.assertEqual(resumed["status"]["patches"]["applied"], 1)
        self.assertEqual(resumed["replayed_patches"][0]["status"], "applied")
        _, verify = _run_cli("verify", *_common(self.db))
        assert verify is not None
        self.assertTrue(verify["ok"], verify["issues"])

    def test_uncommitted_patch_commit_rolls_back_then_resume(self) -> None:
        self._init()
        completed, _payload = _run_cli(
            "apply",
            *_common(self.db),
            str(PATCH_2),
            "--now-ms",
            "2000",
            env_extra={"CONFIG_HUB_FAILPOINT": "before_patch_commit"},
            check=False,
        )
        self.assertEqual(completed.returncode, 90)
        status = self._status()
        self.assertEqual(status["current_version"], 1)
        self.assertEqual(len(status["version_chain"]), 1)
        self.assertEqual(status["patches"]["prepared"], 1)
        _run_cli("resume", *_common(self.db))
        self.assertEqual(self._status()["current_version"], 2)

    def test_incomplete_compute_resumes_with_original_version(self) -> None:
        for failpoint_name in (
            "after_compute_start",
            "before_result_commit",
        ):
            with self.subTest(failpoint=failpoint_name):
                with tempfile.TemporaryDirectory(
                    prefix="config-hub-crash-compute-"
                ) as temp_dir:
                    db = Path(temp_dir) / "hub.db"
                    _run_cli("init", *_common(db))
                    completed, _payload = _run_cli(
                        "compute",
                        *_common(db),
                        "--client",
                        "client-crash",
                        "--round",
                        "1",
                        "--now-ms",
                        "1200",
                        "--input",
                        str(INPUT),
                        env_extra={"CONFIG_HUB_FAILPOINT": failpoint_name},
                        check=False,
                    )
                    self.assertEqual(completed.returncode, 90)
                    status = self._status_for(db)
                    self.assertEqual(status["compute_runs"]["in_progress"], 1)
                    _, resumed = _run_cli("resume", *_common(db))
                    assert resumed is not None
                    self.assertEqual(
                        resumed["status"]["compute_runs"]["in_progress"], 0
                    )
                    self.assertEqual(
                        resumed["status"]["compute_runs"]["completed"], 1
                    )
                    self.assertEqual(
                        resumed["replayed_computations"][0]["config_version"], 1
                    )
                    _, verify = _run_cli("verify", *_common(db))
                    assert verify is not None
                    self.assertTrue(verify["ok"], verify["issues"])

    def _status_for(self, db: Path) -> dict[str, Any]:
        _, payload = _run_cli("status", *_common(db))
        assert payload is not None
        return payload

    # ------------------------------------------------------------------
    # Concurrency and HTTP
    # ------------------------------------------------------------------
    def test_multiple_processes_apply_same_patch_once(self) -> None:
        self._init()
        env = _environment()
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(CTL),
                    "apply",
                    *_common(self.db),
                    str(PATCH_2),
                    "--now-ms",
                    "2000",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(4)
        ]
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(json.loads(stdout))
        self.assertEqual({item["version"] for item in outputs}, {2})
        self.assertEqual(sum(not item["idempotent"] for item in outputs), 1)
        status = self._status()
        self.assertEqual(status["current_version"], 2)
        self.assertEqual(status["patches"]["applied"], 1)

    def test_multiple_processes_commit_same_compute_once(self) -> None:
        self._init()
        env = _environment()
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(CTL),
                    "compute",
                    *_common(self.db),
                    "--client",
                    "parallel-client",
                    "--round",
                    "1",
                    "--now-ms",
                    "1200",
                    "--input",
                    str(INPUT),
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(4)
        ]
        digests = set()
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            digests.add(json.loads(stdout)["result"]["result_digest"])
        self.assertEqual(len(digests), 1)
        status = self._status()
        self.assertEqual(status["compute_runs"]["completed"], 1)
        _, verify = _run_cli("verify", *_common(self.db))
        assert verify is not None
        self.assertTrue(verify["ok"], verify["issues"])

    def test_http_patch_and_status(self) -> None:
        self._init()
        hub = ConfigHub(self.db)
        app = create_app(hub)
        client = app.test_client()
        patch = load_patch(PATCH_2)
        response = client.post(
            "/v1/patches",
            data=json.dumps(
                {"logical_clock_ms": 2000, "patch": patch}, sort_keys=True
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["version"], 2)
        self.assertFalse(payload["idempotent"])

        response = client.post(
            "/v1/patches",
            data=json.dumps(
                {"logical_clock_ms": 7000, "patch": patch}, sort_keys=True
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["idempotent"])

        response = client.get("/v1/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["current_version"], 2)

        response = client.get("/v1/config?version=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["config"]["version"], 1)

    def test_live_loopback_http_server(self) -> None:
        self._init()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen(
            [
                sys.executable,
                str(CTL),
                "serve",
                *_common(self.db),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=str(ROOT),
            env=_environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    with urllib.request.urlopen(
                        base_url + "/health", timeout=0.5
                    ) as response:
                        health = json.loads(response.read())
                    self.assertTrue(health["ok"])
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        self.fail("live HTTP server did not become ready")
                    time.sleep(0.05)

            request = urllib.request.Request(
                base_url + "/v1/patches",
                data=json.dumps(
                    {
                        "logical_clock_ms": 2000,
                        "patch": load_patch(PATCH_2),
                    },
                    sort_keys=True,
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read())
            self.assertEqual(payload["version"], 2)
            with urllib.request.urlopen(base_url + "/v1/status", timeout=5) as response:
                status = json.loads(response.read())
            self.assertEqual(status["current_version"], 2)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def test_non_loopback_host_is_rejected(self) -> None:
        from config_hub.server import validate_loopback_host

        with self.assertRaises(ConfigHubError) as raised:
            validate_loopback_host("0.0.0.0")
        self.assertEqual(raised.exception.code, "E_BAD_HOST")


if __name__ == "__main__":
    unittest.main()
