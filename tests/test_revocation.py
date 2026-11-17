"""Revocation & expiry propagation driven solely by snapshot-declared
observation time."""

import os

from certtrust import errors
from certtrust.anchors import parse_snapshot_files
from certtrust.validation import PARTIAL, revocation_propagation, validate_claim

from tests.conftest import load_basic


def _codes(conclusion):
    return [e.code for e in conclusion.errors]


def test_intermediate_revocation_distinguished_from_leaf(samples):
    certs, claims, snaps, _ = load_basic(samples)
    inter = validate_claim(claims["claim-rev-inter"], certs, snaps[2])
    assert inter.verdict == PARTIAL
    err = inter.errors[0]
    assert err.code == errors.E_REVOKED_INTERMEDIATE
    assert err.position == 1
    leaf = validate_claim(claims["claim-leaf-rev"], certs, snaps[3])
    assert leaf.verdict == PARTIAL
    err = leaf.errors[0]
    assert err.code == errors.E_REVOKED_LEAF
    assert err.position == 0


def test_revocation_respects_observation_time(samples):
    certs, claims, snaps, _ = load_basic(samples)
    early = validate_claim(claims["claim-rev-inter"], certs, snaps[1])
    assert early.verdict == "TRUSTED"
    late = validate_claim(claims["claim-rev-inter"], certs, snaps[2])
    assert late.verdict == PARTIAL


def test_expiry_uses_snapshot_observation_time(samples):
    certs, claims, snaps, _ = load_basic(samples)
    valid = validate_claim(claims["claim-exp"], certs, snaps[1])
    assert valid.verdict == "TRUSTED"
    expired = validate_claim(claims["claim-exp"], certs, snaps[2])
    assert expired.verdict == PARTIAL
    err = expired.errors[0]
    assert err.code == errors.E_EXPIRED
    assert err.position == 0


def test_missing_observation_time_is_a_business_error(samples):
    certs, claims, _, _ = load_basic(samples)
    path = os.path.join(samples["dirs"]["unknown_policy"],
                        "snap_no_obs_time.json")
    snaps, rejects = parse_snapshot_files([path])
    assert rejects == []
    concl = validate_claim(claims["claim-good-a"], certs, snaps[0])
    assert errors.E_SNAPSHOT_OBS_TIME in _codes(concl)


def test_propagation_report_lists_affected_claims(samples):
    certs, claims, snaps, _ = load_basic(samples)
    conclusions = [validate_claim(claims[c], certs, snaps[3])
                   for c in sorted(claims)]
    propagation = {e["cert_id"]: e for e in revocation_propagation(conclusions)}
    assert propagation["inter-rev"]["propagates_to"] == ["claim-rev-inter"]
    assert propagation["inter-rev"]["kinds"] == [errors.E_REVOKED_INTERMEDIATE]
    assert propagation["leaf-3"]["propagates_to"] == ["claim-leaf-rev"]
    assert propagation["leaf-3"]["kinds"] == [errors.E_REVOKED_LEAF]
    assert propagation["leaf-exp"]["kinds"] == [errors.E_EXPIRED]
