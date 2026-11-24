from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from offline_authz.cli import main
from offline_authz.factors import build_proof_digest
from offline_authz.fingerprint import normalize_fingerprint


def _write_clean_bundle(root: Path) -> None:
    root.mkdir(parents=True)
    t0 = 1_767_225_600_000
    fp = normalize_fingerprint(
        "WINDOWS", 2, [{"seq": 1, "components": {"cpu_score": 123456}}], 6
    )
    factor = {
        "factor_id": "fac-1",
        "factor_type": "TOTP",
        "secret_placeholder_digest": "sha256:secret",
    }
    proof_extra = {"code": "123456", "time_step": 1}
    proof = {
        **proof_extra,
        "factor_id": "fac-1",
        "factor_type": "TOTP",
        "challenge": "challenge-1",
        "proof_digest": build_proof_digest(factor, "challenge-1", **proof_extra),
    }
    (root / "user_credentials.jsonl").write_text(
        json.dumps({
            "user_id": "u-1",
            "salt": "salt",
            "password_digest": "sha256:password",
            "digest_tier": 3,
            "iterations": 200_000,
        }) + "\n",
        encoding="utf-8",
    )
    (root / "device_fingerprints.jsonl").write_text(
        json.dumps({
            "device_id": "dev-1",
            "user_id": "u-1",
            "platform": "WINDOWS",
            "series": [{"seq": 1, "components": {"cpu_score": 123456}}],
        }) + "\n",
        encoding="utf-8",
    )
    (root / "factor_bindings.jsonl").write_text(
        json.dumps({
            **factor,
            "user_id": "u-1",
            "device_id": "dev-1",
            "status": "BOUND",
        }) + "\n",
        encoding="utf-8",
    )
    (root / "login_attempts.jsonl").write_text(
        json.dumps({
            "attempt_seq": 1,
            "user_id": "u-1",
            "device_id": "dev-1",
            "policy_key": "p1",
            "observed_at_ms": t0 + 1_000,
            "logical_clock": 1,
            "batch_watermark": 1,
            "factors": [proof],
        }) + "\n",
        encoding="utf-8",
    )
    for name in ["sessions.jsonl", "dependency_edges.jsonl", "revocation_events.jsonl"]:
        (root / name).write_text("", encoding="utf-8")

    (root / "policy_v1").mkdir()
    (root / "policy_v2" / "shards").mkdir(parents=True)
    (root / "policy_v1" / "policy_snapshot.json").write_text(
        json.dumps({
            "policies": [{
                "policy_key": "p1",
                "generation": 1,
                "expires_in_seconds": 3600,
                "required_factors": ["TOTP"],
                "binding_requirement": "CANONICAL_FINGERPRINT",
                "min_digest_tier": 1,
                "min_iterations": 1,
            }]
        }),
        encoding="utf-8",
    )
    (root / "policy_v2" / "shards" / "records.json").write_text(
        json.dumps({"records": [
            {
                "record_kind": "policy",
                "policy_key": "p1",
                "generation": 2,
                "effective_at_ms": t0,
                "expires_at_ms": t0 + 3_600_000,
                "required_factors": ["TOTP"],
                "binding_requirement": "CANONICAL_FINGERPRINT",
                "min_digest_tier": 1,
                "min_iterations": 1,
            },
            {
                "record_kind": "binding",
                "binding_id": "bind-1",
                "policy_key": "p1",
                "user_id": "u-1",
                "device_id": "dev-1",
                "canonical_fingerprint_key": fp["canonical_fingerprint_key"],
                "canonical_vector": fp["points"],
                "generation": 2,
                "effective_at_ms": t0,
                "tombstoned": False,
            },
        ]}),
        encoding="utf-8",
    )
    db = root / "policy_v2" / "policy_meta.sqlite"
    connection = sqlite3.connect(db)
    connection.executescript(
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE policy_meta(policy_key TEXT, generation INTEGER, source_shard TEXT, "
        "record_seq INTEGER, tombstoned INTEGER, batch_watermark INTEGER, "
        "effective_at_ms INTEGER, expires_at_ms INTEGER);"
    )
    connection.commit()
    connection.close()
    (root / "bundle_manifest.json").write_text(
        json.dumps({
            "snapshot_version": "clean-1",
            "layout_version": 2,
            "observation_clock_ms": t0,
            "canonical_fingerprint_decimals": 6,
            "fingerprint_match_threshold_canonical_units": 10,
            "platform_scales": {"WINDOWS": 2},
        }),
        encoding="utf-8",
    )


def test_clean_cli_adjudication_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bundle = tmp_path / "clean"
    _write_clean_bundle(bundle)
    code = main([
        "adjudicate", "--input-dir", str(bundle),
        "--state-dir", str(tmp_path / "state"), "--watermark", "1",
    ])
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["summary"] == {"total": 1, "allow": 1, "deny": 0}


def test_seeded_partial_sample_exits_one(sample_dir: Path, tmp_path: Path) -> None:
    code = main([
        "adjudicate", "--input-dir", str(sample_dir),
        "--state-dir", str(tmp_path / "state"), "--watermark", "1",
    ])
    assert code == 1


def test_verify_empty_state_exits_zero(tmp_path: Path) -> None:
    assert main(["verify-checkpoint", "--state-dir", str(tmp_path / "empty")]) == 0


def test_invalid_worker_parameter_exits_two() -> None:
    assert main(["report", "--workers", "0"]) == 2


def test_argparse_parameter_error_exits_two() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["not-a-command"])
    assert exc.value.code == 2
