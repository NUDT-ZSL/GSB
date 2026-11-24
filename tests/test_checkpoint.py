from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tlsdiag import AnalysisConfig, analyze_run
from tlsdiag import codes
from tlsdiag.generate import SampleBuilder
from tlsdiag.models import ConnectionResult, canonical_json, digest_value, sha256_hex

from conftest import run_profile


ROOT = Path(__file__).resolve().parents[1]


def _rechecksum(payload: dict) -> dict:
    body = dict(payload)
    body.pop("payload_sha256", None)
    body["payload_sha256"] = sha256_hex(canonical_json(body))
    return body


def test_resume_verifies_replay_and_keeps_conclusion(sample_dir, expected, tmp_path) -> None:
    output = tmp_path / "resume"
    first = run_profile(sample_dir, expected, "default", output, jobs=1)
    second = run_profile(sample_dir, expected, "default", output, jobs=4, resume=True)
    assert first.report["summary"]["conclusion_digest"] == second.report["summary"]["conclusion_digest"]
    checkpoint = second.report["checkpoint"]
    assert checkpoint["resumed"] is True
    assert checkpoint["replay_verified_connections"] == 15
    assert checkpoint["recovered_from"] == "main"
    assert checkpoint["half_write_detected"] is False


def test_corrupt_primary_checkpoint_falls_back_to_previous_complete(
    sample_dir, expected, tmp_path
) -> None:
    output = tmp_path / "halfwrite"
    first = run_profile(sample_dir, expected, "default", output, jobs=1)
    checkpoint_path = output / "checkpoint.json"
    previous_path = output / "checkpoint.prev.json"
    assert previous_path.exists()
    checkpoint_path.write_text('{"half":', encoding="utf-8")
    second = run_profile(sample_dir, expected, "default", output, jobs=1, resume=True)
    assert first.report["summary"]["conclusion_digest"] == second.report["summary"]["conclusion_digest"]
    checkpoint = second.report["checkpoint"]
    assert checkpoint["recovered_from"] == "backup"
    assert checkpoint["half_write_detected"] is True
    assert checkpoint["resumed"] is True


def test_stale_temp_files_are_cleaned_on_resume(sample_dir, expected, tmp_path) -> None:
    output = tmp_path / "temp-cleanup"
    run_profile(sample_dir, expected, "default", output, jobs=1)
    temp = output / ".checkpoint.json.123.456.tmp"
    temp.write_bytes(b"half-written")
    second = run_profile(sample_dir, expected, "default", output, jobs=1, resume=True)
    assert str(temp) in second.report["checkpoint"]["cleaned_temp_files"]
    assert not temp.exists()


@pytest.mark.parametrize("field,reason", [
    ("structure_version", "checkpoint_structure_version_changed"),
    ("merge_policy_version", "merge_policy_version_changed"),
])
def test_checkpoint_version_change_rebuilds(
    sample_dir, expected, tmp_path, field, reason
) -> None:
    output = tmp_path / f"version-{field}"
    first = run_profile(sample_dir, expected, "default", output, jobs=1)
    checkpoint_path = output / "checkpoint.json"
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    payload[field] = "changed-version"
    payload = _rechecksum(payload)
    checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
    previous = output / "checkpoint.prev.json"
    if previous.exists():
        previous.unlink()
    second = run_profile(sample_dir, expected, "default", output, jobs=1, resume=True)
    assert first.report["summary"]["conclusion_digest"] == second.report["summary"]["conclusion_digest"]
    checkpoint = second.report["checkpoint"]
    assert checkpoint["resumed"] is False
    assert checkpoint["rebuild"] is True
    assert reason in checkpoint["rebuild_reasons"]


def test_replay_mismatch_rebuilds(sample_dir, expected, tmp_path) -> None:
    output = tmp_path / "mismatch"
    first = run_profile(sample_dir, expected, "default", output, jobs=1)
    checkpoint_path = output / "checkpoint.json"
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    entry = payload["completed"]["conn-broken"]
    modified = entry["result"]
    modified["trust_conclusion"] = "trusted"
    modified["primary_code"] = None
    modified["selected_chain"] = None
    modified["candidate_chains"] = []
    restored = ConnectionResult.from_dict(modified)
    entry["digest"] = restored.compute_digest()
    payload["aggregate_digest"] = digest_value(
        [
            (connection_id, payload["completed"][connection_id]["digest"])
            for connection_id in sorted(payload["completed"])
        ]
    )
    payload = _rechecksum(payload)
    checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
    previous = output / "checkpoint.prev.json"
    if previous.exists():
        previous.unlink()
    second = run_profile(sample_dir, expected, "default", output, jobs=1, resume=True)
    assert first.report["summary"]["conclusion_digest"] == second.report["summary"]["conclusion_digest"]
    checkpoint = second.report["checkpoint"]
    assert checkpoint["resumed"] is False
    assert checkpoint["rebuild"] is True
    assert "replay_mismatch" in checkpoint["rebuild_reasons"]
    assert any(d["code"] == codes.E_REPLAY_MISMATCH for d in second.report["input_diagnostics"])


