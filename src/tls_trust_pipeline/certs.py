from __future__ import annotations

import calendar
import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import (
    dsa,
    ec,
    ed448,
    ed25519,
    padding,
    rsa,
)
from cryptography.x509.oid import ExtendedKeyUsageOID

from .canonical import canonical_fingerprint
from .errors import E_BAD_REQUEST, E_CERT_INVALID, PipelineError
from .models import Anchor, ChainRequest, ChainResult, NodeEvidence

PURPOSE_OIDS = {
    "tls-server": ExtendedKeyUsageOID.SERVER_AUTH,
    "tls-client": ExtendedKeyUsageOID.CLIENT_AUTH,
}
PURPOSE_LABELS = {
    "tls-server": "serverAuth",
    "tls-client": "clientAuth",
}


def _cert_ms(value) -> int:
    return calendar.timegm(value.utctimetuple()) * 1000 + value.microsecond // 1000


def cert_fingerprint(cert: x509.Certificate) -> str:
    return canonical_fingerprint(cert.fingerprint(hashes.SHA256()).hex())


def _safe_name(cert: x509.Certificate) -> str:
    return cert.subject.rfc4514_string()


def _key_usage_list(cert: x509.Certificate) -> list[str]:
    try:
        usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        return []
    names = []
    for name in (
        "digital_signature",
        "content_commitment",
        "key_encipherment",
        "data_encipherment",
        "key_agreement",
        "key_cert_sign",
        "crl_sign",
        "encipher_only",
        "decipher_only",
    ):
        try:
            value = getattr(usage, name)
        except ValueError:
            value = False
        if value:
            names.append(name)
    return names


def _eku_list(cert: x509.Certificate) -> list[str]:
    try:
        usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        return []
    result = []
    for oid in usage:
        if oid == ExtendedKeyUsageOID.SERVER_AUTH:
            result.append("serverAuth")
        elif oid == ExtendedKeyUsageOID.CLIENT_AUTH:
            result.append("clientAuth")
        elif oid == ExtendedKeyUsageOID.CODE_SIGNING:
            result.append("codeSigning")
        elif oid == ExtendedKeyUsageOID.EMAIL_PROTECTION:
            result.append("emailProtection")
        else:
            result.append(oid.dotted_string)
    return sorted(result)


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except x509.ExtensionNotFound:
        return False


def _path_length(cert: x509.Certificate) -> int | None:
    try:
        return cert.extensions.get_extension_for_class(x509.BasicConstraints).value.path_length
    except x509.ExtensionNotFound:
        return None


def _has_key_cert_sign(cert: x509.Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign)
    except x509.ExtensionNotFound:
        return False


def _has_digital_signature(cert: x509.Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_class(x509.KeyUsage).value.digital_signature)
    except x509.ExtensionNotFound:
        return False


def _eku_allows(cert: x509.Certificate, purpose: str) -> bool:
    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        return True
    oids = set(eku)
    if x509.ObjectIdentifier("2.5.29.37.0") in oids:
        return True
    return PURPOSE_OIDS[purpose] in oids


def _san_names(cert: x509.Certificate) -> tuple[list[str], list[str]]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return [], []
    dns_names = sorted(san.get_values_for_type(x509.DNSName))
    ip_names = sorted(str(value) for value in san.get_values_for_type(x509.IPAddress))
    return dns_names, ip_names


def _normalize_hostname(hostname: str) -> str:
    value = hostname.strip().lower()
    value = value.removesuffix(".")
    if not value or any(ch.isspace() or ch == "/" for ch in value):
        raise PipelineError(E_BAD_REQUEST, "invalid hostname", {"hostname": hostname})
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise PipelineError(
            E_BAD_REQUEST, "hostname IDNA conversion failed", {"hostname": hostname}
        ) from exc


