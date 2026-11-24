"""Deterministic local proof dispatch for TOTP, WEBAUTHN and PUSH fixtures."""

from __future__ import annotations

from typing import Any

from .jsonio import sha256_text


def build_proof_digest(factor: dict[str, Any], challenge: str, **extra: Any) -> str:
    material = {
        "factor_id": factor["factor_id"],
        "factor_type": factor["factor_type"],
        "secret_placeholder_digest": factor["secret_placeholder_digest"],
        "challenge": challenge,
        "extra": extra,
    }
    return f"proof-sha256:{sha256_text(material)}"


def verify_submitted_proof(factor: dict[str, Any], submitted: dict[str, Any]) -> tuple[bool, str | None]:
    factor_type = factor.get("factor_type")
    challenge = submitted.get("challenge")
    proof_digest = submitted.get("proof_digest")
    if not isinstance(challenge, str) or not challenge:
        return False, "missing_challenge"
    if not isinstance(proof_digest, str) or not proof_digest.startswith("proof-sha256:"):
        return False, "missing_proof_digest"

    if factor_type == "TOTP":
        code = submitted.get("code")
        time_step = submitted.get("time_step")
        if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
            return False, "invalid_totp_code"
        if not isinstance(time_step, int) or isinstance(time_step, bool) or time_step < 0:
            return False, "invalid_time_step"
        expected = build_proof_digest(factor, challenge, code=code, time_step=time_step)
    elif factor_type == "WEBAUTHN":
        client_data_digest = submitted.get("client_data_digest")
        signature_placeholder = submitted.get("signature_placeholder")
        if not isinstance(client_data_digest, str) or not client_data_digest:
            return False, "missing_client_data_digest"
        if not isinstance(signature_placeholder, str) or not signature_placeholder:
            return False, "missing_signature_placeholder"
        expected = build_proof_digest(
            factor,
            challenge,
            client_data_digest=client_data_digest,
            signature_placeholder=signature_placeholder,
        )
    elif factor_type == "PUSH":
        response = submitted.get("push_response")
        responded_at_ms = submitted.get("responded_at_ms")
        if response != "APPROVED":
            return False, "push_not_approved"
        if not isinstance(responded_at_ms, int) or isinstance(responded_at_ms, bool) or responded_at_ms < 0:
            return False, "invalid_responded_at_ms"
        expected = build_proof_digest(
            factor,
            challenge,
            push_response=response,
            responded_at_ms=responded_at_ms,
        )
    else:
        return False, "unsupported_factor_type"

    return (proof_digest == expected, None if proof_digest == expected else "proof_digest_mismatch")
