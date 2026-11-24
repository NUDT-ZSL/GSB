"""Merge the two snapshot generations into one traceable drift-state view."""

from __future__ import annotations

from collections import defaultdict

from . import codes, values
from .parsers import Evidence
from .spec import BaselineSpec


def _evidence_rank(evidence: Evidence) -> tuple:
    # Latest generation first, then highest algebra, then latest observation,
    # then deterministic snapshot/record identity for stable tie-breaking.
    return (
        evidence.generation,
        evidence.algebra,
        evidence.observed_ms,
        evidence.snapshot_id,
        evidence.record_index,
    )


def _source_ref(evidence: Evidence) -> dict:
    return {
        "generation": evidence.generation,
        "algebra": evidence.algebra,
        "snapshot_id": evidence.snapshot_id,
        "record_index": evidence.record_index,
        "observed_ms": evidence.observed_ms,
        "digest": evidence.digest,
    }


def merge_view(all_evidence: list[Evidence], spec: BaselineSpec) -> dict:
    """Group evidence by (host, canonical key) and adjudicate authority.

    Output is independent of import order: evidence is ranked deterministically.
    """
    grouped: dict[tuple[str, str], list[Evidence]] = defaultdict(list)
    for evidence in all_evidence:
        grouped[(evidence.host, evidence.canonical_key)].append(evidence)

    entries: list[dict] = []
    conflicts: list[dict] = []
    for (host, key), group in grouped.items():
        ordered = sorted(group, key=_evidence_rank, reverse=True)
        authoritative = ordered[0]
        generations = sorted({item.generation for item in group})

        # Tombstones only cover earlier non-tombstone evidence.
        tombstoned = authoritative.tombstone
        if authoritative.tombstone:
            earlier = [item for item in ordered[1:] if _evidence_rank(item) < _evidence_rank(authoritative)]
            if not earlier:
                tombstoned = False

        entry: dict = {
            "host": host,
            "key": key,
            "type": spec.type_of(key),
            "authoritative": _source_ref(authoritative),
            "tombstoned": tombstoned,
            "evidence_chain": [_source_ref(item) for item in ordered],
            "generations_present": generations,
            "relations": [],
            "conflicts": [],
        }

        # Order-independent pairwise value relations between all non-tombstone items.
        live = [item for item in ordered if not item.tombstone]
        for idx in range(len(live)):
            for jdx in range(idx + 1, len(live)):
                first, second = live[idx], live[jdx]
                if first.normalized.get("type") != second.normalized.get("type"):
                    entry["conflicts"].append({
                        "code": codes.E_TYPE_MISMATCH,
                        "left": _source_ref(first),
                        "right": _source_ref(second),
                    })
                    continue
                relation = values.compare_values(first.normalized, second.normalized)
                entry["relations"].append({
                    "left": _source_ref(first),
                    "right": _source_ref(second),
                    **relation,
                })

        # Cross-generation conflicts must be surfaced, never silently resolved.
        if len(generations) == 2:
            gen1 = sorted((item for item in group if item.generation == 1),
                          key=_evidence_rank, reverse=True)
            gen2 = sorted((item for item in group if item.generation == 2),
                          key=_evidence_rank, reverse=True)
            g1, g2 = gen1[0], gen2[0]
            mismatch_reasons = []
            if not g1.tombstone and not g2.tombstone:
                if g1.normalized.get("type") != g2.normalized.get("type"):
                    mismatch_reasons.append("type")
                elif values.value_key(g1.normalized) != values.value_key(g2.normalized):
                    mismatch_reasons.append("value")
            if g1.drift != g2.drift:
                mismatch_reasons.append("drift_verdict")
            if mismatch_reasons:
                conflict = {
                    "code": codes.E_CONFLICT,
                    "host": host,
                    "key": key,
                    "reasons": mismatch_reasons,
                    "left": {"generation": 1, "algebra": g1.algebra,
                             "snapshot_id": g1.snapshot_id, "record_index": g1.record_index},
                    "right": {"generation": 2, "algebra": g2.algebra,
                              "snapshot_id": g2.snapshot_id, "record_index": g2.record_index},
                }
                conflicts.append(conflict)
                entry["conflicts"].append(conflict)

        first_drift_ms = None
        if not tombstoned:
            crossed = [item for item in live
                       if values.baseline_upper_crossed(item.normalized, spec.upper_of(key))]
            if crossed:
                first_drift_ms = min(item.observed_ms for item in crossed)
        entry["first_drift_ms"] = first_drift_ms
        entry["drifted"] = first_drift_ms is not None

        # Normalized value carried on the view for downstream adjudication.
        entry["normalized"] = None if tombstoned else authoritative.normalized
        entries.append(entry)

    entries.sort(key=lambda item: (item["host"], item["key"]))
    conflicts.sort(key=lambda item: (item["host"], item["key"]))
    return {"entries": entries, "conflicts": conflicts}
