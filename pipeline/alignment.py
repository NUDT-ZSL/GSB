from __future__ import annotations

from .db import Database
from .extractor import CANONICAL_FIELDS, REQUIRED_FIELDS


def _type_name(value) -> str:
    return type(value).__name__


def alignment_report(db: Database, biz_id: str) -> dict:
    """Cross-source field alignment for one business id. Version evidence
    comes from the parse-time declaration stored with each row, which is the
    same declaration used to build the unique key, so the two never drift."""
    rows = db.normalized_for_biz(biz_id)
    variants: dict[str, dict] = {}
    evidence_ok = True
    for row in rows:
        label = f"{row['source']}/{row['schema_version']}"
        variants[label] = row["fields"]
        evidence = row["evidence"]
        if evidence.get("source") != row["source"] or \
                str(evidence.get("schema_version")) != str(row["schema_version"]):
            evidence_ok = False
    keys = sorted(variants)
    pairs = []
    for i, baseline in enumerate(keys):
        for candidate in keys[i + 1:]:
            base_fields = variants[baseline]
            cand_fields = variants[candidate]
            added = sorted(set(cand_fields) - set(base_fields))
            missing = sorted(set(base_fields) - set(cand_fields))
            type_changes = sorted(
                f for f in set(base_fields) & set(cand_fields)
                if _type_name(base_fields[f]) != _type_name(cand_fields[f])
            )
            pairs.append({
                "baseline": baseline,
                "candidate": candidate,
                "added_fields": added,
                "missing_fields": missing,
                "type_changes": type_changes,
            })
    required_missing = {
        v: [f for f in REQUIRED_FIELDS if variants[v].get(f) is None]
        for v in keys
    }
    missing_required_total = sum(len(v) for v in required_missing.values())
    return {
        "business_id": biz_id,
        "canonical_fields": CANONICAL_FIELDS,
        "required_fields": REQUIRED_FIELDS,
        "versions": keys,
        "pairs": pairs,
        "required_missing": required_missing,
        "summary": {
            "variant_count": len(keys),
            "pair_count": len(pairs),
            "added_diff_count": sum(1 for p in pairs if p["added_fields"]),
            "missing_diff_count": sum(1 for p in pairs if p["missing_fields"]),
            "type_change_count": sum(1 for p in pairs if p["type_changes"]),
            "missing_required_total": missing_required_total,
            "all_required_complete": missing_required_total == 0,
            "version_evidence_consistent": evidence_ok,
        },
    }
