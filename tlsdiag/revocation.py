"""CRL/OCSP evidence evaluation with generation-scoped conclusions.

Three states are kept strictly separate:

* ``revoked``    - positive CRL or OCSP evidence exists;
* ``good``       - OCSP explicitly says the certificate is good;
* ``unknown``    - no usable evidence at the current logical instant.

Conclusions are keyed by (fingerprint, trust generation, tombstone set) so a
generation bump or a new alert tombstone forces recomputation rather than
reusing a stale verdict.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509

from .clock import LogicalClock


@dataclass(frozen=True)
class RevocationVerdict:
    fingerprint: str
    serial: int
    status: str
    source: str
    generation: int
    tombstone_signature: str
    detail: str

    def to_dict(self) -> dict:
        return {
            "fingerprint_sha256": self.fingerprint,
            "serial": self.serial,
            "status": self.status,
            "source": self.source,
            "generation": self.generation,
            "tombstone_signature": self.tombstone_signature,
            "detail": self.detail,
        }


class RevocationEvidence:
    def __init__(self, crl_path: str | Path, ocsp_path: str | Path, clock: LogicalClock):
        self.clock = clock
        self.crl = json.loads(Path(crl_path).read_text(encoding="utf-8"))
        self.ocsp = json.loads(Path(ocsp_path).read_text(encoding="utf-8"))
        self._revoked_serials: set[int] = set()
        self._ocsp_by_serial: dict[int, dict] = {}
        instant_max = clock.tick_count - 1
        for record in self.crl:
            if record.get("this_update_tick", 0) <= instant_max <= record.get("next_update_tick", instant_max):
                self._revoked_serials.update(record.get("revoked_serials", []))
        for record in self.ocsp:
            if record.get("this_update_tick", 0) <= instant_max <= record.get("next_update_tick", instant_max):
                self._ocsp_by_serial[record["serial"]] = record

    def evaluate(
        self,
        cert: x509.Certificate,
        fingerprint: str,
        generation: int,
        anchor_revoked: bool,
        tombstone_signature: str,
    ) -> RevocationVerdict:
        serial = cert.serial_number
        if serial in self._revoked_serials or anchor_revoked:
            source = "trust-store flag" if anchor_revoked and serial not in self._revoked_serials else "CRL"
            if serial in self._revoked_serials and anchor_revoked:
                source = "CRL+trust-store flag"
            return RevocationVerdict(fingerprint, serial, "revoked", source, generation, tombstone_signature,
                                     "positive revocation evidence")
        ocsp = self._ocsp_by_serial.get(serial)
        if ocsp is not None:
            return RevocationVerdict(
                fingerprint, serial, ocsp["status"], "OCSP", generation, tombstone_signature,
                f"OCSP status {ocsp['status']}",
            )
        return RevocationVerdict(
            fingerprint, serial, "unknown", "none", generation, tombstone_signature,
            "no usable CRL/OCSP evidence at current logical instant",
        )

    @staticmethod
    def tombstone_signature(tombstone: dict | None) -> str:
        if not tombstone:
            return "none"
        return f"{tombstone.get('code')}@tick{tombstone.get('tick')}"
