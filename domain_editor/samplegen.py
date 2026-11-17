"""Deterministic sample and anomaly-fixture generation."""

from __future__ import annotations

import copy
import json
import random
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

from .jsonutil import atomic_write_json, atomic_write_text, dumps_canonical, loads_decimal
from .model import Argument, Document, Event, ProjectState, Relation, Span
from .v1 import V1_ARG_OFFSETS, V1_BASE_WIDTH, V1_OFFSETS, serialize_v1
from .v2 import serialize_v2


def _rule_config() -> dict[str, Any]:
    return {
        "version": 1,
        "max_workers": 4,
        "conversions": {
            "v1_units_per_second": 1000,
            "v1_origin": 0,
            "v2_to_v1_rounding": "half_up",
            "annotation_margin_seconds": 1.0,
            "diagnostic_bins": [0.0, 0.000001, 0.001, 0.01, 0.1, 1.0, 1000000000.0],
        },
        "confidence_bands": [
            {"min": 0.0, "max": 0.499999, "label": "low"},
            {"min": 0.5, "max": 0.749999, "label": "medium"},
            {"min": 0.75, "max": 1.0, "label": "high"},
        ],
        "relation_triggers": {
            "works_for": ["works", "joined", "leads"],
            "located_in": ["in", "at"],
            "acquired": ["acquires", "bought"],
            "founded": ["founded", "established"],
        },
        "event_triggers": {
            "employment": ["works", "joined", "leads"],
            "acquisition": ["acquires", "bought"],
            "founding": ["founded", "established"],
        },
        "event_roles": {
            "employment": ["employee", "employer"],
            "acquisition": [
                "buyer",
                "seller",
                "organization",
                "responsible_organization_department",
            ],
            "founding": ["founder", "organization"],
        },
        "entity_types": ["PERSON", "ORG", "LOCATION"],
        "roles": {
            "employee": {"key": "R_EMPLOYEE", "label": "Employee"},
            "employer": {"key": "R_EMPLOYER", "label": "Employer"},
            "buyer": {"key": "R_BUYER", "label": "Buyer"},
            "seller": {"key": "R_SELLER", "label": "Seller"},
            "organization": {"key": "R_ORGANIZATION", "label": "Organization"},
            "founder": {"key": "R_FOUNDER", "label": "Founder"},
            "responsible_organization_department": {
                "key": "R_RESPONSIBLE_ORGANIZATION_DEPARTMENT",
                "label": "Responsible organization department",
            },
        },
    }


def _span(start: int, end: int) -> Span:
    return Span(Decimal(start), Decimal(end))


def _ann_span(start: int, end: int) -> Span:
    return Span(Decimal(start) / Decimal(1000), Decimal(end) / Decimal(1000))


