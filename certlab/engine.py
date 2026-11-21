from __future__ import annotations

import base64
import ipaddress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .clock import datetime_to_ms
from .errors import ErrorCode
from .snapshot import active_anchors
from .util import load_json_file, sha256_bytes, stable_json

PURPOSE_OIDS = {
    "serverAuth": ExtendedKeyUsageOID.SERVER_AUTH,
    "clientAuth": ExtendedKeyUsageOID.CLIENT_AUTH,
    "emailProtection": ExtendedKeyUsageOID.EMAIL_PROTECTION,
    "codeSigning": ExtendedKeyUsageOID.CODE_SIGNING,
    "timeStamping": ExtendedKeyUsageOID.TIME_STAMPING,
    "ocspSigning": ExtendedKeyUsageOID.OCSP_SIGNING,
}
PURPOSE_ALIASES = {
    "server": "serverAuth",
    "server_auth": "serverAuth",
    "tls-server": "serverAuth",
    "client": "clientAuth",
    "client_auth": "clientAuth",
    "tls-client": "clientAuth",
}

ERROR_PRIORITY = {
    ErrorCode.SIGNATURE_INVALID: 10,
    ErrorCode.CERT_EXPIRED: 20,
    ErrorCode.CERT_NOT_YET_VALID: 21,
    ErrorCode.BASIC_CONSTRAINTS: 30,
    ErrorCode.PATH_LENGTH: 31,
    ErrorCode.KEY_USAGE: 32,
    ErrorCode.ANCHOR_NOT_EFFECTIVE: 33,
    ErrorCode.HOSTNAME_MISMATCH: 40,
    ErrorCode.REVOKED: 50,
    ErrorCode.REVOCATION_UNKNOWN: 51,
    ErrorCode.NO_TRUST_PATH: 90,
}


def fingerprint(cert: x509.Certificate) -> str:
    return sha256_bytes(cert.public_bytes(serialization.Encoding.DER))


def _utc_property(cert_or_crl: Any, aware_name: str, fallback_name: str) -> datetime:
    value = getattr(cert_or_crl, aware_name, None)
    if value is None:
        value = getattr(cert_or_crl, fallback_name)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
    return value


def not_before_ms(cert: x509.Certificate) -> int:
    return datetime_to_ms(_utc_property(cert, "not_valid_before_utc", "not_valid_before"))


def not_after_ms(cert: x509.Certificate) -> int:
    return datetime_to_ms(_utc_property(cert, "not_valid_after_utc", "not_valid_after"))


@dataclass(frozen=True)
class LoadedCertificate:
    cert: x509.Certificate
    pem: str
    fingerprint_sha256: str
    source: str

    @property
    def subject(self) -> x509.Name:
        return self.cert.subject

    @property
    def issuer(self) -> x509.Name:
        return self.cert.issuer

    def summary(self, role: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "fingerprint_sha256": self.fingerprint_sha256,
            "subject": self.cert.subject.rfc4514_string(),
            "issuer": self.cert.issuer.rfc4514_string(),
            "serial_number": str(self.cert.serial_number),
            "not_before_ms": not_before_ms(self.cert),
            "not_after_ms": not_after_ms(self.cert),
        }
        if role is not None:
            result["role"] = role
        return result


def load_certificate(path: Path, *, source: str | None = None) -> LoadedCertificate:
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    return LoadedCertificate(
        cert=cert,
        pem=cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        fingerprint_sha256=fingerprint(cert),
        source=source or str(path),
    )


def loaded_from_pem(pem: str, *, source: str) -> LoadedCertificate:
    cert = x509.load_pem_x509_certificate(pem.encode("utf-8"))
    return LoadedCertificate(
        cert=cert,
        pem=pem,
        fingerprint_sha256=fingerprint(cert),
        source=source,
    )


def _extension_or_none(cert: x509.Certificate, extension_type: type) -> Any | None:
    try:
        return cert.extensions.get_extension_for_class(extension_type).value
    except x509.ExtensionNotFound:
        return None


def _is_ca(cert: x509.Certificate) -> bool:
    value = _extension_or_none(cert, x509.BasicConstraints)
    return bool(value and value.ca)


