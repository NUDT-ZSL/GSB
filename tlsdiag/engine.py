"""High-level orchestration tying parsing, merge, chain, revocation and
precision diagnostics together, backed by versioned SQLite persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from fractions import Fraction

from .chain import validate_chain
from .clock import LogicalClock, PrecisionConfig
from .merge import TrustAnchor, TrustView, merge_snapshots
from .precision import BoundaryResult, Participant, compare_remaining
from .revocation import RevocationEvidence
from .storage import Storage, canonical_hash
from .truststore import load_v1, load_v2


@dataclass
class SampleLayout:
    root: Path
    clock: LogicalClock
    precision: PrecisionConfig

    @classmethod
    def load(cls, root: str | Path) -> "SampleLayout":
        root = Path(root)
        return cls(
            root=root,
            clock=LogicalClock.from_file(root / "clock.json"),
            precision=PrecisionConfig.from_file(root / "precision.json"),
        )

    @property
    def v1_path(self) -> Path:
        return self.root / "truststore_v1" / "truststore.json"

    @property
    def v2_manifest(self) -> Path:
        return self.root / "truststore_v2" / "manifest.json"

    @property
    def expectations_path(self) -> Path:
        return self.root / "leaf_expectations.json"

    @property
    def crl_path(self) -> Path:
        return self.root / "revocation" / "crl.json"

    @property
    def ocsp_path(self) -> Path:
        return self.root / "revocation" / "ocsp.json"


def build_trust_view(layout: SampleLayout) -> tuple[TrustView, dict, dict]:
    v1 = load_v1(layout.v1_path)
    v2 = load_v2(layout.v2_manifest)
    view = merge_snapshots(v1, v2, layout.clock, layout.precision)
    return view, v1, v2


def _load_ca_certs(layout: SampleLayout) -> list[x509.Certificate]:
    certs = []
    for name in ("root_ca.pem", "intermediate_ca.pem"):
        certs.append(x509.load_pem_x509_certificate((layout.root / "certs" / name).read_bytes()))
    return certs


def _anchor_for_subject(view: TrustView, cert: x509.Certificate) -> TrustAnchor | None:
    for anchor in view.anchors.values():
        if anchor.cert.subject == cert.subject:
            return anchor
    return None


def _truth_remaining(anchor: TrustAnchor | None, fixed_remaining: int) -> Fraction:
    """Reconstruct the truth-path remaining time using the anchor ledger error."""
    if anchor is None:
        return Fraction(fixed_remaining)
    deviation = Fraction(anchor.absolute_trust_ms_truth) - Fraction(anchor.absolute_trust_ms_fixed)
    return Fraction(fixed_remaining) - deviation


class DiagnosticEngine:
    def __init__(self, layout: SampleLayout, storage: Storage):
        self.layout = layout
        self.clock = layout.clock
        self.config = layout.precision
        self.storage = storage

    # ----- apply (load + merge + persist) ---------------------------------
    def apply_snapshot(self, requested_version: int) -> dict:
        view, _, _ = build_trust_view(self.layout)
        anchor_payloads = {alias: anchor.to_dict() for alias, anchor in sorted(view.anchors.items())}
        patch_hash = canonical_hash(
            {
                "requested_version": requested_version,
                "v1": self.layout.v1_path.read_text(encoding="utf-8"),
                "v2_manifest": self.layout.v2_manifest.read_text(encoding="utf-8"),
                "precision": self.config.to_dict(),
            }
        )
        content_hash = canonical_hash(anchor_payloads)
        return self.storage.commit_snapshot(
            version=requested_version,
            patch_hash=patch_hash,
            content_hash=content_hash,
            logical_clock_tick=self.clock.tick_count - 1,
            clock_high_water_ms=self.clock.high_water_ms,
            anchor_payloads=anchor_payloads,
            ledger_entries=[entry.to_dict() for entry in view.ledger.entries],
        )

    # ----- verification ----------------------------------------------------
    def verify_all(self) -> list[dict]:
        expectations = json.loads(self.layout.expectations_path.read_text(encoding="utf-8"))
        tick = expectations["verify_tick"]
        instant_ms = self.clock.instant_ms(tick)
        view, _, _ = build_trust_view(self.layout)
        ca_certs = _load_ca_certs(self.layout)
        evidence = RevocationEvidence(self.layout.crl_path, self.layout.ocsp_path, self.clock)
        version = self.storage.current_version()
        results: list[dict] = []
        sensitive: list[BoundaryResult] = []

        leaf_results: list = []
        for spec in expectations["leaves"]:
            leaf = x509.load_pem_x509_certificate((self.layout.root / spec["pem"]).read_bytes())
            purpose = spec["expected_purpose"]
            generation = max(anchor.generation for anchor in view.enabled_anchors())
            # The request identity includes the trust generation, so a generation
            # bump forces recomputation instead of reusing a stale conclusion.
            request = {
                "hostname": spec["hostname"],
                "purpose": purpose,
                "tick": tick,
                "generation": generation,
            }
            request_hash = canonical_hash(request)
            cached = self.storage.job_result(request_hash)
            if cached is not None:
                results.append(cached)
                leaf_results.append((spec, leaf, cached))
                continue

            job_id = "verify:" + request_hash
            if not self.storage.start_job(job_id, "verify", version, request_hash, request):
                cached = self.storage.job_result(request_hash)
                results.append(cached)
                leaf_results.append((spec, leaf, cached))
                continue

            chain_result = validate_chain(
                leaf, spec["hostname"], purpose, generation, instant_ms, view, ca_certs
            )

            anchor = _anchor_for_subject(view, leaf)
            tombstone = RevocationEvidence.tombstone_signature(anchor.alert_tombstone if anchor else None)
            verdict = evidence.evaluate(
                leaf,
                leaf.fingerprint(hashes.SHA256()).hex(),
                generation,
                anchor.revoked if anchor else False,
                tombstone,
            )
            self.storage.save_revocation(verdict.to_dict())
            if verdict.status == "revoked":
                chain_result.valid = False
                chain_result.failure_code = "E4007"
                from .chain import TraceNode

                chain_result.trace.append(
                    TraceNode("revocation", chain_result.leaf_alias, False, verdict.detail, "E4007")
                )

            payload = chain_result.to_dict()
            payload["revocation"] = verdict.to_dict()
            self.storage.save_chain_conclusion(version, spec["hostname"], purpose, generation, payload)
            self.storage.complete_request("verify", request_hash, payload)
            results.append(payload)
            leaf_results.append((spec, leaf, payload))

        sensitive = self._precision_sensitive(view, leaf_results, instant_ms, version)
        for boundary in sensitive:
            self.storage.save_sensitive(version, boundary.to_dict())
        return results

    def _precision_sensitive(self, view, leaf_results, instant_ms, version) -> list[BoundaryResult]:
        cfg = self.config
        outcomes: list[BoundaryResult] = []
        # Trust-anchor activation boundary: anchors sharing a certificate subject
        # are compared on their absolute trust instant (activation) gap.
        subject_groups: dict[str, list[TrustAnchor]] = {}
        for anchor in view.anchors.values():
            subject_groups.setdefault(anchor.cert.subject.rfc4514_string(), []).append(anchor)
        for anchors in subject_groups.values():
            if len(anchors) < 2:
                continue
            participants = []
            for anchor in sorted(anchors, key=lambda item: item.alias):
                fixed_remaining = anchor.absolute_trust_ms_fixed - instant_ms
                truth_remaining = Fraction(anchor.absolute_trust_ms_truth) - instant_ms
                deviation = abs(truth_remaining - Fraction(fixed_remaining))
                participants.append(
                    Participant(
                        label=f"anchor:{anchor.alias}",
                        source_version=anchor.source_version,
                        fixed_remaining_ms=fixed_remaining,
                        truth_remaining_ms=truth_remaining,
                        deviation_ms=deviation,
                        error_bound_ms=Fraction(cfg.sensitive_tolerance_ms, 2),
                    )
                )
            for kind in ("expiry", "reverify"):
                outcomes.append(compare_remaining(kind, participants, cfg))
        participants_by_leaf: dict[str, list[Participant]] = {}

        for spec, leaf, payload in leaf_results:
            subject = payload["ordered_subjects"][0] if payload["ordered_subjects"] else spec["leaf"]
            fixed_remaining = payload["remaining_ms"].get(subject, 0)
            anchor = _anchor_for_subject(view, leaf)
            truth_remaining = _truth_remaining(anchor, fixed_remaining)
            deviation = abs(truth_remaining - Fraction(fixed_remaining))
            bound = Fraction(cfg.sensitive_tolerance_ms, 2)
            participant = Participant(
                label=spec["leaf"],
                source_version=anchor.source_version if anchor else "external",
                fixed_remaining_ms=fixed_remaining,
                truth_remaining_ms=truth_remaining,
                deviation_ms=deviation,
                error_bound_ms=bound,
            )
            participants_by_leaf[spec["leaf"]] = [participant]
            for kind in ("expiry", "reverify"):
                outcomes.append(compare_remaining(kind, [participant], cfg))

        # Pairwise remaining-time comparison across leaves in deterministic order.
        labels = sorted(participants_by_leaf)
        for i in range(len(labels)):
            for j in range(i + 1, len(labels)):
                pair = participants_by_leaf[labels[i]] + participants_by_leaf[labels[j]]
                for kind in ("expiry", "reverify"):
                    outcomes.append(compare_remaining(kind, pair, cfg))
        return [outcome for outcome in outcomes if outcome.precision_sensitive]

    # ----- resume ----------------------------------------------------------
    def resume(self) -> dict:
        unfinished = self.storage.unfinished_jobs("verify")
        replayed = []
        if unfinished:
            # Recompute only unfinished requests.  A forced termination leaves
            # the job row in 'started' with no result; rerunning verify_all fills
            # those rows while committed requests remain untouched.
            expectations = json.loads(self.layout.expectations_path.read_text())
            wanted = {job["request_hash"] for job in unfinished}
            view, _, _ = build_trust_view(self.layout)
            current_generation = max(anchor.generation for anchor in view.enabled_anchors())
            for spec in expectations["leaves"]:
                request = {
                    "hostname": spec["hostname"],
                    "purpose": spec["expected_purpose"],
                    "tick": expectations["verify_tick"],
                    "generation": current_generation,
                }
                if canonical_hash(request) in wanted:
                    replayed.append(spec["leaf"])
            self.verify_all()
            replayed = [job["job_id"] for job in unfinished]
        return {"replayed_jobs": replayed, "remaining_unfinished": self.storage.unfinished_jobs("verify")}

    def status(self) -> dict:
        version = self.storage.current_version()
        return {
            "current_version": version,
            "clock_high_water_ms": self.storage.clock_high_water(),
            "chain_conclusions": sorted(
                (
                    {
                        "hostname": item["hostname"],
                        "purpose": item["purpose"],
                        "generation": item["generation"],
                        "valid": item["valid"],
                        "failure_code": item["failure_code"],
                    }
                    for item in self.storage.chain_conclusions(version)
                ),
                key=lambda item: (item["hostname"], item["purpose"], item["generation"]),
            ),
            "precision_sensitive": self.storage.sensitive_records(version),
            "revocation": self.storage.revocation_conclusions(),
        }