def _build_documents(seed: int) -> list[Document]:
    rng = random.Random(seed)

    def confidence(low: int = 720, high: int = 940) -> Decimal:
        return Decimal(rng.randint(low, high)) / Decimal(1000)

    docs: list[Document] = []

    doc1 = Document(
        doc_id="doc-001",
        tokens=["Alice", "works", "at", "Acme", "in", "London"],
        token_positions=[_span(0, 5), _span(6, 11), _span(12, 14), _span(15, 19), _span(20, 22), _span(23, 29)],
        metadata={"corpus": f"seed-{seed}", "lang": "en"},
    )
    doc1.relations = [
        Relation("rel-001", "works_for", _ann_span(0, 5), _ann_span(15, 19), confidence(), "works", "v1", 0, {"representation": "permille", "value": int(confidence() * 1000)}, {}),
        Relation("rel-002", "located_in", _ann_span(15, 19), _ann_span(23, 29), confidence(), "in", "v1", 1, {"representation": "permille", "value": int(confidence() * 1000)}, {}),
    ]
    doc1.events = [
        Event(
            "evt-001",
            "employment",
            _ann_span(0, 19),
            confidence(),
            "works",
            [
                Argument("employee", None, _ann_span(0, 5), "rel-001", "PERSON", 0, {}),
                Argument("employer", None, _ann_span(15, 19), "rel-001", "ORG", 1, {}),
            ],
            "v1",
            0,
            {"representation": "permille", "value": int(confidence() * 1000)},
            {},
        )
    ]
    docs.append(doc1)

    doc2 = Document(
        doc_id="doc-002",
        tokens=["Bob", "joined", "Acme", "and", "Carol", "leads", "Globex"],
        token_positions=[_span(0, 3), _span(4, 10), _span(11, 15), _span(16, 19), _span(20, 25), _span(26, 31), _span(32, 38)],
        metadata={"corpus": f"seed-{seed}", "lang": "en"},
    )
    doc2.relations = [
        Relation("rel-003", "works_for", _ann_span(0, 3), _ann_span(11, 15), confidence(), "joined", "v1", 0, {"representation": "permille", "value": int(confidence() * 1000)}, {}),
        Relation("rel-004", "works_for", _ann_span(20, 25), _ann_span(32, 38), confidence(), "leads", "v1", 1, {"representation": "permille", "value": int(confidence() * 1000)}, {}),
    ]
    doc2.events = [
        Event(
            "evt-002",
            "employment",
            _ann_span(0, 15),
            confidence(),
            "joined",
            [
                Argument("employee", None, _ann_span(0, 3), "rel-003", "PERSON", 0, {}),
                Argument("employer", None, _ann_span(11, 15), "rel-003", "ORG", 1, {}),
            ],
            "v1",
            0,
            {"representation": "permille", "value": int(confidence() * 1000)},
            {},
        ),
        Event(
            "evt-003",
            "employment",
            _ann_span(20, 38),
            confidence(),
            "leads",
            [
                Argument("employee", None, _ann_span(20, 25), "rel-004", "PERSON", 0, {}),
                Argument("employer", None, _ann_span(32, 38), "rel-004", "ORG", 1, {}),
            ],
            "v1",
            1,
            {"representation": "permille", "value": int(confidence() * 1000)},
            {},
        ),
    ]
    docs.append(doc2)

    doc3 = Document(
        doc_id="doc-003",
        tokens=["Dana", "acquires", "Initech", "in", "Austin"],
        token_positions=[_span(0, 4), _span(5, 13), _span(14, 21), _span(22, 24), _span(25, 31)],
        metadata={"corpus": f"seed-{seed}", "lang": "en"},
    )
    doc3.relations = [
        Relation("rel-005", "acquired", _ann_span(0, 4), _ann_span(14, 21), confidence(), "acquires", "v1", 0, {"representation": "permille", "value": int(confidence() * 1000)}, {}),
        Relation("rel-006", "located_in", _ann_span(14, 21), _ann_span(25, 31), confidence(), "in", "v1", 1, {"representation": "permille", "value": int(confidence() * 1000)}, {}),
    ]
    doc3.events = [
        Event(
            "evt-004",
            "acquisition",
            _ann_span(0, 21),
            confidence(),
            "acquires",
            [
                Argument("buyer", None, _ann_span(0, 4), "rel-005", "ORG", 0, {}),
                Argument("seller", None, _ann_span(14, 21), "rel-005", "ORG", 1, {}),
            ],
            "v1",
            0,
            {"representation": "permille", "value": int(confidence() * 1000)},
            {},
        )
    ]
    docs.append(doc3)

    doc4 = Document(
        doc_id="doc-004",
        tokens=["Eve", "founded", "Umbrella", "in", "Kyoto"],
        token_positions=[_span(0, 3), _span(4, 11), _span(12, 20), _span(21, 23), _span(24, 29)],
        metadata={"corpus": f"seed-{seed}", "lang": "en"},
    )
    doc4.relations = [
        Relation("rel-007", "founded", _ann_span(0, 3), _ann_span(12, 20), confidence(), "founded", "v1", 0, {"representation": "permille", "value": int(confidence() * 1000)}, {}),
        Relation("rel-008", "located_in", _ann_span(12, 20), _ann_span(24, 29), confidence(), "in", "v1", 1, {"representation": "permille", "value": int(confidence() * 1000)}, {}),
    ]
    doc4.events = [
        Event(
            "evt-005",
            "founding",
            _ann_span(0, 20),
            confidence(),
            "founded",
            [
                Argument("founder", None, _ann_span(0, 3), "rel-007", "PERSON", 0, {}),
                Argument("organization", None, _ann_span(12, 20), "rel-007", "ORG", 1, {}),
            ],
            "v1",
            0,
            {"representation": "permille", "value": int(confidence() * 1000)},
            {},
        )
    ]
    docs.append(doc4)
    return docs


def _document_json(doc: Document) -> dict[str, Any]:
    return {
        "doc_id": doc.doc_id,
        "tokens": doc.tokens,
        "token_positions": [[int(position.start), int(position.end)] for position in doc.token_positions],
        "metadata": doc.metadata,
    }