class RevocationEngine:
    def __init__(self, sample_dir: Path, certificates: Iterable[LoadedCertificate]):
        self.sample_dir = sample_dir
        self.by_fingerprint = {cert.fingerprint_sha256: cert for cert in certificates}
        self.crls: list[tuple[Path, x509.CertificateRevocationList]] = []
        self.ocsp_records: list[dict[str, Any]] = []
        self._load_crls()
        self._load_ocsp()

    def _load_crls(self) -> None:
        directory = self.sample_dir / "revocation"
        if not directory.is_dir():
            return
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.suffix.casefold() not in {".pem", ".crl", ".der"}:
                continue
            raw = path.read_bytes()
            try:
                if b"BEGIN X509 CRL" in raw:
                    crl = x509.load_pem_x509_crl(raw)
                else:
                    crl = x509.load_der_x509_crl(raw)
            except ValueError:
                continue
            self.crls.append((path, crl))

    def _load_ocsp(self) -> None:
        path = self.sample_dir / "revocation" / "ocsp-records.json"
        if not path.is_file():
            return
        try:
            payload = load_json_file(path)
        except Exception:
            return
        records = payload.get("records") if isinstance(payload, dict) else None
        if isinstance(records, list):
            self.ocsp_records = [record for record in records if isinstance(record, dict)]

    def _crl_evidence(
        self,
        certificate: LoadedCertificate,
        issuer: LoadedCertificate,
        now_ms: int,
    ) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for path, crl in self.crls:
            if crl.issuer != issuer.cert.subject:
                continue
            try:
                signature_ok = crl.is_signature_valid(issuer.cert.public_key())
            except Exception:
                signature_ok = False
            this_update = datetime_to_ms(
                _utc_property(crl, "last_update_utc", "last_update")
            )
            next_update = datetime_to_ms(
                _utc_property(crl, "next_update_utc", "next_update")
            )
            fresh = this_update <= now_ms <= next_update
            if not signature_ok or not fresh:
                evidence.append(
                    {
                        "type": "crl",
                        "source": path.name,
                        "usable": False,
                        "signature_valid": signature_ok,
                        "fresh": fresh,
                        "this_update_ms": this_update,
                        "next_update_ms": next_update,
                    }
                )
                continue
            revoked = crl.get_revoked_certificate_by_serial_number(
                certificate.cert.serial_number
            )
            reason = None
            if revoked is not None:
                try:
                    reason = revoked.extensions.get_extension_for_class(
                        x509.CRLReason
                    ).value.reason.name
                except x509.ExtensionNotFound:
                    reason = "unspecified"
            evidence.append(
                {
                    "type": "crl",
                    "source": path.name,
                    "usable": True,
                    "signature_valid": True,
                    "fresh": True,
                    "this_update_ms": this_update,
                    "next_update_ms": next_update,
                    "status": "revoked" if revoked is not None else "good",
                    "reason": reason,
                    "serial_number": str(certificate.cert.serial_number),
                }
            )
        return evidence

    def _ocsp_evidence(
        self,
        certificate: LoadedCertificate,
        issuer: LoadedCertificate,
        now_ms: int,
    ) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for record in self.ocsp_records:
            if record.get("issuer_fingerprint_sha256") != issuer.fingerprint_sha256:
                continue
            if str(record.get("serial_number")) != str(certificate.cert.serial_number):
                continue
            source = str(record.get("record_id", "ocsp"))
            try:
                this_update = int(record["this_update_ms"])
                next_update = int(record["next_update_ms"])
                produced_at = int(record["produced_at_ms"])
                status = record["status"]
                payload = {
                    "record_id": record.get("record_id"),
                    "issuer_fingerprint_sha256": record["issuer_fingerprint_sha256"],
                    "serial_number": record["serial_number"],
                    "status": record["status"],
                    "reason": record.get("reason"),
                    "produced_at_ms": produced_at,
                    "this_update_ms": this_update,
                    "next_update_ms": next_update,
                }
                signature = base64.b64decode(record["signature_b64"], validate=True)
                issuer.cert.public_key().verify(
                    signature, stable_json(payload).encode("utf-8")
                )
                signature_ok = True
            except Exception:
                signature_ok = False
                this_update = int(record.get("this_update_ms", -1)) if str(
                    record.get("this_update_ms", "")
                ).lstrip("-").isdigit() else -1
                next_update = int(record.get("next_update_ms", -1)) if str(
                    record.get("next_update_ms", "")
                ).lstrip("-").isdigit() else -1
                produced_at = -1
                status = record.get("status")
            fresh = this_update <= now_ms <= next_update
            usable = signature_ok and fresh and status in {"good", "revoked", "unknown"}
            evidence.append(
                {
                    "type": "ocsp",
                    "source": source,
                    "usable": usable,
                    "signature_valid": signature_ok,
                    "fresh": fresh,
                    "produced_at_ms": produced_at,
                    "this_update_ms": this_update,
                    "next_update_ms": next_update,
                    "status": status if usable else "unknown",
                    "reason": record.get("reason"),
                    "serial_number": str(certificate.cert.serial_number),
                }
            )
        return evidence

    def check(
        self,
        certificate: LoadedCertificate,
        issuer: LoadedCertificate,
        now_ms: int,
    ) -> dict[str, Any]:
        evidence = [
            *self._ocsp_evidence(certificate, issuer, now_ms),
            *self._crl_evidence(certificate, issuer, now_ms),
        ]
        usable = [item for item in evidence if item.get("usable")]
        revoked = [item for item in usable if item.get("status") == "revoked"]
        good = [item for item in usable if item.get("status") == "good"]
        if revoked:
            status = "revoked"
            selected = revoked[0]
        elif good:
            status = "good"
            selected = good[0]
        else:
            status = "unknown"
            selected = None
        return {
            "certificate_fingerprint_sha256": certificate.fingerprint_sha256,
            "issuer_fingerprint_sha256": issuer.fingerprint_sha256,
            "serial_number": str(certificate.cert.serial_number),
            "status": status,
            "evidence_type": selected["type"] if selected else "none",
            "selected_source": selected.get("source") if selected else None,
            "evidence": evidence,
        }


