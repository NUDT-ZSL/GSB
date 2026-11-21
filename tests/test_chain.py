from __future__ import annotations

import json
from pathlib import Path

from certlab.engine import SampleRepository, hostname_matches
from certlab.errors import ErrorCode

from .conftest import CLOCK, leaf_requests


def test_all_sample_leaf_errors_are_isolated(store, sample_dir):
    result = store.verify_requests(leaf_requests(sample_dir), clock=CLOCK)
    assert result["computed_count"] == 13
    by_id = {item["leaf_id"]: item["conclusion"] for item in result["results"]}
    expected = {
        "leaf-good": None,
        "leaf-wild": None,
        "leaf-expired": ErrorCode.CERT_EXPIRED,
        "leaf-badhost": ErrorCode.HOSTNAME_MISMATCH,
        "leaf-revoked-crl": ErrorCode.REVOKED,
        "leaf-revoked-ocsp": ErrorCode.REVOKED,
        "leaf-unknown": ErrorCode.REVOCATION_UNKNOWN,
        "leaf-badsig": ErrorCode.SIGNATURE_INVALID,
        "leaf-under-revoked-int": ErrorCode.REVOKED,
        "leaf-wrongku": ErrorCode.KEY_USAGE,
        "leaf-notyetvalid": ErrorCode.CERT_NOT_YET_VALID,
        "leaf-pathlen": ErrorCode.PATH_LENGTH,
        "leaf-notca": ErrorCode.BASIC_CONSTRAINTS,
    }
    assert {
        leaf_id: conclusion["error_code"] for leaf_id, conclusion in by_id.items()
    } == expected
    assert by_id["leaf-good"]["valid"] is True
    assert by_id["leaf-wild"]["hostname_match"]["wildcard"] is True


def test_revocation_states_are_distinct(store, sample_dir):
    result = store.verify_requests(
        [
            {
                "leaf_id": leaf_id,
                "hostname": hostname,
                "purpose": "serverAuth",
            }
            for leaf_id, hostname in (
                ("leaf-good", "good.example.test"),
                ("leaf-revoked-crl", "revoked-crl.example.test"),
                ("leaf-revoked-ocsp", "revoked-ocsp.example.test"),
                ("leaf-unknown", "unknown.example.test"),
            )
        ],
        clock=CLOCK,
    )
    by_id = {item["leaf_id"]: item["conclusion"] for item in result["results"]}
    good_leaf = by_id["leaf-good"]["revocation"][-1]
    crl_leaf = by_id["leaf-revoked-crl"]["revocation"][-1]
    ocsp_leaf = by_id["leaf-revoked-ocsp"]["revocation"][-1]
    unknown_leaf = by_id["leaf-unknown"]["revocation"][-1]
    assert good_leaf["status"] == "good"
    assert crl_leaf["status"] == "revoked" and crl_leaf["evidence_type"] == "crl"
    assert ocsp_leaf["status"] == "revoked" and ocsp_leaf["evidence_type"] == "ocsp"
    assert unknown_leaf["status"] == "unknown"
    assert unknown_leaf["evidence_type"] == "none"
    assert unknown_leaf["status"] != crl_leaf["status"]


def test_wildcard_matches_exactly_one_leftmost_label(store, sample_dir):
    repository = SampleRepository(sample_dir, store.load_view()[1])
    _, cert = repository.load_leaf("leaf-wild")
    assert hostname_matches(cert.cert, "api.wild.example.test")["matched"] is True
    assert hostname_matches(cert.cert, "API.WILD.EXAMPLE.TEST.")["matched"] is True
    assert hostname_matches(cert.cert, "a.b.wild.example.test")["matched"] is False
    assert hostname_matches(cert.cert, "wild.example.test")["matched"] is False


def test_verify_requests_are_idempotent(store, sample_dir):
    requests = leaf_requests(sample_dir)
    first = store.verify_requests(requests, clock=CLOCK)
    second = store.verify_requests(requests, clock=CLOCK)
    assert first["computed_count"] == 13
    assert first["reused_count"] == 0
    assert second["already_completed"] is True
    assert second["computed_count"] == 0
    assert second["reused_count"] == 13
    first_json = [
        json.dumps(item["conclusion"], sort_keys=True) for item in first["results"]
    ]
    second_json = [
        json.dumps(item["conclusion"], sort_keys=True) for item in second["results"]
    ]
    assert first_json == second_json


def test_chain_cli_is_byte_stable(sample_dir, tmp_path):
    from .conftest import BASE_MS, run_cli

    database = tmp_path / "stable.sqlite"
    run_cli(
        [
            "load",
            "--db",
            str(database),
            "--v1",
            str(sample_dir / "trust-v1.json"),
            "--v2",
            str(sample_dir / "trust-v2"),
            "--sample",
            str(sample_dir),
            "--clock-ms",
            str(BASE_MS),
        ]
    )
    arguments = [
        "chain",
        "--db",
        str(database),
        "--leaf",
        "leaf-good",
        "--clock-ms",
        str(BASE_MS),
    ]
    first = run_cli(arguments)
    second = run_cli(arguments)
    assert first.stdout == second.stdout
    parsed = json.loads(first.stdout)
    assert parsed["chain"]["valid"] is True
    assert [item["role"] for item in parsed["chain"]["path"]] == [
        "anchor",
        "intermediate",
        "leaf",
    ]

