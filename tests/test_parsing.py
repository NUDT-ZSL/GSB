"""Parsing & normalization: reject records carry file index + error code,
and valid entries continue to be processed."""

import os

from certtrust import errors
from certtrust.parsing import parse_cert_files, parse_chain_files

from tests.conftest import load_basic


def _codes(rejects):
    return [r["code"] for r in rejects]


def test_basic_samples_parse_cleanly(samples):
    certs, claims, snaps, rejects = load_basic(samples)
    assert rejects == []
    assert len(certs) == 12
    assert len(claims) == 8
    assert set(snaps) == {1, 2, 3, 4}


def test_invalid_cert_entries_rejected_with_codes(samples):
    path = os.path.join(samples["dirs"]["invalid"], "certs_bad.json")
    certs, rejects = parse_cert_files([path])
    codes = _codes(rejects)
    assert errors.E_CERT_DUP_ID in codes
    assert errors.E_CERT_FINGERPRINT in codes
    assert errors.E_CERT_VALIDITY in codes
    assert errors.E_CERT_ISSUER_MISSING in codes
    assert errors.E_CERT_ID_FORMAT in codes  # entry without issuer field
    # the one valid entry survives; the orphan is dropped with its issuer
    assert set(certs) == {"dup-cert", "ok-cert"}
    for reject in rejects:
        assert reject["file_index"] == 0
        assert reject["entry_index"] is not None


def test_file_index_tracks_position_across_files(samples):
    bad = os.path.join(samples["dirs"]["invalid"], "certs_bad.json")
    good = os.path.join(samples["dirs"]["basic"], "certs.json")
    certs, rejects = parse_cert_files([good, bad])
    assert certs  # good file fully accepted
    assert all(r["file_index"] == 1 for r in rejects)


def test_invalid_chain_entries_rejected_with_positions(samples):
    invalid = samples["dirs"]["invalid"]
    certs, _ = parse_cert_files([os.path.join(invalid, "certs_bad.json")])
    claims, rejects = parse_chain_files(
        [os.path.join(invalid, "chains_bad.json")], certs)
    codes = _codes(rejects)
    assert errors.E_CHAIN_EMPTY in codes
    assert errors.E_CHAIN_DUP in codes
    assert errors.E_CHAIN_REF_MISSING in codes
    assert errors.E_CHAIN_SCHEMA in codes
    dup = next(r for r in rejects if r["code"] == errors.E_CHAIN_DUP)
    assert dup["position"] == 1
    missing = next(r for r in rejects if r["code"] == errors.E_CHAIN_REF_MISSING)
    assert missing["position"] == 1
    assert set(claims) == {"ok-chain"}


def test_fingerprint_normalization():
    from certtrust.parsing import normalize_cert
    raw = {
        "id": "x",
        "issuer": "x",
        "fingerprint": "A" * 64,
        "not_before_ms": 0,
        "not_after_ms": 1,
    }
    cert, reject = normalize_cert(raw, 0, 0)
    assert reject is None
    assert cert.fingerprint == "sha256:" + "a" * 64
