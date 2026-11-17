"""Deterministic local sample generator.

Runs fully offline with a fixed random seed (``SEED = 20261118``) and writes a
multi-version document chain plus an explicit ``markers.json`` ground-truth
file.  The markers are the single source of truth used by the pytest suite:
tests only compare normalised tracking results against these markers.

The data is synthetic; the ID/phone/card numbers are fabricated constants.
"""

from __future__ import annotations

import json
import os
import random
from typing import Any, Dict, List

SEED = 20261118


def _field(path: str, sensitivity: str) -> Dict[str, str]:
    return {"path": path, "sensitivity": sensitivity}


def _derive(source: str, target: str, decl_version: str,
            transitive: bool = False) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "source": source,
        "target": target,
        "decl_version": decl_version,
    }
    if decl_version == "v2":
        record["transitive"] = transitive
    return record


def _version_100() -> Dict[str, Any]:
    data = {
        "profile": {
            "name": "ZHANG SAN",
            "id_number": "11010119900307001X",
            "phone": "13800000001",
            "id_mask": "110101********001X",
            "phone_mask": "138****0001",
            "id_hash": "hash-0001",
            "audit_tag": "tag-0001",
        },
        "payments": [
            {"card_no": "6222020000000001", "bank": "BANK-A",
             "card_mask": "6222 **** **** 0001"},
        ],
    }
    fields = [
        _field("profile.name", "plain"),
        _field("profile.id_number", "sensitive"),
        _field("profile.phone", "sensitive"),
        _field("payments[0].card_no", "sensitive"),
        _field("profile.id_mask", "masked"),
        _field("profile.phone_mask", "masked"),
        _field("payments[0].card_mask", "masked"),
        _field("profile.id_hash", "masked"),
        _field("profile.audit_tag", "plain"),
    ]
    derivations = [
        _derive("profile.id_number", "profile.id_mask", "v1"),
        _derive("profile.phone", "profile.phone_mask", "v1"),
        _derive("payments[0].card_no", "payments[0].card_mask", "v2", True),
        _derive("profile.id_number", "profile.id_hash", "v2", True),
        _derive("profile.id_hash", "profile.audit_tag", "v2", True),
    ]
    return {
        "doc_id": "customer-profile",
        "version": "1.0.0",
        "data": data,
        "fields": fields,
        "derivations": derivations,
        "changes": ["initial profile snapshot"],
        "markers": ["baseline", "mixed_decl_v1_v2"],
    }


def _version_110(rng: random.Random) -> Dict[str, Any]:
    data = {
        "profile": {
            "name": "ZHANG SAN",
            "id_number": "11010119900307001X",
            "phone": "13800000001",
            "id_mask": "110101********001X",
            "phone_mask": "138****0001",
            "id_hash": "hash-0001",
            "audit_tag": "tag-0001",
        },
        "payments": [
            {"card_no": "6222020000000001", "bank": "BANK-A",
             "card_mask": "6222 **** **** 0001"},
            {"card_no": "6222020000000002", "bank": "BANK-B",
             "card_mask": "6222 **** **** 0002"},
        ],
        "contacts": [
            {"phone": "13800000002"},
        ],
    }
    fields = [
        _field("profile.name", "plain"),
        _field("profile.id_number", "sensitive"),
        _field("profile.phone", "sensitive"),
        _field("payments[0].card_no", "sensitive"),
        _field("payments[1].card_no", "sensitive"),
        _field("profile.id_mask", "masked"),
        _field("profile.phone_mask", "masked"),
        _field("payments[0].card_mask", "masked"),
        _field("payments[1].card_mask", "masked"),
        _field("profile.id_hash", "masked"),
        _field("profile.audit_tag", "masked"),
        _field("contacts[0].phone", "sensitive"),
    ]
    derivations = [
        _derive("profile.id_number", "profile.id_mask", "v1"),
        _derive("profile.phone", "profile.phone_mask", "v1"),
        _derive("payments[0].card_no", "payments[0].card_mask", "v2", True),
        _derive("payments[1].card_no", "payments[1].card_mask", "v2", True),
        _derive("profile.id_number", "profile.id_hash", "v2", True),
        _derive("profile.id_hash", "profile.audit_tag", "v2", True),
        # Cross-version derivation whose source spelling differs but normalises
        # to the same canonical key as the 1.0.0 array field.
        _derive("payments[0].card_no", "profile.audit_tag", "v1"),
        _derive("contacts[0].phone", "profile.phone_mask", "v2", True),
    ]
    rng.shuffle(derivations)  # order must not affect tracking conclusions
    return {
        "doc_id": "customer-profile",
        "version": "1.1.0",
        "data": data,
        "fields": fields,
        "derivations": derivations,
        "changes": ["add second payment card and contact phone"],
        "markers": ["address_format_swap", "shuffled_derivations"],
    }


