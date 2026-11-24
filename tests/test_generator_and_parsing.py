from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from certpath.engine import CertPathEngine, parse_sample
from certpath.generator import generate_sample
from tests.helpers import append_v2_record, load_json, write_json


def test_generator_is_byte_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    one = generate_sample(first)
    two = generate_sample(second)
    assert one["sync_marker"] == two["sync_marker"]
    assert load_json(first / "sync.marker.json") == load_json(
        second / "sync.marker.json"
    )
    assert load_json(first / "truth.json") == load_json(second / "truth.json")
    for rel in (
        "snapshots/v2/meta.db",
        "snapshots/v2/shards/shard_0.json",
        "snapshots/v2/shards/shard_1.json",
    ):
        assert (first / rel).read_bytes() == (second / rel).read_bytes()


def test_truth_validation_uses_sync_marker(sample_dir: Path) -> None:
    process = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts/validate_samples.py"),
            "--samples",
            str(sample_dir),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["ok"] is True
    assert result["sync_marker"]
    assert result["chains_checked"] == 13


def test_invalid_certificate_records_are_rejected_and_others_continue(
    sample_copy: Path,
    tmp_path: Path,
) -> None:
    certificates = load_json(sample_copy / "certificates.json")
    records = certificates["certificates"]
    duplicate = dict(records[0])
    records.append(duplicate)
    records.append(
        {
            **dict(records[0]),
            "certificate_id": "bad-fingerprint",
            "public_key_fingerprint": "sha256:xyz",
        }
    )
    records.append(
        {
            **dict(records[0]),
            "certificate_id": "bad-window",
            "not_before_ms": 10,
            "not_after_ms": 10,
        }
    )
    records.append(
        {
            **dict(records[0]),
            "certificate_id": "bad-bool",
            "subject_id": "extra-subject",
            "cross_signed": "yes",
        }
    )
    records.append(
        {
            **dict(records[0]),
            "certificate_id": "orphan",
            "subject_id": "orphan-subject",
            "issuer_id": "missing-issuer",
        }
    )
    write_json(sample_copy / "certificates.json", certificates)

    parsed = parse_sample(sample_copy)
    codes = {item.code for item in parsed.certificate_rejections}
    assert {
        "E_DUP_CERT_ID",
        "E_FINGERPRINT",
        "E_TIME_WINDOW",
        "E_BOOL",
        "E_ISSUER_UNRESOLVED",
    }.issubset(codes)
    assert all(
        item.file_id == "certificates.json" for item in parsed.certificate_rejections
    )
    assert all(item.record_index >= 21 for item in parsed.certificate_rejections)
    assert len(parsed.certificates) == 21

    report = CertPathEngine(tmp_path / "state").adjudicate(sample_copy)
    assert report["summary"]["chains_total"] == 13
    assert report["summary"]["parse_rejections"] >= 5
    assert report["summary"]["all_inputs_valid"] is False


def test_invalid_v2_record_is_rejected_without_losing_snapshot(
    sample_copy: Path,
    tmp_path: Path,
) -> None:
    append_v2_record(
        sample_copy,
        {
            "anchor_id": "bad-path-length",
            "fingerprint": "sha256:" + "ab" * 32,
            "subject_id": "bad-root",
            "effective_ms": 1700000008000,
            "generation": 2,
            "path_length": -1,
            "trusted": True,
            "tombstone": False,
            "revoked": False,
        },
    )
    parsed = parse_sample(sample_copy)
    assert parsed.v2 is not None
    assert len(parsed.v2.anchors) == 5
    assert any(
        item.code == "E_PATH_LENGTH" and "shard_0.json" in item.file_id
        for item in parsed.v2_rejections
    )
    report = CertPathEngine(tmp_path / "state").adjudicate(sample_copy)
    assert report["summary"]["parse_rejections"] >= 1
    assert any(
        item["code"] == "E_PATH_LENGTH" for item in report["rejections"]
    )


def test_invalid_v1_clock_aborts_before_state_change(
    sample_copy: Path,
    tmp_path: Path,
) -> None:
    v1_path = sample_copy / "snapshots/v1/trust_v1.json"
    v1 = load_json(v1_path)
    v1["relative_effective_seconds"] = -1
    write_json(v1_path, v1)
    engine = CertPathEngine(tmp_path / "state")
    try:
        engine.import_sample(sample_copy)
    except Exception as exc:
        assert getattr(exc, "code", None) == "E_V1_CLOCK"
    else:
        raise AssertionError("invalid v1 clock was accepted")
    assert engine.store.load_verdict_rows() == []


def test_invalid_v1_root_items_are_rejected_individually(
    sample_copy: Path,
) -> None:
    v1_path = sample_copy / "snapshots/v1/trust_v1.json"
    v1 = load_json(v1_path)
    v1["trusted_root_ids"].extend(
        [42, "cert-root-unknown", "cert-root-alpha"]
    )
    write_json(v1_path, v1)
    parsed = parse_sample(sample_copy)
    assert parsed.v1 is not None
    assert len(parsed.v1.anchors) == 3
    codes = [item.code for item in parsed.v1_rejections]
    assert "E_ID" in codes
    assert "E_ROOT_REF_UNRESOLVED" in codes
    assert "E_DUP_ROOT_REF" in codes
    assert all(item.file_id == "trust_v1.json" for item in parsed.v1_rejections)