def hostname_matches(cert: x509.Certificate, hostname: str) -> dict[str, Any]:
    candidate = hostname.strip().rstrip(".").casefold()
    result: dict[str, Any] = {
        "hostname": hostname,
        "normalized_hostname": candidate,
        "matched": False,
        "matched_name": None,
        "source": None,
        "wildcard": False,
        "reason": "no_match",
    }
    if not candidate:
        result["reason"] = "empty_hostname"
        return result
    try:
        expected_ip = ipaddress.ip_address(candidate)
    except ValueError:
        expected_ip = None

    san = _extension_or_none(cert, x509.SubjectAlternativeName)
    dns_names: list[str] = []
    ip_addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    if san is not None:
        dns_names = [item.casefold().rstrip(".") for item in san.get_values_for_type(x509.DNSName)]
        ip_addresses = list(san.get_values_for_type(x509.IPAddress))

    if expected_ip is not None:
        for address in ip_addresses:
            if address == expected_ip:
                result.update(
                    {
                        "matched": True,
                        "matched_name": str(address),
                        "source": "san_ip",
                        "reason": "exact",
                    }
                )
                return result
    elif dns_names:
        for pattern in dns_names:
            match = _dns_pattern_matches(pattern, candidate)
            if match is not None:
                result.update(
                    {
                        "matched": True,
                        "matched_name": pattern,
                        "source": "san_dns",
                        "wildcard": match,
                        "reason": "wildcard" if match else "exact",
                    }
                )
                return result
    else:
        common_names = [
            attribute.value
            for attribute in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
            if isinstance(attribute.value, str)
        ]
        for pattern in common_names:
            normalized = pattern.casefold().rstrip(".")
            match = _dns_pattern_matches(normalized, candidate)
            if match is not None:
                result.update(
                    {
                        "matched": True,
                        "matched_name": normalized,
                        "source": "common_name",
                        "wildcard": match,
                        "reason": "wildcard" if match else "exact",
                    }
                )
                return result
    return result


def _dns_pattern_matches(pattern: str, hostname: str) -> bool | None:
    if not pattern:
        return None
    if "*" not in pattern:
        return False if pattern == hostname else None
    if pattern.count("*") != 1 or not pattern.startswith("*."):
        return None
    pattern_labels = pattern.split(".")
    hostname_labels = hostname.split(".")
    if len(pattern_labels) != len(hostname_labels):
        return None
    if pattern_labels[1:] != hostname_labels[1:]:
        return None
    if not hostname_labels[0] or hostname_labels[0] == "*":
        return None
    return True