def _commands(source_version: str, anomaly_shift: bool = False) -> list[dict[str, Any]]:
    return [
        {"op": 1, "command_id": "cmd-001", "type": "load_project", "source_version": source_version},
        {
            "op": 2,
            "command_id": "cmd-002",
            "type": "adjust_trigger",
            "scope": "relation",
            "name": "works_for",
            "action": "add",
            "trigger": "collaborates",
        },
        {
            "op": 3,
            "command_id": "cmd-003",
            "type": "merge_relations",
            "doc_id": "doc-002",
            "entry_ids": ["rel-003", "rel-004"],
        },
        {
            "op": 4,
            "command_id": "cmd-004",
            "type": "split_relation",
            "doc_id": "doc-002",
            "entry_id": "rel-003",
            "boundary_token_index": 4,
        },
        {
            "op": 5,
            "command_id": "cmd-005",
            "type": "shift_offsets",
            "doc_id": "doc-003",
            "amount_seconds": "0.001" if not anomaly_shift else "100",
        },
        {
            "op": 6,
            "command_id": "cmd-006",
            "type": "replace_argument_role",
            "doc_id": "doc-003",
            "event_id": "evt-004",
            "argument_index": 1,
            "role": "organization",
        },
        {"op": 7, "command_id": "cmd-007", "type": "switch_version", "target_version": "v1"},
        {"op": 8, "command_id": "cmd-008", "type": "switch_version", "target_version": "v2"},
        {"op": 9, "command_id": "cmd-009", "type": "exit"},
    ]


def _jsonl(rows: list[dict[str, Any]]) -> str:
    return "".join(dumps_canonical(row, newline=True) for row in rows)


def _normalize_relation_confidence(doc: Document) -> None:
    for relation in doc.relations:
        relation.confidence = (relation.confidence * 1000).to_integral_value() / Decimal(1000)
        relation.original_confidence = {
            "representation": "permille",
            "value": int(relation.confidence * 1000),
        }
    for event in doc.events:
        event.confidence = (event.confidence * 1000).to_integral_value() / Decimal(1000)
        event.original_confidence = {
            "representation": "permille",
            "value": int(event.confidence * 1000),
        }


def _replace_fixed(line: bytes, offset: int, width: int, value: bytes) -> bytes:
    if len(value) > width:
        raise ValueError("injected value does not fit field")
    return line[:offset] + value.ljust(width, b" ") + line[offset + width :]


def _build_anomaly_v1(normal_text: str) -> str:
    lines = [line.encode("utf-8") for line in normal_text.splitlines()]
    output: list[bytes] = []
    for line in lines:
        entry_offset, entry_width = V1_OFFSETS["entry_id"]
        entry_id = line[entry_offset : entry_offset + entry_width].decode("utf-8").strip()
        if entry_id == "rel-001":
            line = line + b"X"
        elif entry_id == "rel-002":
            line = _replace_fixed(line, V1_OFFSETS["confidence_permille"][0], 4, b"1200")
        elif entry_id == "evt-001":
            offset = V1_BASE_WIDTH + V1_ARG_OFFSETS["source_relation"][0]
            line = _replace_fixed(line, offset, V1_ARG_OFFSETS["source_relation"][1], b"missing-rel")
        output.append(line)
    return b"\n".join(output).decode("utf-8") + "\n"


def _build_anomaly_v2(normal_text: str) -> dict[str, Any]:
    value = loads_decimal(normal_text)
    value["role_names"]["R_NOT_ALLOWED"] = "not_in_role_table"
    value["role_names"]["R_LONG_RESPONSIBLE"] = "responsible_organization_department"
    for doc in value["documents"]:
        if doc["doc_id"] == "doc-002":
            for event in doc["events"]:
                if event["entry_id"] == "evt-002":
                    event["arguments"][0]["role"] = "R_NOT_ALLOWED"
        if doc["doc_id"] == "doc-003":
            for event in doc["events"]:
                if event["entry_id"] == "evt-004":
                    event["arguments"].append(
                        {
                            "role": "R_LONG_RESPONSIBLE",
                            "start": Decimal("0.014"),
                            "end": Decimal("0.021"),
                            "source_relation": "rel-005",
                            "entity_type": "ORG",
                        }
                    )
        if doc["doc_id"] == "doc-004":
            for relation in doc["relations"]:
                if relation["entry_id"] == "rel-007":
                    relation["tail"]["start"] = Decimal("0.0124")
                    relation["confidence"] = Decimal("0.8237")
    return value


