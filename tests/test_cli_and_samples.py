from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import REPOSITORY_ROOT, write_json
from trustman.models import CERT_SCHEMA, CHAIN_SCHEMA, SNAPSHOT_SCHEMA
from trustman.samples import generate_samples


def _run(*args: str, cwd: Path = REPOSITORY_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "trustman", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _validate_args(
    workspace: Path, samples: Path, report: Path, *extra: str
) -> list[str]:
    return [
        "validate",
        "--workspace",
        str(workspace),
        "--certificate",
        str(samples / "certificates.json"),
        "--chain",
        str(samples / "chains.json"),
        "--snapshot",
        str(samples / "snapshots" / "v1.json"),
        "--snapshot",
        str(samples / "snapshots" / "v2.json"),
        "--snapshot",
        str(samples / "snapshots" / "v3.json"),
        "--report",
        str(report),
        *extra,
    ]


def _directory_hashes(directory: Path) -> dict[str, str]:
    import hashlib

    return {
        str(path.relative_to(directory)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_generator_is_byte_deterministic(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_samples(first)
    generate_samples(second)
    assert _directory_hashes(first) == _directory_hashes(second)


def test_cli_exit_codes_report_and_query(tmp_path: Path, samples: Path):
    workspace = tmp_path / "workspace"
    report = workspace / "report.json"
    success = _run(*_validate_args(workspace, samples, report, "--workers", "4"))
    assert success.returncode == 0, success.stderr + success.stdout
    payload = json.loads(success.stdout)
    assert payload["ok"] is True
    assert payload["summary"]["succeeded"] == 9
    assert all(payload["reconciliation"].values())

    queried = _run(
        "query",
        "--workspace",
        str(workspace),
        "--chain-id",
        "chain-expired",
        "--report",
        str(report),
    )
    assert queried.returncode == 0
    query_payload = json.loads(queried.stdout)
    assert query_payload["result"]["conclusion"] == "partially_trusted"
    assert query_payload["source"] == "report"

    checkpoint = _run("check-checkpoint", "--workspace", str(workspace))
    assert checkpoint.returncode == 0
    assert json.loads(checkpoint.stdout)["code"] == "CHECKPOINT_OK"

    partial_workspace = tmp_path / "partial"
    partial_report = partial_workspace / "report.json"
    partial = _run(
        *_validate_args(
            partial_workspace,
            samples,
            partial_report,
            "--queue-limit",
            "1",
        )
    )
    assert partial.returncode == 1
    partial_payload = json.loads(partial.stdout)
    assert partial_payload["summary"]["rejected"] == 8
    assert partial_payload["summary"]["queue_rejected"] == 8
    assert partial_payload["reconciliation"]["task_counts_match"] is True

    missing_file = _run(
        "validate",
        "--workspace",
        str(tmp_path / "bad"),
        "--certificate",
        str(tmp_path / "missing.json"),
        "--chain",
        str(samples / "chains.json"),
    )
    assert missing_file.returncode == 2
    assert json.loads(missing_file.stdout)["code"] == "E_ARGS"

    missing_arg = _run("validate", "--workspace", str(tmp_path / "bad"))
    assert missing_arg.returncode == 2


def test_schema_rejection_is_reported_without_stopping_other_chains(
    tmp_path: Path,
):
    certificates = [
        {
            "id": "root-a",
            "issuer_id": "root-a",
            "public_key_fingerprint": "sha256:" + "a" * 64,
            "valid_from_ms": 100,
            "valid_to_ms": 300,
            "revoked": False,
        },
        {
            "id": "leaf-a",
            "issuer_id": "root-a",
            "public_key_fingerprint": "sha256:" + "b" * 64,
            "valid_from_ms": 100,
            "valid_to_ms": 300,
            "revoked": False,
        },
        {
            "id": "bad-cert",
            "issuer_id": "root-a",
            "public_key_fingerprint": "sha256:short",
            "valid_from_ms": 100,
            "valid_to_ms": 300,
            "revoked": False,
        },
    ]
    cert_path = write_json(
        tmp_path / "certificates.json",
        {"schema": CERT_SCHEMA, "certificates": certificates},
    )
    snapshot_path = write_json(
        tmp_path / "snapshot.json",
        {
            "schema": SNAPSHOT_SCHEMA,
            "snapshots": [
                {
                    "version": 1,
                    "policy_version": "1.0.0",
                    "trusted_roots": ["root-a"],
                    "revoked_entries": [],
                    "observed_at_ms": 200,
                }
            ],
        },
    )
    chains = [
        {
            "chain_id": "chain-ok",
            "certificate_ids": ["leaf-a", "root-a"],
            "deadline_ms": 1_000,
            "expected_summary": "trusted:snapshot=1:policy=1.0.0",
        },
        {
            "chain_id": "chain-bad-schema",
            "certificate_ids": ["leaf-a", "leaf-a"],
            "deadline_ms": 1_000,
            "expected_summary": "untrusted",
        },
        {
            "chain_id": "chain-missing-cert",
            "certificate_ids": ["leaf-a", "missing-root"],
            "deadline_ms": 1_000,
            "expected_summary": "untrusted:snapshot=1:policy=1.0.0",
        },
    ]
    chain_path = write_json(
        tmp_path / "chains.json",
        {"schema": CHAIN_SCHEMA, "chains": chains},
    )
    workspace = tmp_path / "workspace"
    report = workspace / "report.json"
    result = _run(
        "validate",
        "--workspace",
        str(workspace),
        "--certificate",
        str(cert_path),
        "--chain",
        str(chain_path),
        "--snapshot",
        str(snapshot_path),
        "--report",
        str(report),
    )
    assert result.returncode == 1
    loaded = json.loads(report.read_text(encoding="utf-8"))
    chains_by_id = {
        chain["chain_id"]: chain for chain in loaded["chains"]
    }
    assert chains_by_id["chain-ok"]["conclusion"] == "trusted"
    assert chains_by_id["chain-bad-schema"]["status"] == "rejected"
    assert chains_by_id["chain-bad-schema"]["errors"][0]["code"] == "E_SCHEMA"
    assert chains_by_id["chain-missing-cert"]["errors"][0]["code"] == "E_MISSING_CERT"
    assert loaded["summary"]["tasks_total"] == 3
    assert loaded["summary"]["succeeded"] == 2
    assert loaded["summary"]["rejected"] == 1
    assert loaded["summary"]["input_rejections"] == 1
    assert loaded["summary"]["task_rejections"] == 1
    assert loaded["summary"]["rejections_total"] == 2
    assert all(loaded["reconciliation"].values())
    rejection = next(
        item
        for item in loaded["rejections"]
        if item["scope"] == "chain"
    )
    assert rejection["source_file"] == "chains.json"
    assert rejection["entry_index"] == 1
    assert rejection["entry_number"] == 2
    assert rejection["code"] == "E_SCHEMA"