class SampleRepository:
    def __init__(self, sample_dir: str | Path, view: dict[str, Any]):
        self.sample_dir = Path(sample_dir)
        self.view = view
        self.cas: list[LoadedCertificate] = []
        self.leaf_index: list[dict[str, Any]] = []
        self._load_cas()
        self._load_leaf_index()
        self.anchors: dict[str, LoadedCertificate] = {}
        for item in active_anchors(view):
            loaded = loaded_from_pem(
                item["certificate_pem"], source=f"trust:{item['alias']}"
            )
            self.anchors[loaded.fingerprint_sha256] = loaded
            if loaded.fingerprint_sha256 not in {
                cert.fingerprint_sha256 for cert in self.cas
            }:
                self.cas.append(loaded)
        all_certs = [*self.cas, *self.anchors.values()]
        self.revocation = RevocationEngine(self.sample_dir, all_certs)

    def _load_cas(self) -> None:
        directory = self.sample_dir / "cas"
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("*.pem")):
            try:
                self.cas.append(load_certificate(path, source=str(path)))
            except ValueError:
                continue
        self.cas.sort(key=lambda item: item.fingerprint_sha256)

    def _load_leaf_index(self) -> None:
        path = self.sample_dir / "leaves" / "index.json"
        if not path.is_file():
            return
        payload = load_json_file(path)
        leaves = payload.get("leaves") if isinstance(payload, dict) else None
        if isinstance(leaves, list):
            self.leaf_index = [item for item in leaves if isinstance(item, dict)]
            self.leaf_index.sort(key=lambda item: str(item.get("id", "")))

    def load_leaf(self, selected: str) -> tuple[dict[str, Any], LoadedCertificate]:
        match: dict[str, Any] | None = None
        path: Path | None = None
        for entry in self.leaf_index:
            if selected in {entry.get("id"), entry.get("file")}:
                match = entry
                path = self.sample_dir / "leaves" / str(entry["file"])
                break
        candidate = Path(selected)
        if path is None and candidate.is_file():
            resolved = candidate.resolve()
            for entry in self.leaf_index:
                indexed = (self.sample_dir / "leaves" / str(entry["file"])).resolve()
                if indexed == resolved:
                    match = entry
                    path = resolved
                    break
        if path is None and candidate.is_file():
            path = candidate
            match = {
                "id": candidate.stem,
                "file": str(candidate),
                "hostname": None,
                "purpose": None,
            }
        if path is None or match is None:
            raise FileNotFoundError(f"leaf not found: {selected}")
        return match, load_certificate(path, source=str(path))

    def anchor_metadata(self, anchor_fp: str) -> dict[str, Any]:
        for item in self.view["anchors"]:
            if item["fingerprint_sha256"] == anchor_fp:
                return item
        return {}


def _signature_ok(child: x509.Certificate, parent: x509.Certificate) -> tuple[bool, str | None]:
    try:
        child.verify_directly_issued_by(parent)
        return True, None
    except Exception as exc:
        return False, type(exc).__name__


def find_candidate_paths(
    repository: SampleRepository, leaf: LoadedCertificate
) -> tuple[list[list[LoadedCertificate]], list[dict[str, Any]]]:
    paths: list[list[LoadedCertificate]] = []
    failures: list[dict[str, Any]] = []

    def walk(current: LoadedCertificate, path: list[LoadedCertificate]) -> None:
        if len(path) > 8:
            return
        if leaf.issuer == current.subject:
            valid, cause = _signature_ok(leaf.cert, current.cert)
            if valid:
                paths.append([*path, leaf])
            else:
                failures.append(
                    {
                        "child": leaf.summary(),
                        "parent": current.summary(),
                        "error_code": ErrorCode.SIGNATURE_INVALID,
                        "cause": cause,
                    }
                )
        used = {item.fingerprint_sha256 for item in path}
        for candidate in repository.cas:
            if candidate.fingerprint_sha256 in used:
                continue
            if candidate.issuer != current.subject:
                continue
            valid, cause = _signature_ok(candidate.cert, current.cert)
            if not valid:
                failures.append(
                    {
                        "child": candidate.summary(),
                        "parent": current.summary(),
                        "error_code": ErrorCode.SIGNATURE_INVALID,
                        "cause": cause,
                    }
                )
                continue
            walk(candidate, [*path, candidate])

    for anchor in sorted(
        repository.anchors.values(), key=lambda item: item.fingerprint_sha256
    ):
        walk(anchor, [anchor])
    unique: dict[tuple[str, ...], list[LoadedCertificate]] = {}
    for path in paths:
        key = tuple(item.fingerprint_sha256 for item in path)
        unique[key] = path
    ordered = [unique[key] for key in sorted(unique)]
    failures.sort(
        key=lambda item: (
            item["child"]["fingerprint_sha256"],
            item["parent"]["fingerprint_sha256"],
        )
    )
    return ordered, failures