def match_hostname(cert: x509.Certificate, hostname: str) -> dict:
    normalized = _normalize_hostname(hostname)
    dns_names, ip_names = _san_names(cert)
    result = {
        "hostname": normalized,
        "matched": False,
        "rule": "none",
        "matched_value": None,
        "san_dns": dns_names,
        "san_ip": ip_names,
        "reason": None,
    }
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        address = None
    if address is not None:
        candidate = str(address)
        if candidate in ip_names:
            result.update(matched=True, rule="san-ip", matched_value=candidate)
            return result
        result["reason"] = "E_HOSTNAME_MISMATCH"
        return result

    for candidate in dns_names:
        try:
            candidate_normalized = (
                candidate.lower().rstrip(".").encode("idna").decode("ascii")
            )
        except UnicodeError:
            continue
        if candidate_normalized == normalized:
            result.update(matched=True, rule="exact", matched_value=candidate)
            return result
        if candidate_normalized.startswith("*."):
            suffix = candidate_normalized[2:]
            if not suffix or "*" in suffix:
                continue
            left, separator, rest = normalized.partition(".")
            if separator and left and rest == suffix:
                result.update(matched=True, rule="wildcard", matched_value=candidate)
                return result
    result["reason"] = "E_HOSTNAME_MISMATCH"
    return result