def _version_200() -> Dict[str, Any]:
    data = {
        "profile": {
            "name": "ZHANG SAN",
            "id_number": "11010119900307001X",
            "phone": "13800000001",
            "phone_mask": "138****0001",
            "id_hash": "hash-0001",
            # id_mask deliberately deleted while a derivation still uses it.
            "audit_tag": "tag-0001",
            "leak_field": "11010119900307001X",
        },
        "payments": [
            {"card_no": "6222020000000001", "bank": "BANK-A",
             "card_mask": "6222 **** **** 0001"},
            {"card_no": "6222020000000002", "bank": "BANK-B",
             "card_mask": "6222 **** **** 0002"},
        ],
        "contacts": [
            {"phone": "13800000002"},
        ],
    }
    fields = [
        _field("profile.name", "plain"),
        _field("profile.id_number", "sensitive"),
        _field("profile.phone", "sensitive"),
        _field("payments[0].card_no", "sensitive"),
        _field("payments[1].card_no", "sensitive"),
        _field("profile.phone_mask", "masked"),
        _field("payments[0].card_mask", "masked"),
        _field("payments[1].card_mask", "masked"),
        _field("profile.id_hash", "masked"),
        _field("profile.audit_tag", "masked"),
        _field("contacts[0].phone", "sensitive"),
        _field("profile.leak_field", "plain"),  # bypass target
    ]
    derivations = [
        _derive("profile.phone", "profile.phone_mask", "v1"),
        _derive("payments[0].card_no", "payments[0].card_mask", "v2", True),
        _derive("payments[1].card_no", "payments[1].card_mask", "v2", True),
        _derive("profile.id_hash", "profile.audit_tag", "v2", True),
        # Mask bypass: sensitive source -> plaintext downstream.
        _derive("profile.id_number", "profile.leak_field", "v2", True),
        # Dangling: id_mask was removed in this snapshot but still referenced.
        _derive("profile.id_number", "profile.id_mask", "v1"),
    ]
    return {
        "doc_id": "customer-profile",
        "version": "2.0.0",
        "data": data,
        "fields": fields,
        "derivations": derivations,
        "changes": ["delete id_mask", "add plaintext leak field"],
        "markers": ["mask_bypass", "dangling_derive"],
    }


def _invalid_documents() -> List[Dict[str, Any]]:
    bad_version = {
        "doc_id": "bad-version",
        "version": "1.0",  # invalid: needs MAJOR.MINOR.PATCH
        "data": {},
        "fields": [],
        "derivations": [],
    }
    unknown_decl = {
        "doc_id": "bad-decl",
        "version": "3.0.0",
        "data": {"a": {"b": 1}},
        "fields": [_field("a.b", "plain")],
        "derivations": [
            {"source": "a.b", "target": "a.b", "decl_version": "v3"}
        ],
    }
    out_of_range = {
        "doc_id": "bad-bound",
        "version": "3.0.1",
        "data": {"items": [{"v": 1}]},
        "fields": [
            _field("items[0].v", "plain"),
            _field("items[5].v", "sensitive"),  # index out of range
            _field("a..b", "plain"),           # malformed addressing
        ],
        "derivations": [],
    }
    return [bad_version, unknown_decl, out_of_range]


def _markers() -> Dict[str, Any]:
    return {
        "seed": SEED,
        "valid_versions": ["1.0.0", "1.1.0", "2.0.0"],
        "invalid_sources": [
            "sample_bad_version.json",
            "sample_bad_decl.json",
            "sample_bad_bound.json",
        ],
        "expected_new_fields": {
            "1.1.0": [
                "contacts[0].phone",
                "payments[1].card_mask",
                "payments[1].card_no",
            ],
            "2.0.0": ["profile.leak_field"],
        },
        "expected_removed_fields": {
            "1.1.0": [],
            "2.0.0": ["profile.id_mask"],
        },
        "canonical_identity": {
            # Different textual spellings share one canonical key.
            "payments[0].card_no": ["payments[0].card_no"],
            "profile.id_number": ["profile.id_number"],
        },
        "expected_effective": {
            "1.0.0": {
                "profile.id_mask": "sensitive",
                "profile.phone_mask": "sensitive",
                "payments[0].card_mask": "sensitive",
                "profile.id_hash": "sensitive",
                "profile.audit_tag": "sensitive",
            },
            "1.1.0": {
                "profile.audit_tag": "sensitive",
                "payments[1].card_mask": "sensitive",
            },
            "2.0.0": {
                "profile.leak_field": "sensitive",
            },
        },
        "expected_violations": [
            {
                "code": "E_MASK_BYPASS",
                "version": "2.0.0",
                "field": "profile.leak_field",
            },
            {
                "code": "E_DANGLING_DERIVE",
                "version": "2.0.0",
                "field": "profile.id_mask",
            },
            {
                "code": "E_VERSION",
                "version": "3.0.0",
            },
            {
                "code": "E_FIELD",
                "version": "3.0.1",
            },
        ],
        "v1_non_transitive_chain": [
            "profile.id_number",
            "profile.id_mask",
        ],
    }


def generate_samples(output_dir: str) -> Dict[str, str]:
    """Write all deterministic sample files and return their paths."""

    rng = random.Random(SEED)
    os.makedirs(output_dir, exist_ok=True)
    documents: Dict[str, Dict[str, Any]] = {
        "sample_1.0.0.json": _version_100(),
        "sample_1.1.0.json": _version_110(rng),
        "sample_2.0.0.json": _version_200(),
    }
    for name, document in list(_invalid_documents_named().items()):
        documents[name] = document

    written: Dict[str, str] = {}
    for name, document in sorted(documents.items()):
        path = os.path.join(output_dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
        written[name] = path

    markers_path = os.path.join(output_dir, "markers.json")
    with open(markers_path, "w", encoding="utf-8") as handle:
        json.dump(_markers(), handle, ensure_ascii=False, indent=2, sort_keys=True)
    written["markers.json"] = markers_path
    return written


def _invalid_documents_named() -> Dict[str, Dict[str, Any]]:
    bad_version, unknown_decl, out_of_range = _invalid_documents()
    return {
        "sample_bad_version.json": bad_version,
        "sample_bad_decl.json": unknown_decl,
        "sample_bad_bound.json": out_of_range,
    }
