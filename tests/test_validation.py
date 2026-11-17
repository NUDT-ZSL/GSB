"""Path validation: verdict grading, per-position error codes, and the
binding of every conclusion to its anchor version."""

from certtrust import errors
from certtrust.validation import PARTIAL, TRUSTED, UNTRUSTED, validate_claim

from tests.conftest import load_basic


def _by_id(conclusions):
    return {c.claim_id: c for c in conclusions}


def _codes(conclusion):
    return [e.code for e in conclusion.errors]


def test_trusted_and_untrusted_root(samples, workdir):
    certs, claims, snaps, _ = load_basic(samples)
    results = _by_id([validate_claim(claims[c], certs, snaps[1])
                      for c in sorted(claims)])
    assert results["claim-good-a"].verdict == TRUSTED
    untrusted = results["claim-good-b"]
    assert untrusted.verdict == UNTRUSTED
    assert errors.E_ROOT_UNTRUSTED in _codes(untrusted)
    assert untrusted.errors[0].position == 2
    assert results["claim-untrusted-root"].verdict == UNTRUSTED
    assert errors.E_ROOT_UNTRUSTED in _codes(results["claim-untrusted-root"])


def test_missing_intermediate_and_root_mismatch(samples):
    certs, claims, snaps, _ = load_basic(samples)
    missing = validate_claim(claims["claim-missing-inter"], certs, snaps[1])
    assert missing.verdict == UNTRUSTED
    err = missing.errors[0]
    assert err.code == errors.E_CHAIN_INTERMEDIATE_MISSING
    assert err.position == 0
    # under v2 root-b is trusted, so a chain stopping at inter-2 mismatches
    mismatch = validate_claim(claims["claim-root-mismatch"], certs, snaps[2])
    assert mismatch.verdict == UNTRUSTED
    err = mismatch.errors[0]
    assert err.code == errors.E_ROOT_MISMATCH
    assert err.position == 1


def test_conclusion_records_anchor_identity(samples):
    certs, claims, snaps, _ = load_basic(samples)
    concl = validate_claim(claims["claim-good-a"], certs, snaps[2])
    assert concl.anchor_version == 2
    assert concl.anchor_key == snaps[2].stable_key
    assert concl.policy_version == "1.0.0"
    other = validate_claim(claims["claim-good-a"], certs, snaps[3])
    assert other.anchor_key != concl.anchor_key


def test_explanation_is_human_readable(samples):
    certs, claims, snaps, _ = load_basic(samples)
    concl = validate_claim(claims["claim-good-a"], certs, snaps[1])
    assert "leaf-1" in concl.explanation
    assert "root-a" in concl.explanation
    assert "anchor v1" in concl.explanation
    assert TRUSTED in concl.explanation


def test_root_midchain_error(samples):
    certs, claims, snaps, _ = load_basic(samples)
    from certtrust.models import ChainClaim
    claim = ChainClaim("midchain", ("leaf-1", "inter-1", "root-a", "leaf-1"))
    concl = validate_claim(claim, certs, snaps[1])
    assert concl.verdict == UNTRUSTED
    assert errors.E_ROOT_MIDCHAIN in _codes(concl)


def test_partial_verdict_only_for_invalidation(samples):
    certs, claims, snaps, _ = load_basic(samples)
    concl = validate_claim(claims["claim-rev-inter"], certs, snaps[2])
    assert concl.verdict == PARTIAL
    assert errors.E_REVOKED_INTERMEDIATE in _codes(concl)
