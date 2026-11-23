"""Certificate chain construction and traceable validation.

Chains are built bottom-up by matching each certificate's issuer name to a
subject among the trust view and on-disk CA certificates.  Every validation
step (signature, basic constraints/path length, key usage, validity window,
hostname) is recorded as a traceable node.  Validity uses only the injected
logical clock milliseconds; the wall clock is never consulted.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec, rsa, padding
from cryptography.x509.oid import ExtendedKeyUsageOID

from .errors import DiagError
from .merge import TrustView

PURPOSE_OID = {
    "serverAuth": ExtendedKeyUsageOID.SERVER_AUTH,
    "clientAuth": ExtendedKeyUsageOID.CLIENT_AUTH,
}


def _dt_to_ms(value) -> int:
    """Integer millisecond epoch conversion (no floating point)."""
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    delta = value - epoch
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


@dataclass
class TraceNode:
    stage: str
    subject: str
    ok: bool
    detail: str
    error_code: str | None = None

    def to_dict(self) -> dict:
        payload = {"stage": self.stage, "subject": self.subject, "ok": self.ok, "detail": self.detail}
        if self.error_code:
            payload["error_code"] = self.error_code
        return payload


@dataclass
class ChainResult:
    leaf_alias: str
    hostname: str
    purpose: str
    generation: int
    instant_ms: int
    valid: bool
    ordered_subjects: list[str] = field(default_factory=list)
    trace: list[TraceNode] = field(default_factory=list)
    failure_code: str | None = None
    remaining_ms_by_subject: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "leaf_alias": self.leaf_alias,
            "hostname": self.hostname,
            "purpose": self.purpose,
            "generation": self.generation,
            "instant_ms": self.instant_ms,
            "valid": self.valid,
            "failure_code": self.failure_code,
            "ordered_subjects": self.ordered_subjects,
            "trace": [node.to_dict() for node in self.trace],
            "remaining_ms": self.remaining_ms_by_subject,
        }


def _cn(cert: x509.Certificate) -> str:
    try:
        return cert.subject.rfc4514_string()
    except Exception:
        return "<unparseable subject>"


def _verify_signature(child: x509.Certificate, issuer: x509.Certificate) -> None:
    public_key = issuer.public_key()
    if isinstance(public_key, rsa.RSAPublicKey):
        public_key.verify(child.signature, child.tbs_certificate_bytes, padding.PKCS1v15(), child.signature_hash_algorithm)
    elif isinstance(public_key, ec.EllipticCurvePublicKey):
        public_key.verify(child.signature, child.tbs_certificate_bytes, ec.ECDSA(child.signature_hash_algorithm))
    else:
        raise TypeError("unsupported issuer key type")


def _basic_constraints(cert: x509.Certificate) -> x509.BasicConstraints:
    try:
        return cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        return x509.BasicConstraints(ca=False, path_length=None)


def _is_ca(cert: x509.Certificate) -> bool:
    return bool(_basic_constraints(cert).ca)


def wildcard_match(hostname: str, pattern: str) -> bool:
    """RFC 6125 style wildcard: a single leftmost ``*`` covers one label."""
    hostname = hostname.lower().rstrip(".")
    pattern = pattern.lower().rstrip(".")
    if "*" not in pattern:
        return hostname == pattern
    if pattern.count("*") != 1 or not pattern.startswith("*."):
        return False
    suffix = pattern[2:]
    if not suffix or "." not in suffix:
        return False
    if not hostname.endswith("." + suffix):
        return False
    leftmost = hostname[: -(len(suffix) + 1)]
    return bool(leftmost) and "*" not in leftmost and "." not in leftmost


def _verify_hostname(cert: x509.Certificate, hostname: str) -> bool:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = san.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        names = []
    if names:
        return any(wildcard_match(hostname, name) for name in names)
    # Fallback to CN only when no SAN is present.
    try:
        common = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
        return bool(common) and wildcard_match(hostname, common[0].value)
    except Exception:
        return False


def build_chain(leaf: x509.Certificate, view: TrustView, ca_certs: list[x509.Certificate]) -> list[x509.Certificate]:
    """Return leaf -> intermediate(s) -> trust anchor, bottom-up."""
    anchors_by_subject: dict[x509.Name, x509.Certificate] = {}
    trusted_subjects: set[x509.Name] = set()
    for anchor in view.enabled_anchors():
        anchors_by_subject.setdefault(anchor.cert.subject, anchor.cert)
        trusted_subjects.add(anchor.cert.subject)
    for ca in ca_certs:
        anchors_by_subject.setdefault(ca.subject, ca)

    chain = [leaf]
    current = leaf
    for _ in range(8):
        if current.issuer == current.subject:
            # Self-signed terminus: it must be a trusted CA anchor.
            if current.subject in trusted_subjects and _is_ca(current):
                return chain
            raise DiagError("E4001", f"self-signed cert not trusted: {_cn(current)}")
        issuer = anchors_by_subject.get(current.issuer)
        if issuer is None:
            raise DiagError("E4001", f"no issuer cert found for {_cn(current)}")
        chain.append(issuer)
        current = issuer
    raise DiagError("E4001", "chain construction exceeded depth limit")


def _validate_usage(cert: x509.Certificate, purpose: str, is_ca: bool) -> str | None:
    try:
        ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
        if is_ca and not (ku.key_cert_sign and ku.crl_sign):
            return "CA certificate lacks keyCertSign/cRLSign"
        if not is_ca and not ku.digital_signature:
            return "leaf certificate lacks digitalSignature"
    except x509.ExtensionNotFound:
        return "KeyUsage extension missing"
    if not is_ca:
        try:
            eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            oid = PURPOSE_OID.get(purpose)
            if oid is None or oid not in eku:
                return f"EKU does not permit {purpose}"
        except x509.ExtensionNotFound:
            return "ExtendedKeyUsage extension missing"
    return None


def validate_chain(
    leaf: x509.Certificate,
    hostname: str,
    purpose: str,
    generation: int,
    instant_ms: int,
    view: TrustView,
    ca_certs: list[x509.Certificate],
) -> ChainResult:
    result = ChainResult(
        leaf_alias=_cn(leaf),
        hostname=hostname,
        purpose=purpose,
        generation=generation,
        instant_ms=instant_ms,
        valid=True,
    )

    def fail(code: str, detail: str, subject: str) -> None:
        result.valid = False
        result.failure_code = code
        result.trace.append(TraceNode("validation", subject, False, detail, code))

    try:
        chain = build_chain(leaf, view, ca_certs)
    except DiagError as exc:
        fail(exc.code, exc.detail, _cn(leaf))
        return result

    result.ordered_subjects = [_cn(cert) for cert in chain]

    # Signatures bottom-up.
    for index in range(len(chain) - 1):
        child, issuer = chain[index], chain[index + 1]
        try:
            _verify_signature(child, issuer)
            result.trace.append(TraceNode("signature", _cn(child), True, f"verified by {_cn(issuer)}"))
        except Exception as exc:  # noqa: BLE001
            fail("E4002", f"signature invalid: {exc}", _cn(child))
            return result

    # Basic constraints, path length, usage and validity top-down.
    anchor = chain[-1]
    # Position counted from the leaf: leaf=0, first intermediate=1, ...
    for position_from_leaf, cert in enumerate(chain):
        subject = _cn(cert)
        is_leaf = position_from_leaf == 0
        is_ca = not is_leaf
        constraints = _basic_constraints(cert)
        if is_leaf and constraints.ca:
            fail("E4003", "leaf certificate must not assert CA=true", subject)
            return result
        if is_ca and not constraints.ca:
            fail("E4003", "issuing certificate is not a CA", subject)
            return result
        if is_ca and constraints.path_length is not None:
            # Number of non-leaf certs that sit below this one in the chain.
            intermediates_below = position_from_leaf - 1
            if constraints.path_length < intermediates_below:
                fail("E4003", f"path_len {constraints.path_length} < required {intermediates_below}", subject)
                return result
        result.trace.append(TraceNode("basic_constraints", subject, True, f"ca={constraints.ca}, path_len={constraints.path_length}"))

        usage_problem = _validate_usage(cert, purpose, is_ca)
        if usage_problem:
            fail("E4004", usage_problem, subject)
            return result
        result.trace.append(TraceNode("key_usage", subject, True, "CA usage" if is_ca else f"EKU {purpose} permitted"))

        not_before = _dt_to_ms(cert.not_valid_before_utc)
        not_after = _dt_to_ms(cert.not_valid_after_utc)
        remaining = not_after - instant_ms
        result.remaining_ms_by_subject[subject] = remaining
        if instant_ms < not_before or instant_ms > not_after:
            fail(
                "E4005",
                f"instant {instant_ms} outside [{not_before}, {not_after}]",
                subject,
            )
            return result
        result.trace.append(TraceNode("validity", subject, True, f"remaining_ms={remaining}"))

    # Hostname and leaf EKU purpose on the leaf only.
    if not _verify_hostname(leaf, hostname):
        fail("E4006", f"hostname {hostname} not permitted", _cn(leaf))
        return result
    result.trace.append(TraceNode("hostname", _cn(leaf), True, f"{hostname} matched SAN/CN"))

    anchor_used = next((a for a in view.enabled_anchors() if a.cert == anchor), None)
    result.trace.append(
        TraceNode(
            "trust_anchor",
            _cn(anchor),
            True,
            f"alias={anchor_used.alias if anchor_used else 'external'} generation={generation}",
        )
    )
    return result
