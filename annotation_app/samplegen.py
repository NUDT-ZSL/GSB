"""Deterministic local sample and versioned asset generation."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from .lineformat import export_line
from .migration import downgrade_v2_to_v1
from .model import Confidence, CorefGroup, Document, Edge, Node, V1Sample, V2Sample
from .ontology import (
    OntologySnapshot,
    make_config_dict,
    make_ontology_dict,
)
from .reader import parse_v2
from .storage import atomic_write_json, atomic_write_bytes


FIXED_SEED = 20241120


def _span(text: str, needle: str, occurrence: int = 1) -> tuple[int, int]:
    cursor = 0
    found = -1
    for _ in range(occurrence):
        found = text.find(needle, cursor)
        if found < 0:
            raise ValueError(f"{needle!r} occurrence {occurrence} not found")
        cursor = found + len(needle)
    return found, found + len(needle)


def generate_v2_sample(seed: int = FIXED_SEED) -> V2Sample:
    rng = random.Random(seed)
    people = ["张伟", "李娜", "王芳", "赵磊", "陈晨"]
    organizations = ["星河科技", "云岭数据", "远航智能", "北辰系统"]
    institutes = ["星河研究院", "云岭实验室", "远航研究所", "北辰中心"]
    projects = ["统一协作平台", "城市感知网络", "可信数据空间"]
    job_titles = ["研究员", "架构师", "项目经理"]

    person_one, person_two = rng.sample(people, 2)
    org_one, org_two = rng.sample(organizations, 2)
    institute = rng.choice(institutes)
    project = rng.choice(projects)
    job_title = rng.choice(job_titles)
    year = "二〇二四年"
    text = (
        f"{person_one}于{year}加入{org_one}，担任{job_title}。"
        f"{person_two}随后入职{institute}，继续负责{project}。"
    )
    canonical_org = f"org:{org_one}"

    def node_span(needle: str, occurrence: int = 1) -> tuple[int, int, str]:
        start, end = _span(text, needle, occurrence)
        return start, end, text[start:end]

    entity_specs = [
        ("e1", "PERSON", person_one, 1, f"person:{person_one}"),
        ("e2", "ORG", org_one, 1, f"org:{org_one}"),
        ("e3", "PERSON", person_two, 1, f"person:{person_two}"),
        ("e4", "ORG", institute, 1, canonical_org),
        ("e5", "PROJECT", project, 1, f"project:{project}"),
        ("e6", "JOB_TITLE", job_title, 1, f"job_title:{job_title}"),
    ]
    trigger_specs = [
        ("t1", "JOIN", "加入", 1, "evt-hire-1"),
        ("t2", "JOIN", "入职", 1, "evt-hire-1"),
    ]
    nodes: list[Node] = []
    confidence_cycle = [
        Confidence(0.96, 0.99),
        Confidence(0.91, 0.97),
        Confidence(0.88, 0.95),
    ]
    for index, (entity_id, entity_type, raw, occurrence, canonical_id) in enumerate(
        entity_specs
    ):
        start, end, mention = node_span(raw, occurrence)
        nodes.append(
            Node(
                id=entity_id,
                kind="entity",
                type=entity_type,
                start=start,
                end=end,
                text=mention,
                confidence=confidence_cycle[index % len(confidence_cycle)],
                canonical_id=canonical_id,
            )
        )
    for index, (trigger_id, trigger_type, raw, occurrence, event_id) in enumerate(
        trigger_specs
    ):
        start, end, mention = node_span(raw, occurrence)
        nodes.append(
            Node(
                id=trigger_id,
                kind="trigger",
                type=trigger_type,
                start=start,
                end=end,
                text=mention,
                confidence=confidence_cycle[(index + 1) % len(confidence_cycle)],
                event_id=event_id,
            )
        )

    edges = [
        Edge(
            id="r1",
            kind="relation",
            type="WORKS_AT",
            source="e1",
            target="e2",
            confidence=Confidence(0.90, 0.97),
        ),
        Edge(
            id="r2",
            kind="relation",
            type="WORKS_AT",
            source="e3",
            target="e4",
            confidence=Confidence(0.89, 0.96),
        ),
        Edge(
            id="r3",
            kind="relation",
            type="OPERATES",
            source="e4",
            target="e5",
            confidence=Confidence(0.83, 0.94),
        ),
    ]
    argument_specs = [
        ("a1", "employee", "e1", "t1"),
        ("a2", "employer", "e2", "t1"),
        ("a3", "position", "e6", "t1"),
        ("a4", "employee", "e3", "t2"),
        ("a5", "employer", "e4", "t2"),
        ("a6", "project", "e5", "t2"),
    ]
    trigger_event = {trigger_id: event for trigger_id, _, _, _, event in trigger_specs}
    for index, (argument_id, role, entity_id, trigger_id) in enumerate(argument_specs):
        edges.append(
            Edge(
                id=argument_id,
                kind="argument",
                role=role,
                source=entity_id,
                target=trigger_id,
                event_id=trigger_event[trigger_id],
                confidence=Confidence(0.86 + index * 0.01, 0.95 + index * 0.005),
            )
        )

    sample = V2Sample(
        sample_id=f"sample-{seed}",
        documents=[
            Document(
                doc_id="doc-1",
                text=text,
                nodes=nodes,
                edges=edges,
            )
        ],
        coref=[
            CorefGroup(
                id="c1",
                canonical_id=canonical_org,
                members=["e2", "e4"],
            )
        ],
        metadata={
            "seed": seed,
            "generator": "local-deterministic",
            "source": "fixed-seed",
        },
    )
    return sample


def make_ontology_v1() -> dict[str, Any]:
    return make_ontology_dict(
        version=1,
        entity_types=["PERSON", "ORG", "PROJECT", "JOB_TITLE", "DATE"],
        argument_roles=["employee", "employer", "position", "project", "time"],
        relation_types=["WORKS_AT", "OPERATES", "SUBSIDIARY_OF"],
        migration_policy={
            "v1_to_v2": {
                "canonical_id_strategy": "stable_type_text",
                "canonical_id_overrides": {},
                "role_map": {"worker": "employee"},
                "unknown_role_policy": "reject",
                "default_node_confidence": [1.0, 1.0],
                "default_edge_confidence": [1.0, 1.0],
            },
            "v2_to_v1": {
                "coref": "drop",
                "multi_trigger_event": "split",
                "confidence": "drop",
                "canonical_id": "drop",
            },
        },
        component_versions={
            "entity_types": 1,
            "argument_roles": 1,
            "relation_types": 1,
            "migration_policy": 1,
        },
        shrink_policy="lenient",
    )


def make_ontology_v2() -> dict[str, Any]:
    return make_ontology_dict(
        version=2,
        entity_types=["PERSON", "ORG", "JOB_TITLE", "DATE"],
        argument_roles=["employee", "employer", "position", "time"],
        relation_types=["WORKS_AT", "SUBSIDIARY_OF"],
        migration_policy={
            "v1_to_v2": {
                "canonical_id_strategy": "stable_type_text",
                "canonical_id_overrides": {},
                "role_map": {"worker": "employee", "project": "position"},
                "unknown_role_policy": "reject",
                "default_node_confidence": [1.0, 1.0],
                "default_edge_confidence": [1.0, 1.0],
            },
            "v2_to_v1": {
                "coref": "drop",
                "multi_trigger_event": "split",
                "confidence": "drop",
                "canonical_id": "drop",
            },
        },
        component_versions={
            "entity_types": 2,
            "argument_roles": 2,
            "relation_types": 2,
            "migration_policy": 2,
        },
        shrink_policy="strict",
    )


def make_config_v1() -> dict[str, Any]:
    return make_config_dict(
        version=1,
        ontology_contract_mode="strict",
        limits={
            "max_import_bytes": 1048576,
            "max_annotation_total": 200,
            "max_graph_nodes": 100,
            "on_limit": "reject",
        },
    )


def write_assets(root: Path) -> dict[str, Any]:
    """Generate committed samples and snapshots from the fixed seed."""

    sample_v2 = generate_v2_sample(FIXED_SEED)
    # Validate the generated object before persisting it.
    ontology = OntologySnapshot.from_dict(make_ontology_v1())
    sample_v2 = parse_v2(sample_v2.to_dict(), ontology=ontology)
    downgraded = downgrade_v2_to_v1(sample_v2, ontology)
    sample_v1 = downgraded.sample
    assert isinstance(sample_v1, V1Sample)

    samples_dir = root / "samples"
    snapshots_dir = root / "snapshots"
    cache_dir = root / "cache"
    for directory in (samples_dir, snapshots_dir, cache_dir):
        directory.mkdir(parents=True, exist_ok=True)

    atomic_write_json(samples_dir / "sample-v1.json", sample_v1.to_dict())
    atomic_write_json(samples_dir / "sample-v2.json", sample_v2.to_dict())
    line_text = export_line(sample_v2, ontology=ontology)
    atomic_write_bytes(samples_dir / "sample.line", line_text.encode("utf-8"))
    atomic_write_json(snapshots_dir / "ontology-v1.json", make_ontology_v1())
    atomic_write_json(snapshots_dir / "ontology-v2.json", make_ontology_v2())
    atomic_write_json(snapshots_dir / "config-v1.json", make_config_v1())
    return {
        "seed": FIXED_SEED,
        "sample_id": sample_v2.sample_id,
        "text": sample_v2.documents[0].text,
        "v1": str(samples_dir / "sample-v1.json"),
        "v2": str(samples_dir / "sample-v2.json"),
        "line": str(samples_dir / "sample.line"),
        "ontology_version": ontology.version,
        "ontology_checksum": ontology.checksum_value,
        "downgrade_report": downgraded.report.to_dict(),
    }


if __name__ == "__main__":
    import json
    import sys

    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    print(json.dumps(write_assets(root), ensure_ascii=False, indent=2, sort_keys=True))

