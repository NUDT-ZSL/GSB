from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import ROOT, SAMPLES, sha256_hex

from multipart_store.service import MultipartStore


def _command(data_dir: Path, *args: str) -> list[str]:
    return [sys.executable, "-m", "multipart_store", "--data-dir", str(data_dir), *args]


def _env() -> dict:
    return os.environ.copy()


def test_concurrent_same_event_commit_is_idempotent(tmp_path: Path):
    data_dir = tmp_path / "store"
    subprocess.run(
        _command(data_dir, "load", str(SAMPLES / "budget.json")),
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    content = b"p0"
    checksum = f"sha256:{sha256_hex(content)}"
    args = [
        "upload",
        "--session-id",
        "sess-budget",
        "--part-number",
        "0",
        "--content-base64",
        "cDA=",
        "--checksum",
        checksum,
        "--logical-time-ms",
        "100",
        "--event-id",
        "900",
        "--expected-version",
        "0",
    ]
    process_a = subprocess.Popen(
        _command(data_dir, *args),
        cwd=ROOT,
        env=_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    process_b = subprocess.Popen(
        _command(data_dir, *args),
        cwd=ROOT,
        env=_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out_a, err_a = process_a.communicate(timeout=30)
    out_b, err_b = process_b.communicate(timeout=30)
    assert process_a.returncode == 0, (out_a, err_a)
    assert process_b.returncode == 0, (out_b, err_b)
    results = [json.loads(out_a), json.loads(out_b)]
    assert sum(1 for result in results if result["idempotent"] is False) == 1
    assert sum(1 for result in results if result["idempotent"] is True) == 1

    status = MultipartStore(data_dir).status()
    assert status["sessions"][0]["visible_version"] == 1
    succeeded = [event for event in status["events"] if event["status"] == "succeeded"]
    assert len(succeeded) == 1


def test_concurrent_different_uploads_same_expected_version_commit_once(tmp_path: Path):
    data_dir = tmp_path / "store"
    subprocess.run(
        _command(data_dir, "load", str(SAMPLES / "budget.json")),
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    base = [
        "upload",
        "--session-id",
        "sess-budget",
        "--part-number",
        "0",
        "--checksum",
        f"sha256:{sha256_hex(b'p0')}",
        "--logical-time-ms",
        "100",
        "--expected-version",
        "0",
    ]
    first = base + ["--content-base64", "cDA=", "--event-id", "910"]
    second = base + ["--content-base64", "cDE=", "--event-id", "911"]
    proc_a = subprocess.Popen(
        _command(data_dir, *first),
        cwd=ROOT,
        env=_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc_b = subprocess.Popen(
        _command(data_dir, *second),
        cwd=ROOT,
        env=_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    outputs = []
    for process in (proc_a, proc_b):
        out, err = process.communicate(timeout=30)
        outputs.append((process.returncode, json.loads(out), err))
    assert sorted(code for code, _, _ in outputs) == [0, 1]
    failure = next(payload for code, payload, _ in outputs if code == 1)
    assert failure["error"]["code"] == "E_VERSION_CONFLICT"

    status = MultipartStore(data_dir).status()
    session = status["sessions"][0]
    assert session["visible_version"] == 1
    assert len(session["visible_parts"]) == 1
    succeeded = [event for event in status["events"] if event["status"] == "succeeded"]
    assert len(succeeded) == 1


def test_concurrent_complete_produces_one_winner(tmp_path: Path):
    data_dir = tmp_path / "store"
    subprocess.run(
        _command(data_dir, "load", str(SAMPLES / "competing.json")),
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        _command(data_dir, "upload", str(SAMPLES / "competing.json")),
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    processes = [
        subprocess.Popen(
            _command(data_dir, "complete", "--session-id", session_id),
            cwd=ROOT,
            env=_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for session_id in ("sess-race-a", "sess-race-b")
    ]
    outputs = []
    for process in processes:
        out, err = process.communicate(timeout=30)
        outputs.append((process.returncode, json.loads(out), err))
    assert sorted(code for code, _, _ in outputs) == [0, 1]
    winner = next(payload for code, payload, _ in outputs if code == 0)
    loser = next(payload for code, payload, _ in outputs if code == 1)
    assert winner["status"] == "completed"
    assert loser["error"]["code"] in {
        "E_SESSION_ABORTED",
        "E_OBJECT_ALREADY_COMPLETED",
    }

    status = MultipartStore(data_dir).status()
    completed = [item for item in status["sessions"] if item["phase"] == "completed"]
    aborted = [item for item in status["sessions"] if item["phase"] == "aborted"]
    assert len(completed) == 1
    assert winner["session_id"] in {item["session_id"] for item in completed}
    assert len(aborted) == 1
    assert next(iter({item["session_id"] for item in aborted})) != winner["session_id"]


def test_cli_status_is_stably_sorted_and_verify_reports_valid(tmp_path: Path):
    data_dir = tmp_path / "store"
    subprocess.run(
        _command(data_dir, "load", str(SAMPLES / "budget.json")),
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        _command(data_dir, "upload"),
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        _command(data_dir, "upload"),
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        _command(data_dir, "complete", "--session-id", "sess-budget"),
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    status_a = subprocess.run(
        _command(data_dir, "status"),
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    status_b = subprocess.run(
        _command(data_dir, "status"),
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert status_a == status_b
    payload = json.loads(status_a)
    assert [item["session_id"] for item in payload["sessions"]] == sorted(
        item["session_id"] for item in payload["sessions"]
    )
    verify = subprocess.run(
        _command(data_dir, "verify", "--session-id", "sess-budget"),
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert verify.returncode == 0
    assert json.loads(verify.stdout)["valid"] is True
