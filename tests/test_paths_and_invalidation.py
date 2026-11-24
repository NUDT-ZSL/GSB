from __future__ import annotations

from pathlib import Path

from certpath.engine import CertPathEngine
from tests.helpers import chain_map


def _report(sample_dir: Path, tmp_path: Path) -> dict:
    return CertPathEngine(tmp_path / "state").adjudicate(sample_dir)


def test_cross_signed_paths_are_fully_enumerated_and_stable(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    report = _report(sample_dir, tmp_path)
    chain = chain_map(report)["chain-cross"]
    assert chain["conclusion"] == "trusted"
    assert chain["candidates_total"] == 3
    sequences = [
        item["fingerprint_sequence"] for item in chain["candidates"]
    ]
    assert sequences == sorted(sequences)
    assert chain["enumeration_truncated"] is False
    assert chain["trust_generation"] == 2
    assert chain["selected_path"]["certificate_ids"][-1] == "cert-root-beta"
    assert any(
        "E_ROOT_UNTRUSTED" in item["business_codes"]
        for item in chain["candidates"]
    )
    assert any(
        "E_ROOT_REVOKED" in item["business_codes"]
        for item in chain["candidates"]
    )
    assert len(chain["remaining_available_paths"]) == 1


def test_path_length_constraint_rejects_a_structural_candidate(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    chain = chain_map(_report(sample_dir, tmp_path))["chain-strict"]
    assert chain["conclusion"] == "untrusted"
    assert chain["selected_path"] is None
    assert chain["candidates_total"] == 1
    candidate = chain["candidates"][0]
    assert candidate["intermediate_count"] == 1
    assert candidate["business_codes"] == ["E_PATH_LENGTH"]
    assert "E_PATH_LENGTH" in chain["business_codes"]


def test_truncation_does_not_hide_a_trusted_candidate(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    chain = chain_map(_report(sample_dir, tmp_path))["chain-limited"]
    assert chain["enumeration_truncated"] is True
    assert chain["candidates_total"] == 3
    assert len(chain["candidates"]) == 2
    assert chain["truncated_count"] == 1
    assert chain["truncated_has_valid"] is True
    assert chain["conclusion"] == "trusted"
    assert chain["selected_path"]["certificate_ids"][-1] == "cert-root-beta"
    assert "W_PATH_ENUM_TRUNCATED" in chain["business_codes"]


def test_partial_trust_is_reported_for_pending_root_constraint(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    chain = chain_map(_report(sample_dir, tmp_path))["chain-partial"]
    assert chain["conclusion"] == "partial"
    assert chain["selected_path"]["pending_confirmation"] is True
    assert chain["selected_path"]["valid"] is True
    assert chain["trust_generation"] == 2


def test_issuer_break_missing_and_root_untrusted_have_distinct_codes(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    chains = chain_map(_report(sample_dir, tmp_path))
    assert "E_ISSUER_BREAK" in chains["chain-issuer-break"]["business_codes"]
    assert chains["chain-issuer-break"]["conclusion"] == "untrusted"
    assert (
        "E_MISSING_INTERMEDIATE"
        in chains["chain-missing-intermediate"]["business_codes"]
    )
    assert chains["chain-missing-intermediate"]["candidates_total"] == 0
    assert "E_ROOT_UNTRUSTED" in chains["chain-root-untrusted"]["business_codes"]
    assert "E_ROOT_REVOKED" in chains["chain-root-untrusted"]["business_codes"]
    assert "E_SIGNATURE_MISMATCH" in chains["chain-bad-signature"]["business_codes"]


def test_revocation_and_expiry_propagation_distinguish_roles(
    sample_dir: Path,
    tmp_path: Path,
) -> None:
    report = _report(sample_dir, tmp_path)
    chains = chain_map(report)
    assert "E_LEAF_REVOKED" in chains["chain-revoked-leaf"]["business_codes"]
    assert (
        "E_INTERMEDIATE_REVOKED"
        in chains["chain-revoked-intermediate"]["business_codes"]
    )
    assert "E_CERT_EXPIRED" in chains["chain-expired-leaf"]["business_codes"]
    propagation = {
        (item["code"], item["certificate_id"]): item
        for item in report["invalidation_propagation"]
    }
    leaf = propagation[("E_LEAF_REVOKED", "cert-leaf-revoked")]
    assert leaf["role"] == "leaf"
    assert "chain-revoked-leaf" in leaf["affected_chains"]
    intermediate = propagation[
        ("E_INTERMEDIATE_REVOKED", "cert-inter-revoked")
    ]
    assert intermediate["role"] == "intermediate"
    assert "chain-revoked-intermediate" in intermediate["affected_chains"]
    expired = propagation[("E_CERT_EXPIRED", "cert-leaf-expired")]
    assert expired["kind"] == "expiry"
    assert "chain-expired-leaf" in expired["affected_chains"]
