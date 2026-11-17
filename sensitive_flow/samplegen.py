"""Deterministic local sample generator and truth manifest builder."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .engine import RULESET_VERSION


def _sync(version: str) -> dict[str, str]:
    return {"declaration_version": version, "rule_version": RULESET_VERSION}


def _field(
    path: str,
    level: int,
    masked: bool,
) -> dict[str, Any]:
    return {"path": path, "level": level, "masked": masked}


def _derivation(
    derivation_id: str,
    source: str,
    target: str,
    version: str,
    transitive: bool,
    expected_target_level: int,
) -> dict[str, Any]:
    return {
        "id": derivation_id,
        "source": source,
        "target": target,
        "version": version,
        "transitive": transitive,
        "version_sync": _sync(version),
        "truth": {"expected_effective_target_level": expected_target_level},
    }


def build_snapshots_payload() -> dict[str, Any]:
    v1_derivations = [
        _derivation(
            "d-v1-phone-direct",
            "customer.phone",
            "reports.phoneCopy",
            "v1",
            False,
            0,
        ),
        _derivation(
            "d-v2-id-trans",
            "customer.id",
            "reports.contactCopy",
            "v2",
            True,
            3,
        ),
        _derivation(
            "d-v2-copy-chain",
            "reports.contactCopy",
            "analytics.profile",
            "v2",
            True,
            3,
        ),
        _derivation(
            "d-v2-copy-nontrans",
            "analytics.profile",
            "analytics.safeHash",
            "v2",
            False,
            0,
        ),
    ]
    v2_derivations = [
        _derivation(
            "d-v2-copy-archive",
            "reports.contactCopy",
            "analytics.archiveCopy",
            "v2",
            True,
            3,
        ),
        _derivation(
            "d-v2-phone-audit",
            "customer.phone",
            "analytics.audit",
            "v2",
            False,
            0,
        ),
    ]
    snapshots = [
        {
            "snapshot_version": "v1",
            "document_id": "DOC-MAIN",
            "previous_snapshot": None,
            "rule_version": RULESET_VERSION,
            "field_set_complete": True,
            "arrays": {"orders": 2},
            "fields": [
                _field("customer.id", 3, True),
                _field("customer.phone", 2, True),
                _field("orders[0].contact", 2, True),
                _field("reports.contactCopy", 0, True),
                _field("reports.phoneCopy", 0, True),
                _field("analytics.profile", 0, True),
                _field("analytics.safeHash", 0, True),
                _field("analytics.audit", 0, True),
            ],
            "derivations": v1_derivations,
        },
        {
            "snapshot_version": "v2",
            "document_id": "DOC-MAIN",
            "previous_snapshot": "v1",
            "rule_version": RULESET_VERSION,
            "field_set_complete": True,
            "arrays": {"orders": 2},
            "fields": [
                _field("customer.id", 3, True),
                _field("customer.phone", 2, True),
                _field("orders.0.contact", 2, True),
                _field("reports.contactCopy", 0, True),
                _field("reports.phoneCopy", 0, False),
                _field("analytics.profile", 0, True),
                _field("analytics.safeHash", 0, True),
                _field("analytics.audit", 0, True),
                _field("analytics.archiveCopy", 0, True),
            ],
            "derivations": v2_derivations,
        },
        {
            "snapshot_version": 3,
            "document_id": "DOC-MAIN",
            "previous_snapshot": "v2",
            "rule_version": RULESET_VERSION,
            "field_set_complete": True,
            "arrays": {"orders": 2},
            "fields": [
                _field("customer.id", 3, True),
                _field("orders[0].contact", 2, True),
                _field("reports.contactCopy", 0, True),
                _field("reports.phoneCopy", 0, True),
                _field("analytics.profile", 0, True),
                _field("analytics.safeHash", 0, True),
                _field("analytics.audit", 0, True),
                _field("analytics.archiveCopy", 0, True),
            ],
            "derivations": [],
        },
    ]
    return {
        "schema_version": "1.0",
        "rule_version": RULESET_VERSION,
        "snapshots": snapshots,
    }


def build_edits_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "rule_version": RULESET_VERSION,
        "edits": [
            {
                "seq": 1,
                "document_id": "DOC-MAIN",
                "type": "set_level",
                "path": "reports.phoneCopy",
                "level": 1,
            },
            {
                "seq": 2,
                "document_id": "DOC-MAIN",
                "type": "remove_derivation",
                "id": "d-v1-phone-direct",
            },
            {
                "seq": 3,
                "document_id": "DOC-MAIN",
                "type": "remove_reference",
                "id": "d-v2-phone-audit",
            },
            {
                "seq": 4,
                "document_id": "DOC-MAIN",
                "type": "add_derivation",
                "id": "d-edit-id-audit",
                "source": "customer.id",
                "target": "analytics.audit",
                "version": "v2",
                "transitive": True,
                "version_sync": _sync("v2"),
            },
            {
                "seq": 5,
                "document_id": "DOC-MAIN",
                "type": "change_level",
                "path": "analytics.profile",
                "level": 1,
            },
            {
                "seq": 6,
                "document_id": "DOC-MAIN",
                "type": "set_level",
                "path": "reports.phoneCopy",
                "level": 9,
            },
            {
                "seq": 7,
                "document_id": "DOC-MAIN",
                "type": "add_reference",
                "id": "d-invalid-self",
                "source": "customer.id",
                "target": "customer.id",
                "version": "v2",
                "transitive": True,
            },
            {
                "seq": 8,
                "document_id": "DOC-MAIN",
                "type": "remove_derivation",
                "id": "d-no-such",
            },
            {
                "seq": 9,
                "document_id": "DOC-MAIN",
                "type": "add_derivation",
                "id": "d-invalid-cycle",
                "source": "reports.contactCopy",
                "target": "customer.id",
                "version": "v2",
                "transitive": True,
            },
            {
                "seq": 10,
                "document_id": "DOC-MAIN",
                "type": "set_masked",
                "path": "reports.phoneCopy",
                "masked": False,
            },
            {
                "seq": 11,
                "document_id": "DOC-MAIN",
                "type": "set_masked",
                "path": "missing.field",
                "masked": True,
            },
            {
                "seq": 12,
                "document_id": "DOC-MAIN",
                "type": "change_masked",
                "path": "analytics.audit",
                "masked": False,
            },
            {
                "seq": 13,
                "document_id": "DOC-MAIN",
                "type": "delete_field",
                "path": "customer.id",
            },
            {
                "seq": 14,
                "document_id": "DOC-MAIN",
                "type": "remove_derivation",
                "id": "d-edit-id-audit",
            },
            {
                "seq": 15,
                "document_id": "DOC-MAIN",
                "type": "delete_field",
                "path": "analytics.audit",
            },
            {
                "seq": 16,
                "document_id": "DOC-MAIN",
                "type": "set_masked",
                "path": "reports.contactCopy",
                "masked": True,
            },
        ],
    }


def build_truth_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "rule_version": RULESET_VERSION,
        "semantics": {
            "v1": "lineage and masking edge only; no sensitivity extension",
            "v2_transitive": "sensitivity extends through the edge and subsequent chains",
            "v2_direct": "no sensitivity extension",
        },
        "version_sync": _sync("v2"),
        "expected_by_version": {
            "v1": {
                "effective_levels": {
                    "customer.id": 3,
                    "customer.phone": 2,
                    "orders[0].contact": 2,
                    "reports.contactCopy": 3,
                    "reports.phoneCopy": 0,
                    "analytics.profile": 3,
                    "analytics.safeHash": 0,
                    "analytics.audit": 0,
                },
                "violations": [],
            },
            "v2": {
                "effective_levels": {
                    "customer.id": 3,
                    "customer.phone": 2,
                    "orders[0].contact": 2,
                    "reports.contactCopy": 3,
                    "reports.phoneCopy": 0,
                    "analytics.profile": 3,
                    "analytics.safeHash": 0,
                    "analytics.audit": 0,
                    "analytics.archiveCopy": 3,
                },
                "violations": [
                    {
                        "code": "E_MASK_BYPASS",
                        "field_key": "reports.phoneCopy",
                        "source_key": "customer.phone",
                    }
                ],
            },
            "v3": {
                "effective_levels": {
                    "customer.id": 3,
                    "orders[0].contact": 2,
                    "reports.contactCopy": 3,
                    "reports.phoneCopy": 0,
                    "analytics.profile": 3,
                    "analytics.safeHash": 0,
                    "analytics.audit": 0,
                    "analytics.archiveCopy": 3,
                },
                "violations": [
                    {
                        "code": "E_DANGLING_MASK",
                        "field_key": "analytics.audit",
                        "source_key": "customer.phone",
                    },
                    {
                        "code": "E_DANGLING_MASK",
                        "field_key": "reports.phoneCopy",
                        "source_key": "customer.phone",
                    },
                ],
            },
        },
        "expected_final": {
            "processed_edits": 16,
            "accepted_edits": 9,
            "rejected_edits": 7,
            "effective_levels": {
                "customer.id": 3,
                "orders[0].contact": 2,
                "reports.contactCopy": 3,
                "reports.phoneCopy": 1,
                "analytics.profile": 3,
                "analytics.safeHash": 0,
                "analytics.archiveCopy": 3,
            },
            "deleted_fields": ["analytics.audit", "customer.phone"],
            "violations": [],
            "rejection_codes": [
                "E_LEVEL",
                "E_DERIVE_CYCLE",
                "E_EDIT_DERIVATION",
                "E_DERIVE_CYCLE",
                "E_EDIT_FIELD",
                "E_MASK_BYPASS",
                "E_DANGLING_MASK",
            ],
        },
    }


def build_invalid_snapshots_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "rule_version": RULESET_VERSION,
        "snapshots": [
            {
                "snapshot_version": "v1",
                "document_id": "DOC-INVALID",
                "previous_snapshot": None,
                "arrays": {"items": 2},
                "fields": [
                    _field("good.a", 1, True),
                    _field("bad..path", 1, True),
                    _field("items[9].x", 1, True),
                    _field("bad.level", 9, True),
                    _field("good.a", 2, True),
                    _field("good.b", 0, True),
                ],
                "derivations": [
                    _derivation(
                        "d-invalid-version",
                        "good.a",
                        "good.b",
                        "v3",
                        True,
                        0,
                    ),
                    _derivation(
                        "d-missing-target",
                        "good.a",
                        "no.target",
                        "v1",
                        False,
                        0,
                    ),
                    _derivation(
                        "d-missing-source",
                        "no.source",
                        "good.b",
                        "v1",
                        False,
                        0,
                    ),
                    _derivation(
                        "d-valid-after-errors",
                        "good.a",
                        "good.b",
                        "v2",
                        True,
                        1,
                    ),
                ],
            },
            {
                "snapshot_version": "release-1",
                "document_id": "DOC-BAD-VERSION",
                "fields": [_field("x", 1, True)],
                "derivations": [],
            },
            {
                "snapshot_version": "v1",
                "document_id": "DOC-BAD-VERSION",
                "previous_snapshot": None,
                "fields": [_field("x", 1, True)],
                "derivations": [],
            },
            {
                "snapshot_version": "v1",
                "document_id": "DOC-ORDER",
                "previous_snapshot": None,
                "fields": [_field("x", 1, True)],
                "derivations": [],
            },
            {
                "snapshot_version": "v1",
                "document_id": "DOC-ORDER",
                "previous_snapshot": "v1",
                "fields": [_field("x", 1, True)],
                "derivations": [],
            },
            {
                "snapshot_version": "v1",
                "document_id": "DOC-CONTINUED",
                "previous_snapshot": None,
                "fields": [_field("only.field", 1, True)],
                "derivations": [],
            },
        ],
    }


def build_cycle_snapshots_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "rule_version": RULESET_VERSION,
        "snapshots": [
            {
                "snapshot_version": "v1",
                "document_id": "DOC-CYCLE",
                "previous_snapshot": None,
                "fields": [
                    _field("a", 1, True),
                    _field("b", 0, True),
                    _field("c", 0, True),
                ],
                "derivations": [
                    _derivation("e-ab", "a", "b", "v2", True, 1),
                    _derivation("e-bc", "b", "c", "v2", True, 1),
                    _derivation("e-ca", "c", "a", "v2", True, 1),
                ],
            }
        ],
    }


def write_sample_directory(output: str | Path) -> dict[str, Path]:
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "snapshots": directory / "snapshots.json",
        "edits": directory / "edits.json",
        "truth": directory / "truth.json",
        "invalid_snapshots": directory / "invalid_snapshots.json",
        "cycle_snapshots": directory / "cycle_snapshots.json",
    }
    payloads = {
        "snapshots": build_snapshots_payload(),
        "edits": build_edits_payload(),
        "truth": build_truth_payload(),
        "invalid_snapshots": build_invalid_snapshots_payload(),
        "cycle_snapshots": build_cycle_snapshots_payload(),
    }
    for key, path in files.items():
        path.write_text(
            json.dumps(payloads[key], sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="samples", help="sample output directory")
    args = parser.parse_args(argv)
    files = write_sample_directory(args.out)
    print(json.dumps({name: str(path) for name, path in files.items()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
