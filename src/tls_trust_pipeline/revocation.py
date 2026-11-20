from __future__ import annotations

import json
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from .canonical import (
    b64d,
    canonical_fingerprint,
    canonical_json,
    sha256_bytes,
    sha256_file,
)
from .certs import CertPool, cert_fingerprint
from .errors import E_BAD_REQUEST, PipelineError


class RevocationEvidence:
    def __init__(
        self,
        path: Path | None,
        pool: CertPool,
    ) -> None:
        self.path = path
        self.pool = pool
        self.digest = sha256_file(path) if path and path.exists() else None
        self.records: list[dict] = []
        self._invalid_records: list[dict] = []
        if path is not None:
            self._load(path)

    def _load(self, path: Path) -> None:
        if not path.is_file():
            raise PipelineError(E_BAD_REQUEST, "revocation evidence file not found", {"path": str(path)})
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineError(E_BAD_REQUEST, "invalid revocation evidence JSON") from exc
        if (
            not isinstance(document, dict)
            or document.get("schema") != 1
            or not isinstance(document.get("records"), list)
        ):
            raise PipelineError(E_BAD_REQUEST, "unsupported revocation evidence schema")
        for index, record in enumerate(document["records"]):
            reason = self._validate_record(record)
            if reason is None:
                self.records.append(record)
            else:
                self._invalid_records.append({"index": index, "reason": reason})
        self.records.sort(
            key=lambda record: (
                record["issuer_cert_file"],
                record["source"],
                int(record["issued_at_ms"]),
                sha256_bytes(canonical_json(record)),
            )
        )

    def _validate_record(self, record: dict) -> str | None:
        required = {
            "source",
            "issuer_cert_file",
            "issued_at_ms",
            "next_update_ms",
            "entries",
            "signature_algorithm",
            "signature_b64",
        }
        if not required.issubset(record):
            return "E_REVOCATION_EVIDENCE_INVALID"
        if not isinstance(record["issuer_cert_file"], str):
            return "E_REVOCATION_EVIDENCE_INVALID"
        if not isinstance(record["signature_b64"], str):
            return "E_REVOCATION_EVIDENCE_INVALID"
        if record["source"] not in ("crl", "ocsp"):
            return "E_REVOCATION_EVIDENCE_INVALID"
        for field in ("issued_at_ms", "next_update_ms"):
            if not isinstance(record[field], int) or isinstance(record[field], bool):
                return "E_REVOCATION_EVIDENCE_INVALID"
        if record["issued_at_ms"] > record["next_update_ms"]:
            return "E_REVOCATION_EVIDENCE_INVALID"
        if not isinstance(record["entries"], list):
            return "E_REVOCATION_EVIDENCE_INVALID"
        for entry in record["entries"]:
            if not isinstance(entry, dict) or entry.get("status") not in (
                "good",
                "revoked",
            ):
                return "E_REVOCATION_EVIDENCE_INVALID"
            if record["source"] == "crl" and not isinstance(
                entry.get("serial_hex"), str
            ):
                return "E_REVOCATION_EVIDENCE_INVALID"
            if record["source"] == "ocsp" and not isinstance(
                entry.get("cert_fingerprint"), str
            ):
                return "E_REVOCATION_EVIDENCE_INVALID"
            if entry["status"] == "revoked":
                revoked_at_ms = entry.get("revoked_at_ms")
                if (
                    not isinstance(revoked_at_ms, int)
                    or isinstance(revoked_at_ms, bool)
                    or revoked_at_ms < 0
                ):
                    return "E_REVOCATION_EVIDENCE_INVALID"
        if record["signature_algorithm"] != "rsa-pkcs1v15-sha256":
            return "E_REVOCATION_EVIDENCE_UNSUPPORTED"
        try:
            signer = self.pool.get_file(record["issuer_cert_file"])
        except PipelineError:
            return "E_REVOCATION_EVIDENCE_INVALID"
        try:
            basic = signer.extensions.get_extension_for_class(
                x509.BasicConstraints
            ).value
            key_usage = signer.extensions.get_extension_for_class(x509.KeyUsage).value
        except x509.ExtensionNotFound:
            return "E_REVOCATION_EVIDENCE_INVALID"
        if not basic.ca:
            return "E_REVOCATION_EVIDENCE_INVALID"
        if record["source"] == "crl" and not key_usage.crl_sign:
            return "E_REVOCATION_EVIDENCE_INVALID"
        if record["source"] == "ocsp" and not key_usage.digital_signature:
            return "E_REVOCATION_EVIDENCE_INVALID"
        signed = {
            key: value
            for key, value in record.items()
            if key not in ("signature_algorithm", "signature_b64")
        }
        try:
            signature = b64d(record["signature_b64"])
            signer.public_key().verify(signature, canonical_json(signed), padding.PKCS1v15(), hashes.SHA256())
        except (ValueError, InvalidSignature, TypeError):
            return "E_REVOCATION_EVIDENCE_INVALID"
        return None

    @staticmethod
    def _entry_matches(
        record: dict,
        cert: x509.Certificate,
    ) -> dict | None:
        for entry in record["entries"]:
            if not isinstance(entry, dict):
                continue
            if record["source"] == "crl":
                if str(entry.get("serial_hex", "")).lower() == format(cert.serial_number, "x"):
                    return entry
            else:
                fingerprint = entry.get("cert_fingerprint")
                if fingerprint is not None:
                    try:
                        if canonical_fingerprint(fingerprint) == cert_fingerprint(cert):
                            return entry
                    except (TypeError, ValueError):
                        continue
        return None

    def check(self, cert: x509.Certificate, logical_clock_ms: int) -> dict:
        cert_fp = cert_fingerprint(cert)
        applicable = [
            record
            for record in self.records
            if self.pool.get_file(record["issuer_cert_file"]).subject == cert.issuer
        ]
        if not applicable:
            return {
                "certificate_fingerprint": cert_fp,
                "serial_hex": format(cert.serial_number, "x"),
                "status": "UNKNOWN",
                "reason": "E_REVOCATION_EVIDENCE_MISSING",
                "source": None,
                "evidence_sha256": self.digest,
                "checked_clock_ms": logical_clock_ms,
                "record_sha256": None,
                "revoked_at_ms": None,
            }

        usable: list[dict] = []
        reasons: list[str] = []
        for record in applicable:
            if logical_clock_ms < record["issued_at_ms"]:
                reasons.append("E_REVOCATION_EVIDENCE_NOT_YET_VALID")
                continue
            if logical_clock_ms > record["next_update_ms"]:
                reasons.append("E_REVOCATION_EVIDENCE_EXPIRED")
                continue
            entry = self._entry_matches(record, cert)
            if entry is None:
                reasons.append("E_REVOCATION_ENTRY_MISSING")
                continue
            usable.append({"record": record, "entry": entry})

        if not usable:
            reason = "E_REVOCATION_EVIDENCE_MISSING"
            for candidate in (
                "E_REVOCATION_EVIDENCE_INVALID",
                "E_REVOCATION_EVIDENCE_EXPIRED",
                "E_REVOCATION_EVIDENCE_NOT_YET_VALID",
                "E_REVOCATION_ENTRY_MISSING",
            ):
                if candidate in reasons:
                    reason = candidate
                    break
            if not applicable and self._invalid_records:
                reason = "E_REVOCATION_EVIDENCE_INVALID"
            return {
                "certificate_fingerprint": cert_fp,
                "serial_hex": format(cert.serial_number, "x"),
                "status": "UNKNOWN",
                "reason": reason,
                "source": None,
                "evidence_sha256": self.digest,
                "checked_clock_ms": logical_clock_ms,
                "record_sha256": None,
                "revoked_at_ms": None,
            }

        usable.sort(
            key=lambda item: (
                0 if item["record"]["source"] == "ocsp" else 1,
                -int(item["record"]["issued_at_ms"]),
                sha256_bytes(canonical_json(item["record"])),
            )
        )
        selected = usable[0]
        record = selected["record"]
        entry = selected["entry"]
        if entry.get("status") == "revoked":
            status = "REVOKED"
            reason = "E_REVOKED"
        elif entry.get("status") == "good":
            status = "GOOD"
            reason = "OK"
        else:
            status = "UNKNOWN"
            reason = "E_REVOCATION_STATUS_INVALID"
        return {
            "certificate_fingerprint": cert_fp,
            "serial_hex": format(cert.serial_number, "x"),
            "status": status,
            "reason": reason,
            "source": record["source"],
            "evidence_sha256": self.digest,
            "record_sha256": sha256_bytes(canonical_json(record)),
            "checked_clock_ms": logical_clock_ms,
            "revoked_at_ms": entry.get("revoked_at_ms"),
        }

    def check_chain(
        self,
        chain_fingerprints: list[str],
        logical_clock_ms: int,
    ) -> list[dict]:
        results = []
        for fingerprint in chain_fingerprints[:-1]:
            cert = self.pool.get_fp(fingerprint)
            if cert is None:
                raise PipelineError(
                    E_BAD_REQUEST,
                    "chain certificate is absent from cert pool",
                    {"fingerprint": fingerprint},
                )
            results.append(self.check(cert, logical_clock_ms))
        return results