def verify_signature(cert: x509.Certificate, issuer: x509.Certificate) -> bool:
    public_key = issuer.public_key()
    try:
        if isinstance(public_key, rsa.RSAPublicKey):
            signature_padding = cert.signature_algorithm_parameters
            if not isinstance(signature_padding, padding.PSS):
                signature_padding = padding.PKCS1v15()
            public_key.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                signature_padding,
                cert.signature_hash_algorithm,
            )
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                ec.ECDSA(cert.signature_hash_algorithm),
            )
        elif isinstance(public_key, dsa.DSAPublicKey):
            public_key.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                cert.signature_hash_algorithm,
            )
        elif isinstance(public_key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
            public_key.verify(cert.signature, cert.tbs_certificate_bytes)
        else:
            return False
    except (InvalidSignature, UnsupportedAlgorithm, ValueError):
        return False
    return True


@dataclass(frozen=True)
class CertPool:
    certs_by_fp: dict[str, x509.Certificate]
    certs_by_file: dict[str, x509.Certificate]

    @classmethod
    def load(cls, cert_dir: Path) -> CertPool:
        if not cert_dir.is_dir():
            raise PipelineError(E_CERT_INVALID, "certificate directory does not exist", {"path": str(cert_dir)})
        by_fp: dict[str, x509.Certificate] = {}
        by_file: dict[str, x509.Certificate] = {}
        for path in sorted(cert_dir.glob("*.pem")):
            try:
                cert = x509.load_pem_x509_certificate(path.read_bytes())
            except ValueError as exc:
                raise PipelineError(
                    E_CERT_INVALID, "invalid PEM certificate", {"file": path.name}
                ) from exc
            fingerprint = cert_fingerprint(cert)
            existing = by_fp.get(fingerprint)
            if existing is not None and existing.public_bytes(serialization.Encoding.DER) != cert.public_bytes(
                serialization.Encoding.DER
            ):
                raise PipelineError(
                    E_CERT_INVALID, "SHA-256 collision or duplicate fingerprint", {"file": path.name}
                )
            by_fp[fingerprint] = cert
            by_file[path.name] = cert
        if not by_fp:
            raise PipelineError(E_CERT_INVALID, "certificate directory is empty", {"path": str(cert_dir)})
        return cls(certs_by_fp=by_fp, certs_by_file=by_file)

    def get_file(self, filename: str) -> x509.Certificate:
        if Path(filename).name != filename:
            raise PipelineError(E_BAD_REQUEST, "certificate file must be a basename", {"file": filename})
        try:
            return self.certs_by_file[filename]
        except KeyError as exc:
            raise PipelineError(E_CERT_INVALID, "certificate file not found", {"file": filename}) from exc

    def get_fp(self, fingerprint: str) -> x509.Certificate | None:
        try:
            return self.certs_by_fp[canonical_fingerprint(fingerprint)]
        except (TypeError, ValueError):
            return None


def _node_evidence(
    cert: x509.Certificate,
    position: int,
    is_anchor: bool,
    is_leaf: bool,
) -> NodeEvidence:
    return NodeEvidence(
        position=position,
        subject=_safe_name(cert),
        issuer=cert.issuer.rfc4514_string(),
        serial_hex=format(cert.serial_number, "x"),
        fingerprint=cert_fingerprint(cert),
        not_before_ms=_cert_ms(cert.not_valid_before_utc),
        not_after_ms=_cert_ms(cert.not_valid_after_utc),
        is_anchor=is_anchor,
        is_leaf=is_leaf,
        basic_constraints_ca=_is_ca(cert),
        path_length=_path_length(cert),
        key_usage=_key_usage_list(cert),
        extended_key_usage=_eku_list(cert),
        checks={},
    )


def _fail(node: NodeEvidence, code: str) -> None:
    if code not in node.failure_codes:
        node.failure_codes.append(code)


def _validate_path(
    path: list[x509.Certificate],
    anchor_by_fp: dict[str, Anchor],
    request: ChainRequest,
    hostname_evidence: dict,
) -> tuple[list[NodeEvidence], list[str]]:
    nodes = [
        _node_evidence(
            cert,
            position=index,
            is_anchor=index == len(path) - 1,
            is_leaf=index == 0,
        )
        for index, cert in enumerate(path)
    ]
    failures: list[str] = []
    now = request.logical_clock_ms

    for index, (cert, node) in enumerate(zip(path, nodes)):
        node.checks["issuer_subject_match"] = index == 0 or cert.issuer == path[index - 1].issuer
        if index > 0:
            signer = path[index]
            signed = path[index - 1]
            node.checks["issuer_subject_match"] = signed.issuer == signer.subject
            node.checks["signature_valid"] = verify_signature(signed, signer)
        else:
            node.checks["signature_valid"] = True
        valid_from = _cert_ms(cert.not_valid_before_utc)
        valid_to = _cert_ms(cert.not_valid_after_utc)
        node.checks["not_before_ok"] = valid_from <= now
        node.checks["not_after_ok"] = now <= valid_to
        node.checks["certificate_time_valid"] = node.checks["not_before_ok"] and node.checks[
            "not_after_ok"
        ]
        if not node.checks["not_before_ok"]:
            _fail(node, "E_NOT_YET_VALID")
            failures.append("E_NOT_YET_VALID")
        if not node.checks["not_after_ok"]:
            _fail(node, "E_EXPIRED")
            failures.append("E_EXPIRED")
        if index > 0 and not node.checks["signature_valid"]:
            _fail(node, "E_SIGNATURE_INVALID")
            failures.append("E_SIGNATURE_INVALID")
        if index > 0 and not node.checks["issuer_subject_match"]:
            _fail(node, "E_ISSUER_MISMATCH")
            failures.append("E_ISSUER_MISMATCH")

    leaf = path[0]
    leaf_node = nodes[0]
    leaf_node.checks["leaf_is_not_ca"] = not _is_ca(leaf)
    if _is_ca(leaf):
        _fail(leaf_node, "E_LEAF_IS_CA")
        failures.append("E_LEAF_IS_CA")
    leaf_node.checks["key_usage_allows_signature"] = _has_digital_signature(leaf)
    if not leaf_node.checks["key_usage_allows_signature"]:
        _fail(leaf_node, "E_KEY_USAGE")
        failures.append("E_KEY_USAGE")
    leaf_node.checks["eku_allows_purpose"] = _eku_allows(leaf, request.purpose)
    if not leaf_node.checks["eku_allows_purpose"]:
        _fail(leaf_node, "E_EXTENDED_KEY_USAGE")
        failures.append("E_EXTENDED_KEY_USAGE")
    leaf_node.checks["hostname_matches"] = bool(hostname_evidence["matched"])
    if not hostname_evidence["matched"]:
        _fail(leaf_node, "E_HOSTNAME_MISMATCH")
        failures.append("E_HOSTNAME_MISMATCH")

    for index in range(1, len(path)):
        cert = path[index]
        node = nodes[index]
        node.checks["issuer_is_ca"] = _is_ca(cert)
        node.checks["key_cert_sign"] = _has_key_cert_sign(cert)
        if not node.checks["issuer_is_ca"]:
            _fail(node, "E_ISSUER_NOT_CA")
            failures.append("E_ISSUER_NOT_CA")
        if not node.checks["key_cert_sign"]:
            _fail(node, "E_KEY_USAGE")
            failures.append("E_KEY_USAGE")
        below_ca_count = sum(1 for lower in path[1:index] if _is_ca(lower))
        declared_path_length = _path_length(cert)
        within_path_length = declared_path_length is None or below_ca_count <= declared_path_length
        node.checks["path_length_ok"] = within_path_length
        node.checks["path_length_below_ca_count"] = below_ca_count
        if not within_path_length:
            _fail(node, "E_PATH_LENGTH")
            failures.append("E_PATH_LENGTH")
        if index < len(path) - 1:
            node.checks["eku_allows_purpose"] = _eku_allows(cert, request.purpose)
            if not node.checks["eku_allows_purpose"]:
                _fail(node, "E_EXTENDED_KEY_USAGE")
                failures.append("E_EXTENDED_KEY_USAGE")

    anchor_fp = nodes[-1].fingerprint
    anchor = anchor_by_fp.get(anchor_fp)
    anchor_node = nodes[-1]
    if anchor is None:
        _fail(anchor_node, "E_UNTRUSTED_ANCHOR")
        failures.append("E_UNTRUSTED_ANCHOR")
    else:
        anchor_node.checks["anchor_not_tombstoned"] = not anchor.revoked
        anchor_node.checks["anchor_not_before_ok"] = anchor.not_before_ms <= now
        anchor_node.checks["anchor_not_after_ok"] = now <= anchor.not_after_ms
        anchor_node.checks["anchor_time_valid"] = (
            anchor_node.checks["anchor_not_before_ok"]
            and anchor_node.checks["anchor_not_after_ok"]
        )
        if anchor.revoked:
            _fail(anchor_node, "E_ANCHOR_TOMBSTONE")
            failures.append("E_ANCHOR_TOMBSTONE")
        if not anchor_node.checks["anchor_not_before_ok"]:
            _fail(anchor_node, "E_ANCHOR_NOT_YET_VALID")
            failures.append("E_ANCHOR_NOT_YET_VALID")
        if not anchor_node.checks["anchor_not_after_ok"]:
            _fail(anchor_node, "E_ANCHOR_EXPIRED")
            failures.append("E_ANCHOR_EXPIRED")
        if len(path) >= 2 and _is_ca(path[-2]):
            anchor_node.checks["anchor_can_issue_intermediates"] = anchor.can_issue_intermediates
            if not anchor.can_issue_intermediates:
                _fail(anchor_node, "E_ANCHOR_CANNOT_ISSUE_INTERMEDIATE")
                failures.append("E_ANCHOR_CANNOT_ISSUE_INTERMEDIATE")
    return nodes, failures


def _discover_paths(
    leaf: x509.Certificate,
    pool: CertPool,
    anchor_fps: set[str],
    max_depth: int,
) -> list[list[x509.Certificate]]:
    paths: list[list[x509.Certificate]] = []
    stack: list[tuple[x509.Certificate, list[x509.Certificate]]] = [(leaf, [leaf])]
    while stack:
        current, path = stack.pop()
        current_fp = cert_fingerprint(current)
        if current_fp in anchor_fps:
            paths.append(path)
            continue
        if len(path) >= max_depth:
            continue
        seen = {cert_fingerprint(item) for item in path}
        candidates = [
            cert
            for fingerprint, cert in pool.certs_by_fp.items()
            if fingerprint not in seen and cert.subject == current.issuer
        ]
        candidates.sort(key=cert_fingerprint, reverse=True)
        for candidate in candidates:
            stack.append((candidate, path + [candidate]))
    paths.sort(
        key=lambda path: (
            len(path),
            tuple(cert_fingerprint(cert) for cert in path),
        )
    )
    return paths


def build_chain(
    request: ChainRequest,
    pool: CertPool,
    anchors: Iterable[Anchor],
    max_depth: int = 8,
) -> ChainResult:
    if request.purpose not in PURPOSE_OIDS:
        raise PipelineError(
            E_BAD_REQUEST,
            "unsupported purpose",
            {"purpose": request.purpose, "supported": sorted(PURPOSE_OIDS)},
        )
    leaf = pool.get_file(request.leaf_file)
    hostname_evidence = match_hostname(leaf, request.hostname)
    anchor_list = sorted(anchors, key=lambda item: item.alias)
    anchor_by_fp = {anchor.fingerprint: anchor for anchor in anchor_list}
    paths = _discover_paths(leaf, pool, set(anchor_by_fp), max_depth=max_depth)
    if not paths:
        return ChainResult(
            request=request,
            status="INVALID",
            reason_code="E_CHAIN_NOT_FOUND",
            reason="no issuer path from leaf to a snapshot trust anchor",
            chain_fingerprints=[],
            nodes=[],
            anchor_alias=None,
            anchor_fingerprint=None,
            hostname_check=hostname_evidence,
        )

    failures_by_path: list[tuple[list[str], list[NodeEvidence], list[x509.Certificate]]] = []
    for path in paths:
        nodes, failures = _validate_path(path, anchor_by_fp, request, hostname_evidence)
        if not failures:
            anchor = anchor_by_fp[nodes[-1].fingerprint]
            return ChainResult(
                request=request,
                status="VALID",
                reason_code="OK",
                reason="chain is valid at the injected logical clock",
                chain_fingerprints=[cert_fingerprint(cert) for cert in path],
                nodes=nodes,
                anchor_alias=anchor.alias,
                anchor_fingerprint=anchor.fingerprint,
                hostname_check=hostname_evidence,
            )
        failures_by_path.append((failures, nodes, path))

    failures, nodes, path = failures_by_path[0]
    anchor = anchor_by_fp.get(nodes[-1].fingerprint)
    return ChainResult(
        request=request,
        status="INVALID",
        reason_code=failures[0],
        reason="chain validation failed",
        chain_fingerprints=[cert_fingerprint(cert) for cert in path],
        nodes=nodes,
        anchor_alias=anchor.alias if anchor else None,
        anchor_fingerprint=anchor.fingerprint if anchor else None,
        hostname_check=hostname_evidence,
    )


def chain_result_to_dict(result: ChainResult) -> dict:
    return {
        "request_id": result.request.request_id,
        "hostname": result.request.hostname,
        "purpose": result.request.purpose,
        "logical_clock_ms": result.request.logical_clock_ms,
        "require_revocation": result.request.require_revocation,
        "status": result.status,
        "reason_code": result.reason_code,
        "reason": result.reason,
        "anchor_alias": result.anchor_alias,
        "anchor_fingerprint": result.anchor_fingerprint,
        "hostname_check": result.hostname_check,
        "chain": [
            {
                "position": node.position,
                "subject": node.subject,
                "issuer": node.issuer,
                "serial_hex": node.serial_hex,
                "fingerprint": node.fingerprint,
                "not_before_ms": node.not_before_ms,
                "not_after_ms": node.not_after_ms,
                "is_leaf": node.is_leaf,
                "is_anchor": node.is_anchor,
                "basic_constraints_ca": node.basic_constraints_ca,
                "path_length": node.path_length,
                "key_usage": node.key_usage,
                "extended_key_usage": node.extended_key_usage,
                "checks": dict(sorted(node.checks.items())),
                "failure_codes": node.failure_codes,
            }
            for node in result.nodes
        ],
    }

