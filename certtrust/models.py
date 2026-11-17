"""Core data model: certificate descriptions, snapshots, chain claims."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_hash(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CertDesc:
    cert_id: str
    issuer: str
    fingerprint: str  # normalized "sha256:<64 lowercase hex>"
    not_before_ms: int
    not_after_ms: int
    revoked: bool = False  # static self-declared status from the description

    @property
    def is_self_issued(self) -> bool:
        return self.issuer == self.cert_id

    def to_dict(self) -> dict:
        return {
            "id": self.cert_id,
            "issuer": self.issuer,
            "fingerprint": self.fingerprint,
            "not_before_ms": self.not_before_ms,
            "not_after_ms": self.not_after_ms,
            "revoked": self.revoked,
        }

    @staticmethod
    def from_dict(d: dict) -> "CertDesc":
        return CertDesc(
            cert_id=d["id"],
            issuer=d["issuer"],
            fingerprint=d["fingerprint"],
            not_before_ms=d["not_before_ms"],
            not_after_ms=d["not_after_ms"],
            revoked=bool(d.get("revoked", False)),
        )


@dataclass(frozen=True)
class Snapshot:
    """A trust-anchor version snapshot. Snapshots fully replace each other."""

    structure_version: int
    snapshot_version: int
    policy_version: str
    observed_at_ms: int | None
    trusted_roots: frozenset
    revocations: tuple  # tuple of (cert_id, revoked_at_ms), sorted by cert_id

    def content_dict(self) -> dict:
        return {
            "structure_version": self.structure_version,
            "snapshot_version": self.snapshot_version,
            "policy_version": self.policy_version,
            "observed_at_ms": self.observed_at_ms,
            "trusted_roots": sorted(self.trusted_roots),
            "revocations": [
                {"cert_id": cid, "revoked_at_ms": ts}
                for cid, ts in self.revocations
            ],
        }

    @property
    def stable_key(self) -> str:
        return stable_hash(self.content_dict())

    @property
    def sort_key(self):
        # Effective version is decided by (version, stable key) only, never
        # by arrival order or directory traversal order.
        return (self.snapshot_version, self.stable_key)

    def revocation_map(self) -> dict:
        return dict(self.revocations)

    def to_dict(self) -> dict:
        d = self.content_dict()
        d["stable_key"] = self.stable_key
        return d

    @staticmethod
    def from_dict(d: dict) -> "Snapshot":
        return Snapshot(
            structure_version=d["structure_version"],
            snapshot_version=d["snapshot_version"],
            policy_version=d["policy_version"],
            observed_at_ms=d.get("observed_at_ms"),
            trusted_roots=frozenset(d["trusted_roots"]),
            revocations=tuple(
                sorted(
                    (r["cert_id"], r["revoked_at_ms"]) for r in d["revocations"]
                )
            ),
        )


@dataclass(frozen=True)
class ChainClaim:
    claim_id: str
    chain: tuple  # leaf -> root ordered certificate identifiers

    def to_dict(self) -> dict:
        return {"claim_id": self.claim_id, "chain": list(self.chain)}

    @staticmethod
    def from_dict(d: dict) -> "ChainClaim":
        return ChainClaim(claim_id=d["claim_id"], chain=tuple(d["chain"]))


@dataclass
class ValidationError:
    code: str
    position: int | None
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "position": self.position, "message": self.message}


@dataclass
class Conclusion:
    """A path-validation conclusion, always bound to one anchor version."""

    claim_id: str
    verdict: str  # TRUSTED | UNTRUSTED | PARTIAL
    anchor_version: int
    anchor_key: str
    policy_version: str
    errors: list = field(default_factory=list)  # list[ValidationError]
    path: list = field(default_factory=list)  # resolved cert ids, leaf -> root
    explanation: str = ""

    def to_dict(self) -> dict:
        return {
            "claim_id": self.claim_id,
            "verdict": self.verdict,
            "anchor": {
                "snapshot_version": self.anchor_version,
                "stable_key": self.anchor_key,
                "policy_version": self.policy_version,
            },
            "errors": [e.to_dict() for e in self.errors],
            "path": list(self.path),
            "explanation": self.explanation,
        }

    @staticmethod
    def from_dict(d: dict) -> "Conclusion":
        return Conclusion(
            claim_id=d["claim_id"],
            verdict=d["verdict"],
            anchor_version=d["anchor"]["snapshot_version"],
            anchor_key=d["anchor"]["stable_key"],
            policy_version=d["anchor"]["policy_version"],
            errors=[
                ValidationError(e["code"], e.get("position"), e["message"])
                for e in d["errors"]
            ],
            path=list(d["path"]),
            explanation=d["explanation"],
        )
