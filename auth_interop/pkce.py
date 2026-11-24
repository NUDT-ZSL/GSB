from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Optional


def s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_pkce(
    method: Optional[str],
    challenge: Optional[str],
    verifier: Optional[str],
) -> tuple[bool, str]:
    """Return ``(ok, reason_code)``.

    A missing verifier is reported as a PKCE downgrade because a declared
    challenge is present while the exchange side has no proof.  A present but
    invalid proof is a normal PKCE mismatch.
    """

    if not challenge:
        return True, "not_required"
    if verifier is None or verifier == "":
        return False, "missing_verifier"

    normalized_method = (method or "").strip().upper()
    if normalized_method == "S256":
        expected = s256_challenge(verifier)
        ok = hmac.compare_digest(expected, challenge)
        return ok, "match" if ok else "digest_mismatch"
    if normalized_method == "PLAIN":
        ok = hmac.compare_digest(verifier, challenge)
        return ok, "match" if ok else "plain_mismatch"
    return False, "unsupported_method"