def _check(
    check: str,
    ok: bool,
    *,
    error_code: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "check": check,
        "ok": ok,
        "error_code": error_code,
        "details": details or {},
    }


def validate_path(
    repository: SampleRepository,
    path: list[LoadedCertificate],
    *,
    hostname: str,
    purpose: str,
    now_ms: int,
) -> dict[str, Any]:
    leaf = path[-1]
    anchor = path[0]
    steps: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    anchor_meta = repository.anchor_metadata(anchor.fingerprint_sha256)

    def error(code: str, check: str, details: dict[str, Any]) -> None:
        errors.append({"code": code, "check": check, "details": details})

    established = anchor_meta.get("effective_at_ms")
    trust_effective = established is None or now_ms >= int(established)
    steps.append(
        _check(
            "trust_anchor_effective",
            trust_effective,
            error_code=None if trust_effective else ErrorCode.ANCHOR_NOT_EFFECTIVE,
            details={
                "alias": anchor_meta.get("alias"),
                "effective_at_ms": established,
                "observed_at_ms": now_ms,
                "source_version": anchor_meta.get("source", {}).get("version"),
            },
        )
    )
    if not trust_effective:
        error(
            ErrorCode.ANCHOR_NOT_EFFECTIVE,
            "trust_anchor_effective",
            {"alias": anchor_meta.get("alias"), "effective_at_ms": established},
        )

    for index, item in enumerate(path):
        role = "anchor" if index == 0 else ("leaf" if index == len(path) - 1 else "intermediate")
        nb = not_before_ms(item.cert)
        na = not_after_ms(item.cert)
        valid_time = nb <= now_ms <= na
        code = None
        if now_ms < nb:
            code = ErrorCode.CERT_NOT_YET_VALID
        elif now_ms > na:
            code = ErrorCode.CERT_EXPIRED
        steps.append(
            _check(
                "validity",
                valid_time,
                error_code=code,
                details={
                    "role": role,
                    "fingerprint_sha256": item.fingerprint_sha256,
                    "not_before_ms": nb,
                    "not_after_ms": na,
                    "observed_at_ms": now_ms,
                },
            )
        )
        if code:
            error(code, "validity", {"fingerprint_sha256": item.fingerprint_sha256, "role": role})

    for index in range(1, len(path)):
        child = path[index]
        parent = path[index - 1]
        valid, cause = _signature_ok(child.cert, parent.cert)
        steps.append(
            _check(
                "signature",
                valid,
                error_code=None if valid else ErrorCode.SIGNATURE_INVALID,
                details={
                    "child_fingerprint_sha256": child.fingerprint_sha256,
                    "issuer_fingerprint_sha256": parent.fingerprint_sha256,
                    "child_issuer": child.cert.issuer.rfc4514_string(),
                    "parent_subject": parent.cert.subject.rfc4514_string(),
                    "cause": cause,
                },
            )
        )
        if not valid:
            error(
                ErrorCode.SIGNATURE_INVALID,
                "signature",
                {
                    "child_fingerprint_sha256": child.fingerprint_sha256,
                    "issuer_fingerprint_sha256": parent.fingerprint_sha256,
                },
            )

    for index, item in enumerate(path):
        constraints = _extension_or_none(item.cert, x509.BasicConstraints)
        role = "anchor" if index == 0 else ("leaf" if index == len(path) - 1 else "intermediate")
        if role == "leaf":
            ok = bool(constraints is not None and not constraints.ca)
        else:
            ok = bool(constraints is not None and constraints.ca)
        steps.append(
            _check(
                "basic_constraints",
                ok,
                error_code=None if ok else ErrorCode.BASIC_CONSTRAINTS,
                details={
                    "role": role,
                    "fingerprint_sha256": item.fingerprint_sha256,
                    "ca": constraints.ca if constraints is not None else None,
                    "present": constraints is not None,
                },
            )
        )
        if not ok:
            error(
                ErrorCode.BASIC_CONSTRAINTS,
                "basic_constraints",
                {"fingerprint_sha256": item.fingerprint_sha256, "role": role},
            )

    for index, item in enumerate(path):
        constraints = _extension_or_none(item.cert, x509.BasicConstraints)
        if constraints is None or not constraints.ca or constraints.path_length is None:
            continue
        subordinate_cas = sum(
            1
            for lower in path[index + 1 : -1]
            if _is_ca(lower.cert)
        )
        ok = subordinate_cas <= constraints.path_length
        steps.append(
            _check(
                "path_length",
                ok,
                error_code=None if ok else ErrorCode.PATH_LENGTH,
                details={
                    "fingerprint_sha256": item.fingerprint_sha256,
                    "path_length_limit": constraints.path_length,
                    "subordinate_ca_count": subordinate_cas,
                },
            )
        )
        if not ok:
            error(
                ErrorCode.PATH_LENGTH,
                "path_length",
                {
                    "fingerprint_sha256": item.fingerprint_sha256,
                    "limit": constraints.path_length,
                    "actual": subordinate_cas,
                },
            )

    purpose_oid = PURPOSE_OIDS.get(PURPOSE_ALIASES.get(purpose, purpose))
    for index, item in enumerate(path):
        role = "anchor" if index == 0 else ("leaf" if index == len(path) - 1 else "intermediate")
        key_usage = _extension_or_none(item.cert, x509.KeyUsage)
        ok = True
        detail: dict[str, Any] = {
            "role": role,
            "fingerprint_sha256": item.fingerprint_sha256,
            "key_usage_present": key_usage is not None,
        }
        if role == "leaf":
            if key_usage is not None:
                if purpose == "serverAuth":
                    leaf_usage_ok = (
                        key_usage.digital_signature or key_usage.key_encipherment
                    )
                else:
                    leaf_usage_ok = key_usage.digital_signature
            else:
                leaf_usage_ok = True
            if not leaf_usage_ok:
                ok = False
                detail["missing"] = (
                    "digitalSignatureOrKeyEncipherment"
                    if purpose == "serverAuth"
                    else "digitalSignature"
                )
            eku = _extension_or_none(item.cert, x509.ExtendedKeyUsage)
            detail["eku_present"] = eku is not None
            detail["purpose"] = purpose
            if purpose_oid is None:
                ok = False
                detail["missing"] = "known_purpose"
            elif eku is not None and purpose_oid not in eku:
                ok = False
                detail["missing"] = f"eku:{purpose}"
        elif key_usage is not None and not key_usage.key_cert_sign:
            ok = False
            detail["missing"] = "keyCertSign"
        steps.append(
            _check(
                "key_usage",
                ok,
                error_code=None if ok else ErrorCode.KEY_USAGE,
                details=detail,
            )
        )
        if not ok:
            error(
                ErrorCode.KEY_USAGE,
                "key_usage",
                {"fingerprint_sha256": item.fingerprint_sha256, "role": role},
            )

    hostname_result = hostname_matches(leaf.cert, hostname)
    steps.append(
        _check(
            "hostname",
            hostname_result["matched"],
            error_code=None if hostname_result["matched"] else ErrorCode.HOSTNAME_MISMATCH,
            details=hostname_result,
        )
    )
    if not hostname_result["matched"]:
        error(
            ErrorCode.HOSTNAME_MISMATCH,
            "hostname",
            {"expected": hostname, "matched_name": hostname_result["matched_name"]},
        )

    revocations: list[dict[str, Any]] = []
    anchor_revocation = anchor_meta.get("revocation", "unknown")
    if anchor_revocation == "revoked":
        revocations.append(
            {
                "certificate_fingerprint_sha256": anchor.fingerprint_sha256,
                "issuer_fingerprint_sha256": None,
                "serial_number": str(anchor.cert.serial_number),
                "status": "revoked",
                "evidence_type": "trust_store",
                "selected_source": f"trust:{anchor_meta.get('alias')}",
                "evidence": [],
            }
        )
    for index in range(1, len(path)):
        revocation = repository.revocation.check(path[index], path[index - 1], now_ms)
        revocations.append(revocation)
    for revocation in revocations:
        status = revocation["status"]
        steps.append(
            _check(
                "revocation",
                status == "good",
                error_code=None
                if status == "good"
                else (ErrorCode.REVOKED if status == "revoked" else ErrorCode.REVOCATION_UNKNOWN),
                details={
                    "certificate_fingerprint_sha256": revocation[
                        "certificate_fingerprint_sha256"
                    ],
                    "status": status,
                    "evidence_type": revocation["evidence_type"],
                    "selected_source": revocation["selected_source"],
                },
            )
        )
        if status == "revoked":
            error(
                ErrorCode.REVOKED,
                "revocation",
                {
                    "certificate_fingerprint_sha256": revocation[
                        "certificate_fingerprint_sha256"
                    ]
                },
            )
        elif status == "unknown":
            error(
                ErrorCode.REVOCATION_UNKNOWN,
                "revocation",
                {
                    "certificate_fingerprint_sha256": revocation[
                        "certificate_fingerprint_sha256"
                    ]
                },
            )

    errors.sort(
        key=lambda item: (ERROR_PRIORITY.get(item["code"], 999), item["code"], stable_json(item))
    )
    primary = errors[0] if errors else None
    chain_certificates = [
        item.summary(
            "anchor" if index == 0 else ("leaf" if index == len(path) - 1 else "intermediate")
        )
        for index, item in enumerate(path)
    ]
    return {
        "schema": "certlab-chain-result-v1",
        "valid": not errors,
        "error_code": primary["code"] if primary else None,
        "primary_error": primary,
        "leaf": leaf.summary("leaf"),
        "anchor": {
            **anchor.summary("anchor"),
            "alias": anchor_meta.get("alias"),
            "source_version": anchor_meta.get("source", {}).get("version"),
        },
        "hostname": hostname,
        "purpose": purpose,
        "observed_at_ms": now_ms,
        "path": chain_certificates,
        "steps": steps,
        "hostname_match": hostname_result,
        "revocation": revocations,
        "cache_dependencies": {
            "certificate_fingerprints": [item.fingerprint_sha256 for item in path],
            "anchor_fingerprint_sha256": anchor.fingerprint_sha256,
            "leaf_fingerprint_sha256": leaf.fingerprint_sha256,
        },
    }


