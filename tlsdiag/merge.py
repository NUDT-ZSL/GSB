"""Cross-version merge producing a unified trust view.

Arbitration for an alias present in both snapshots follows the declared
logical clock tick, then the trust generation, then a fixed source-format
priority (v2 wins the final tie-break because its metadata is the newer
schema).  Every surviving anchor keeps its source version, original time
representation and the textual decision basis.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .clock import LogicalClock, PrecisionConfig
from .errors import DiagError
from .precision import PrecisionLedger
from .truststore import RawAnchor, Snapshot

SOURCE_PRIORITY = {"v2": 2, "v1": 1}


@dataclass
class TrustAnchor:
    alias: str
    cert: object
    fingerprint: str
    pem_path: str
    source_version: str
    logical_clock_tick: int
    generation: int
    enabled: bool
    revoked: bool
    alert_tombstone: dict | None
    original_time: dict
    absolute_trust_ms_fixed: int
    absolute_trust_ms_truth: str
    scale: str
    rounding: str
    decision_basis: str

    def to_dict(self) -> dict:
        return {
            "alias": self.alias,
            "fingerprint_sha256": self.fingerprint,
            "pem": self.pem_path,
            "source_version": self.source_version,
            "logical_clock_tick": self.logical_clock_tick,
            "generation": self.generation,
            "enabled": self.enabled,
            "revoked": self.revoked,
            "alert_tombstone": self.alert_tombstone,
            "original_time_representation": self.original_time,
            "absolute_trust_ms_fixed": self.absolute_trust_ms_fixed,
            "absolute_trust_ms_truth": self.absolute_trust_ms_truth,
            "scale": self.scale,
            "rounding": self.rounding,
            "decision_basis": self.decision_basis,
        }


@dataclass
class TrustView:
    anchors: dict[str, TrustAnchor] = field(default_factory=dict)
    ledger: PrecisionLedger | None = None
    arbitration_log: list[dict] = field(default_factory=list)

    def enabled_anchors(self) -> list[TrustAnchor]:
        return [anchor for anchor in self.anchors.values() if anchor.enabled]


def _arbitrate(v1_anchor: RawAnchor | None, v2_anchor: RawAnchor | None) -> tuple[RawAnchor, str]:
    if v1_anchor is None:
        return v2_anchor, "only present in v2 snapshot"
    if v2_anchor is None:
        return v1_anchor, "only present in v1 snapshot"

    t1 = v1_anchor.logical_clock_tick
    t2 = v2_anchor.logical_clock_tick
    g1 = 1
    g2 = v2_anchor.generation if v2_anchor.generation is not None else 0
    if t2 > t1:
        return v2_anchor, f"logical clock v2 tick {t2} > v1 tick {t1}"
    if t1 > t2:
        return v1_anchor, f"logical clock v1 tick {t1} > v2 tick {t2}"
    if g2 > g1:
        return v2_anchor, f"equal tick {t1}; generation v2 {g2} > v1 {g1}"
    if g1 > g2:
        return v1_anchor, f"equal tick {t1}; generation v1 {g1} > v2 {g2}"
    if SOURCE_PRIORITY["v2"] >= SOURCE_PRIORITY["v1"]:
        return v2_anchor, f"equal tick {t1} and generation; source priority v2 > v1"
    return v1_anchor, f"equal tick {t1} and generation; source priority v1 > v2"


def merge_snapshots(
    v1: Snapshot,
    v2: Snapshot,
    clock: LogicalClock,
    config: PrecisionConfig,
) -> TrustView:
    ledger = PrecisionLedger(config)
    view = TrustView(ledger=ledger)
    v1_by_alias = {anchor.alias: anchor for anchor in v1.anchors}
    v2_by_alias = {anchor.alias: anchor for anchor in v2.anchors}
    all_aliases = sorted(set(v1_by_alias) | set(v2_by_alias))

    for alias in all_aliases:
        raw_v1 = v1_by_alias.get(alias)
        raw_v2 = v2_by_alias.get(alias)
        winner, basis = _arbitrate(raw_v1, raw_v2)
        if raw_v1 and raw_v2:
            view.arbitration_log.append(
                {
                    "alias": alias,
                    "v1_tick": raw_v1.logical_clock_tick,
                    "v2_tick": raw_v2.logical_clock_tick,
                    "winner": winner.source_version,
                    "decision_basis": basis,
                }
            )

        if winner.source_version == "v1":
            base_ms = clock.instant_ms(winner.logical_clock_tick)
            conversion = ledger.convert_relative_seconds(
                alias=alias,
                source_version="v1",
                step=f"clock.tick[{winner.logical_clock_tick}] + relative_trust_seconds*scale",
                relative_seconds=winner.relative_trust_seconds,
                base_ms=base_ms,
            )
            anchor = TrustAnchor(
                alias=alias,
                cert=winner.cert,
                fingerprint=winner.fingerprint,
                pem_path=winner.pem_path,
                source_version="v1",
                logical_clock_tick=winner.logical_clock_tick,
                generation=1,
                enabled=winner.enabled,
                revoked=False,
                alert_tombstone=None,
                original_time={
                    "relative_trust_seconds": winner.relative_trust_seconds,
                    "base_clock_ms": base_ms,
                },
                absolute_trust_ms_fixed=conversion.fixed_abs_ms,
                absolute_trust_ms_truth=conversion.truth_abs_ms,
                scale=conversion.scale,
                rounding=conversion.rounding,
                decision_basis=basis,
            )
        else:
            if winner.effective_ms is None:
                raise DiagError("E2004", f"v2 alias {alias} missing effective_ms")
            conversion = ledger.pass_through_exact(
                alias=alias,
                source_version="v2",
                step="integer millisecond effective_ms passthrough",
                abs_ms=winner.effective_ms,
            )
            anchor = TrustAnchor(
                alias=alias,
                cert=winner.cert,
                fingerprint=winner.fingerprint,
                pem_path=winner.pem_path,
                source_version="v2",
                logical_clock_tick=winner.logical_clock_tick,
                generation=winner.generation,
                enabled=winner.enabled,
                revoked=winner.revoked,
                alert_tombstone=winner.alert_tombstone,
                original_time={"effective_ms": winner.effective_ms},
                absolute_trust_ms_fixed=conversion.fixed_abs_ms,
                absolute_trust_ms_truth=conversion.truth_abs_ms,
                scale=conversion.scale,
                rounding=conversion.rounding,
                decision_basis=basis,
            )
        view.anchors[alias] = anchor
    return view