def test_input_change_rebuilds(sample_dir, expected, tmp_path) -> None:
    output = tmp_path / "input-change"
    run_profile(sample_dir, expected, "default", output, jobs=1)
    changed = tmp_path / "changed.jsonl"
    changed.write_bytes((sample_dir / "input/handshake.jsonl").read_bytes() + b"\n")
    profile = expected["profiles"]["default"]
    run = analyze_run(
        handshake_path=changed,
        v1_path=sample_dir / profile["v1"],
        v2_shards=sample_dir / profile["v2_shards"],
        v2_db=sample_dir / profile["v2_db"],
        output_dir=output,
        config=AnalysisConfig(resume=True),
        resume=True,
    )
    checkpoint = run.report["checkpoint"]
    assert checkpoint["resumed"] is False
    assert checkpoint["rebuild"] is True
    assert "input_fingerprint_changed" in checkpoint["rebuild_reasons"]


def _large_handshake(sample_dir: Path, path: Path, count: int = 120) -> None:
    builder = SampleBuilder(20261125)
    lines = []
    for index in range(count):
        connection_id = f"kill-{index:04d}"
        records = builder.connection_records(
            connection_id,
            [
                (1000 + index * 2, builder.certificate_message(["leaf_ok", "int_ok", "root_alpha"])),
                (1001 + index * 2, builder.handshake_message(connection_id)),
            ],
            chunk_size=4096,
        )
        lines.extend(
            json.dumps(record, sort_keys=True, separators=(",", ":"))
            for record in records
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_forced_kill_recovers_only_last_complete_checkpoint(
    sample_dir, expected, tmp_path
) -> None:
    handshake = tmp_path / "kill.jsonl"
    _large_handshake(sample_dir, handshake, count=120)
    profile = expected["profiles"]["default"]
    reference = analyze_run(
        handshake_path=handshake,
        v1_path=sample_dir / profile["v1"],
        v2_shards=sample_dir / profile["v2_shards"],
        v2_db=sample_dir / profile["v2_db"],
        output_dir=tmp_path / "reference",
        config=AnalysisConfig(jobs=4),
    )
    output = tmp_path / "killed"
    checkpoint_path = output / "checkpoint.json"
    command = [
        sys.executable,
        "-m",
        "tlsdiag",
        "analyze",
        "--handshake",
        str(handshake),
        "--v1",
        str(sample_dir / profile["v1"]),
        "--v2-shards",
        str(sample_dir / profile["v2_shards"]),
        "--v2-db",
        str(sample_dir / profile["v2_db"]),
        "--out",
        str(output),
        "--jobs",
        "1",
        "--timeout-ms",
        "5000",
        "--json",
    ]
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 30
    saw_partial = False
    try:
        while time.time() < deadline:
            if process.poll() is not None:
                break
            if checkpoint_path.exists():
                try:
                    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    payload = None
                if payload and 1 <= payload.get("completed_count", 0) < 120:
                    process.kill()
                    saw_partial = True
                    break
            time.sleep(0.005)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
    if not saw_partial:
        pytest.skip("analysis completed before a partial checkpoint could be killed")
    assert not (output / "report.json").exists()

    resumed = analyze_run(
        handshake_path=handshake,
        v1_path=sample_dir / profile["v1"],
        v2_shards=sample_dir / profile["v2_shards"],
        v2_db=sample_dir / profile["v2_db"],
        output_dir=output,
        config=AnalysisConfig(jobs=4, resume=True),
        resume=True,
    )
    assert (
        resumed.report["summary"]["conclusion_digest"]
        == reference.report["summary"]["conclusion_digest"]
    )
    checkpoint = resumed.report["checkpoint"]
    assert checkpoint["resumed"] is True
    assert checkpoint["replay_verified_connections"] >= 1