def _injection_config() -> dict[str, Any]:
    return {
        "schema": "anomaly-injections/1",
        "injections": [
            {
                "id": "INJ-001",
                "kind": "fixed_width_overlong",
                "target": "v1",
                "doc_id": "doc-001",
                "entry_id": "rel-001",
                "expected_code": "E_FORMAT_OVERLONG",
            },
            {
                "id": "INJ-002",
                "kind": "confidence_out_of_range",
                "target": "v1",
                "doc_id": "doc-001",
                "entry_id": "rel-002",
                "expected_code": "E_CONF_RANGE",
            },
            {
                "id": "INJ-003",
                "kind": "dangling_argument",
                "target": "v1",
                "doc_id": "doc-001",
                "entry_id": "evt-001",
                "expected_code": "E_ARG_DANGLING",
            },
            {
                "id": "INJ-004",
                "kind": "shift_out_of_bounds",
                "target": "commands",
                "operation_index": 5,
                "expected_code": "E_EDIT_INVALID",
            },
            {
                "id": "INJ-005",
                "kind": "invalid_role",
                "target": "v2",
                "doc_id": "doc-002",
                "entry_id": "evt-002",
                "expected_code": "E_ROLE_UNKNOWN",
            },
        ],
    }


def generate_samples(output: str | Path, seed: int = 20261117, force: bool = False) -> dict[str, str]:
    target = Path(output)
    if target.exists() and force:
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    anomaly = target / "anomaly"
    anomaly.mkdir(parents=True, exist_ok=True)
    rules = _rule_config()
    documents = _build_documents(seed)
    for document in documents:
        _normalize_relation_confidence(document)
    state = ProjectState(
        source_version="v1",
        source_fingerprint="sample",
        target_version="v1",
        documents={doc.doc_id: doc for doc in documents},
        rule_config=copy.deepcopy(rules),
    )
    state.canonicalize()
    v1_text = serialize_v1(state, rules, rules["max_workers"]).text
    state.target_version = "v2"
    v2_result = serialize_v2(state, rules, rules["max_workers"])
    v2_text = v2_result.text

    atomic_write_text(target / "documents.jsonl", _jsonl([_document_json(doc) for doc in documents]))
    atomic_write_json(target / "rules.json", rules)
    atomic_write_text(target / "project_v1.txt", v1_text)
    atomic_write_text(target / "project_v2.json", v2_text)
    atomic_write_text(target / "commands.jsonl", _jsonl(_commands("v1")))
    atomic_write_text(target / "commands_v2.jsonl", _jsonl(_commands("v2")))
    atomic_write_json(target / "anomaly_injection.json", _injection_config())
    manifest = {
        "schema": "sample-bundle/1",
        "documents": "documents.jsonl",
        "rules": "rules.json",
        "v1": "project_v1.txt",
        "v2": "project_v2.json",
        "commands": "commands.jsonl",
        "injections": "anomaly_injection.json",
        "max_workers": 4,
        "default_source_version": "v1",
    }
    atomic_write_json(target / "manifest.json", manifest)
    manifest_v2 = dict(manifest)
    manifest_v2["commands"] = "commands_v2.jsonl"
    manifest_v2["default_source_version"] = "v2"
    atomic_write_json(target / "manifest_v2.json", manifest_v2)

    atomic_write_text(anomaly / "project_v1.anomaly.txt", _build_anomaly_v1(v1_text))
    atomic_write_json(anomaly / "project_v2.anomaly.json", _build_anomaly_v2(v2_text))
    atomic_write_text(anomaly / "commands_v1.anomaly.jsonl", _jsonl(_commands("v1", anomaly_shift=True)))
    atomic_write_text(anomaly / "commands_v2.anomaly.jsonl", _jsonl(_commands("v2", anomaly_shift=True)))
    atomic_write_json(anomaly / "anomaly_injection.json", _injection_config())
    anomaly_manifest_v1 = {
        "schema": "sample-bundle/1",
        "documents": "../documents.jsonl",
        "rules": "../rules.json",
        "v1": "project_v1.anomaly.txt",
        "v2": "project_v2.anomaly.json",
        "commands": "commands_v1.anomaly.jsonl",
        "injections": "anomaly_injection.json",
        "max_workers": 4,
        "default_source_version": "v1",
    }
    anomaly_manifest_v2 = dict(anomaly_manifest_v1)
    anomaly_manifest_v2["commands"] = "commands_v2.anomaly.jsonl"
    anomaly_manifest_v2["default_source_version"] = "v2"
    atomic_write_json(anomaly / "manifest_v1.json", anomaly_manifest_v1)
    atomic_write_json(anomaly / "manifest_v2.json", anomaly_manifest_v2)
    return {
        "samples": str(target),
        "manifest": str(target / "manifest.json"),
        "anomaly_manifest_v1": str(anomaly / "manifest_v1.json"),
        "anomaly_manifest_v2": str(anomaly / "manifest_v2.json"),
        "seed": str(seed),
    }
