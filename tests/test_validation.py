from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dynabatch.errors import BusinessError
from dynabatch.validation import validate_bundle


def _code(bundle, mutate) -> str:
    value = json.loads(json.dumps(bundle))
    mutate(value)
    with pytest.raises(BusinessError) as exc:
        validate_bundle(value)
    return exc.value.code


def test_validation_error_codes(tiny_bundle) -> None:
    assert (
        _code(
            tiny_bundle,
            lambda b: b["replica_versions"][0]["replicas"].append(
                dict(b["replica_versions"][0]["replicas"][0])
            ),
        )
        == "E_REPLICA_DUPLICATE"
    )
    assert (
        _code(
            tiny_bundle,
            lambda b: b["replica_versions"][0]["replicas"][0].update(
                {"compute_units": 0}
            ),
        )
        == "E_REPLICA_COMPUTE"
    )
    assert (
        _code(
            tiny_bundle,
            lambda b: b["replica_versions"][0]["replicas"][0].update(
                {"memory_mib": 0}
            ),
        )
        == "E_REPLICA_MEMORY"
    )
    assert (
        _code(
            tiny_bundle,
            lambda b: b["replica_versions"][0]["replicas"][0].update(
                {"max_batch_size": 0}
            ),
        )
        == "E_REPLICA_BATCH_SIZE"
    )
    assert (
        _code(
            tiny_bundle,
            lambda b: b["replica_versions"][0]["replicas"][0].update(
                {"max_sequence_length": 7}
            ),
        )
        == "E_SEQUENCE_LENGTH"
    )
    assert (
        _code(tiny_bundle, lambda b: b["requests"].append(dict(b["requests"][0])))
        == "E_REQUEST_DUPLICATE"
    )
    assert (
        _code(
            tiny_bundle,
            lambda b: b["requests"][0].update({"arrival_ms": -1}),
        )
        == "E_REQUEST_TIME"
    )
    assert (
        _code(
            tiny_bundle,
            lambda b: b["requests"][0].update({"deadline_ms": -1}),
        )
        == "E_REQUEST_TIME"
    )
    assert (
        _code(
            tiny_bundle,
            lambda b: b["requests"][0].update({"arrival_ms": 9, "deadline_ms": 8}),
        )
        == "E_REQUEST_DEADLINE"
    )
    assert (
        _code(
            tiny_bundle,
            lambda b: b["precision"].update({"compute_scale": 0}),
        )
        == "E_PRECISION_SCALE"
    )
    assert (
        _code(
            tiny_bundle,
            lambda b: b["precision"].update({"tolerance": 0}),
        )
        == "E_PRECISION_TOLERANCE"
    )
    assert (
        _code(
            tiny_bundle,
            lambda b: b["scheduler"].update({"max_concurrent_batches": 0}),
        )
        == "E_CONFIG_CONCURRENCY"
    )


def test_invalid_cli_write_leaves_no_database(tmp_path, tiny_bundle) -> None:
    bundle = json.loads(json.dumps(tiny_bundle))
    bundle["replica_versions"][0]["replicas"][0]["compute_units"] = 0
    bundle_path = tmp_path / "invalid.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    db_path = tmp_path / "must-not-exist.db"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "dynabatch",
            "save",
            "--bundle",
            str(bundle_path),
            "--db",
            str(db_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "E_REPLICA_COMPUTE" in result.stderr
    assert not db_path.exists()
    assert not Path(str(db_path) + ".lock").exists()


def test_sample_digest_matches_manifest(tiny_bundle) -> None:
    from dynabatch.canonical import digest_json

    root = Path(__file__).resolve().parents[1]
    bundle = json.loads((root / "sample" / "bundle.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (root / "sample" / "manifest.json").read_text(encoding="utf-8")
    )
    assert digest_json(bundle) == manifest["canonical_sha256"]
    validate_bundle(bundle)