def build_and_validate(
    repository: SampleRepository,
    leaf: LoadedCertificate,
    *,
    hostname: str,
    purpose: str,
    now_ms: int,
) -> dict[str, Any]:
    paths, failures = find_candidate_paths(repository, leaf)
    validated: list[dict[str, Any]] = []
    for path in paths:
        validated.append(
            validate_path(
                repository,
                path,
                hostname=hostname,
                purpose=purpose,
                now_ms=now_ms,
            )
        )
    successful = [result for result in validated if result["valid"]]
    if successful:
        successful.sort(
            key=lambda result: tuple(
                item["fingerprint_sha256"] for item in result["path"]
            )
        )
        return successful[0]
    if validated:
        validated.sort(
            key=lambda result: (
                ERROR_PRIORITY.get(result["error_code"], 999),
                tuple(item["fingerprint_sha256"] for item in result["path"]),
            )
        )
        result = validated[0]
        result["rejected_path_count"] = len(validated)
        return result

    code = (
        ErrorCode.SIGNATURE_INVALID
        if any(item["error_code"] == ErrorCode.SIGNATURE_INVALID for item in failures)
        else ErrorCode.NO_TRUST_PATH
    )
    details = {
        "leaf_fingerprint_sha256": leaf.fingerprint_sha256,
        "leaf_subject": leaf.cert.subject.rfc4514_string(),
        "leaf_issuer": leaf.cert.issuer.rfc4514_string(),
        "signature_failures": failures[:8],
        "active_anchor_count": len(repository.anchors),
    }
    return {
        "schema": "certlab-chain-result-v1",
        "valid": False,
        "error_code": code,
        "primary_error": {"code": code, "check": "path_build", "details": details},
        "leaf": leaf.summary("leaf"),
        "anchor": None,
        "hostname": hostname,
        "purpose": purpose,
        "observed_at_ms": now_ms,
        "path": [],
        "steps": [
            _check(
                "path_build",
                False,
                error_code=code,
                details=details,
            )
        ],
        "hostname_match": hostname_matches(leaf.cert, hostname),
        "revocation": [],
        "cache_dependencies": {
            "certificate_fingerprints": [leaf.fingerprint_sha256],
            "anchor_fingerprint_sha256": None,
            "leaf_fingerprint_sha256": leaf.fingerprint_sha256,
        },
    }

