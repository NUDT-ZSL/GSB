from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dep_arc_interop.config import section_checksum
from dep_arc_interop.formats import serialize_graph
from dep_arc_interop.models import (
    Arc,
    ArcGraph,
    BidirectionalChain,
    CrossSentenceRef,
    FormatVersion,
    SentenceGraph,
)

SEED = 20261122
DATA_ROOT = ROOT / "data"
SAMPLES = DATA_ROOT / "samples"
CONFIG = DATA_ROOT / "config"
INJECTION = DATA_ROOT / "injection"

POS_TAGS = (
    "ADJ",
    "ADP",
    "ADV",
    "AUX",
    "CCONJ",
    "DET",
    "NOUN",
    "NUM",
    "PART",
    "PRON",
    "PROPN",
    "PUNCT",
    "SCONJ",
    "SYM",
    "VERB",
)

DEPRELS = (
    "acl",
    "advcl",
    "advmod",
    "amod",
    "aux",
    "case",
    "cc",
    "conj",
    "det",
    "mark",
    "nmod",
    "nsubj",
    "nummod",
    "obj",
    "obl",
    "punct",
    "root",
)

RAW_SENTENCES: list[dict[str, Any]] = [
    {
        "sent_id": "S001",
        "tokens": [
            [1, "The", "DET", 3, "det"],
            [2, "quick", "ADJ", 3, "amod"],
            [3, "fox", "NOUN", 4, "nsubj"],
            [4, "jumps", "VERB", 0, "root"],
            [5, "over", "ADP", 8, "case"],
            [6, "the", "DET", 8, "det"],
            [7, "lazy", "ADJ", 8, "amod"],
            [8, "dog", "NOUN", 4, "obl"],
            [9, ".", "PUNCT", 4, "punct"],
        ],
    },
    {
        "sent_id": "S002",
        "tokens": [
            [1, "小明", "PROPN", 4, "nsubj"],
            [2, "在", "ADP", 3, "case"],
            [3, "图书馆", "NOUN", 4, "obl"],
            [4, "阅读", "VERB", 0, "root"],
            [5, "了", "PART", 4, "aux"],
            [6, "三本", "NUM", 7, "nummod"],
            [7, "书", "NOUN", 4, "obj"],
            [8, "。", "PUNCT", 4, "punct"],
        ],
    },
    {
        "sent_id": "S003",
        "tokens": [
            [1, "Alice", "PROPN", 4, "nsubj"],
            [2, "and", "CCONJ", 3, "cc"],
            [3, "Bob", "PROPN", 1, "conj"],
            [4, "like", "VERB", 0, "root"],
            [5, "tea", "NOUN", 4, "obj"],
            [6, "and", "CCONJ", 7, "cc"],
            [7, "coffee", "NOUN", 5, "conj"],
            [8, ".", "PUNCT", 4, "punct"],
        ],
    },
    {
        "sent_id": "S004",
        "tokens": [
            [1, "If", "SCONJ", 3, "mark"],
            [2, "it", "PRON", 3, "nsubj"],
            [3, "rains", "VERB", 6, "advcl"],
            [4, "we", "PRON", 6, "nsubj"],
            [5, "will", "AUX", 6, "aux"],
            [6, "stay", "VERB", 0, "root"],
            [7, "home", "NOUN", 6, "obl"],
            [8, ".", "PUNCT", 6, "punct"],
        ],
    },
    {
        "sent_id": "S005",
        "tokens": [
            [1, "The", "DET", 2, "det"],
            [2, "report", "NOUN", 6, "nsubj"],
            [3, "written", "VERB", 2, "acl"],
            [4, "by", "ADP", 5, "case"],
            [5, "Alice", "PROPN", 3, "obl"],
            [6, "surprised", "VERB", 0, "root"],
            [7, "the", "DET", 8, "det"],
            [8, "team", "NOUN", 6, "obj"],
            [9, ".", "PUNCT", 6, "punct"],
        ],
    },
]


def tokens_as_dicts(sentence: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": token_id,
            "word": word,
            "pos": pos,
            "head_gold": head,
            "deprel_gold": deprel,
        }
        for token_id, word, pos, head, deprel in sentence["tokens"]
    ]


def build_snapshot_payload(
    version: int,
    *,
    pos_values: tuple[str, ...] = POS_TAGS,
    deprel_values: tuple[str, ...] = DEPRELS,
    policy_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pos_section: dict[str, Any] = {
        "version": version,
        "values": sorted(pos_values),
    }
    pos_section["checksum"] = section_checksum(pos_section)
    deprel_section: dict[str, Any] = {
        "version": version,
        "values": sorted(deprel_values),
    }
    deprel_section["checksum"] = section_checksum(deprel_section)
    policy_section: dict[str, Any] = {
        "version": version,
        "defaults": {
            "confidence_low": 0.0,
            "confidence_high": 1.0,
            "confidence_tolerance": 0.000001,
            "forward_check": True,
            "reverse_check": True,
        },
        "discard_rules": {
            "downgrade_to_v1": [
                "root_marker",
                "confidence_low",
                "confidence_high",
                "forward_check",
                "reverse_check",
                "bidirectional_arc_chains",
                "cross_sentence_refs",
            ],
            "downgrade_to_v2": [
                "forward_check",
                "reverse_check",
                "bidirectional_arc_chains",
                "cross_sentence_refs",
            ],
        },
    }
    if policy_extra:
        policy_section.update(policy_extra)
    policy_section["checksum"] = section_checksum(policy_section)
    return {
        "schema": "deparc-controlled-snapshot-v1",
        "generated_by": "scripts/generate_samples.py",
        "seed": SEED,
        "pos_tags": pos_section,
        "deprels": deprel_section,
        "migration_policy": policy_section,
    }


def build_arc_graph(
    version: FormatVersion,
    *,
    confidences: dict[str, dict[int, tuple[float, float]]] | None = None,
    checks: tuple[bool, bool] = (True, True),
    chains: dict[str, list[BidirectionalChain]] | None = None,
    refs: dict[str, list[CrossSentenceRef]] | None = None,
) -> ArcGraph:
    graph = ArcGraph(format_version=version)
    for raw_sentence in RAW_SENTENCES:
        sent_id = raw_sentence["sent_id"]
        arcs: dict[int, Arc] = {}
        root_marker: int | None = None
        for token in tokens_as_dicts(raw_sentence):
            confidence = (
                confidences[sent_id][token["id"]]
                if confidences is not None
                else (0.0, 1.0)
            )
            if token["head_gold"] == 0:
                root_marker = token["id"]
            arc = Arc(
                token_id=token["id"],
                head=token["head_gold"],
                deprel=token["deprel_gold"],
            )
            if version in (FormatVersion.V2, FormatVersion.V3):
                arc.confidence_low = confidence[0]
                arc.confidence_high = confidence[1]
            if version is FormatVersion.V3:
                arc.forward_check = checks[0]
                arc.reverse_check = checks[1]
            arcs[token["id"]] = arc
        graph.add(
            SentenceGraph(
                sent_id=sent_id,
                arcs=arcs,
                root_marker=(
                    root_marker
                    if version in (FormatVersion.V2, FormatVersion.V3)
                    else None
                ),
                root_source=(
                    "observed"
                    if version in (FormatVersion.V2, FormatVersion.V3)
                    else "inferred"
                ),
                bidirectional_chains=(
                    chains.get(sent_id, []) if chains is not None else []
                ),
                cross_sentence_refs=(refs.get(sent_id, []) if refs is not None else []),
            )
        )
    return graph


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def generate() -> dict[str, Any]:
    for directory in (SAMPLES, CONFIG, INJECTION):
        directory.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(SEED)
    raw_lines = [
        json.dumps(
            {
                "sent_id": sentence["sent_id"],
                "tokens": tokens_as_dicts(sentence),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for sentence in RAW_SENTENCES
    ]
    write_text(SAMPLES / "dependency_samples.jsonl", "\n".join(raw_lines) + "\n")
    snapshot_v1 = build_snapshot_payload(1)
    snapshot_v2 = build_snapshot_payload(
        2,
        pos_values=POS_TAGS + ("INTJ",),
        deprel_values=DEPRELS + ("discourse",),
        policy_extra={"replacement_note": "complete atomic snapshot v2"},
    )
    retired_deprels = tuple(
        deprel for deprel in DEPRELS if deprel not in ("conj", "punct")
    )
    snapshot_retired = build_snapshot_payload(
        3,
        pos_values=POS_TAGS + ("INTJ",),
        deprel_values=retired_deprels,
        policy_extra={"replacement_note": "retire conj and punct"},
    )
    snapshot_corrupt = deepcopy(snapshot_v1)
    snapshot_corrupt["deprels"]["checksum"] = "sha256:0000000000000000"
    write_json(CONFIG / "controlled_snapshot_v1.json", snapshot_v1)
    write_json(CONFIG / "controlled_snapshot_v2.json", snapshot_v2)
    write_json(CONFIG / "controlled_snapshot_retired_v3.json", snapshot_retired)
    write_json(CONFIG / "controlled_snapshot_corrupt.json", snapshot_corrupt)

    v1_graph = build_arc_graph(FormatVersion.V1)
    v2_graph = build_arc_graph(FormatVersion.V2)
    v3_graph = build_arc_graph(FormatVersion.V3)
    write_text(SAMPLES / "arcs_v1.txt", serialize_graph(v1_graph, version="v1"))
    write_text(SAMPLES / "arcs_v2.txt", serialize_graph(v2_graph, version="v2"))
    write_text(SAMPLES / "arcs_v3.json", serialize_graph(v3_graph, version="v3"))

    drift_confidences: dict[str, dict[int, tuple[float, float]]] = {}
    for sentence in RAW_SENTENCES:
        sent_id = sentence["sent_id"]
        drift_confidences[sent_id] = {}
        for token_id, *_ in sentence["tokens"]:
            low = round(float(rng.uniform(0.70, 0.94)), 6)
            high = round(low + float(rng.uniform(0.02, 0.05)), 6)
            drift_confidences[sent_id][token_id] = (low, high)
    drift_v3 = build_arc_graph(
        FormatVersion.V3,
        confidences=drift_confidences,
        chains={
            "S001": [
                BidirectionalChain(
                    chain_id="S001:bc:1",
                    token_ids=(3, 4, 5),
                    forward=True,
                    reverse=True,
                )
            ]
        },
        refs={
            "S001": [
                CrossSentenceRef(
                    source_token_id=8,
                    target_sent_id="S002",
                    target_token_id=3,
                    relation="bridging",
                    confidence=round(float(rng.uniform(0.80, 0.95)), 6),
                )
            ]
        },
    )
    write_text(
        INJECTION / "semantic_drift_v3.json",
        serialize_graph(drift_v3, version="v3"),
    )

    write_text(INJECTION / "format_unknown_version.txt", "# format=v9\n")
    write_text(INJECTION / "format_truncated_header.txt", "# format=\n")
    write_text(
        INJECTION / "format_v1_bad_field_count.txt",
        "# format=v1\n# sent_id=B001\n1|0|root|extra\n",
    )
    write_text(
        INJECTION / "format_v2_missing_field.txt",
        (
            "# format=v2\n# sent_id=B002\n# root=1\n"
            "token_id=1|head=0|deprel=root|confidence_low=0.0\n"
        ),
    )
    write_text(
        INJECTION / "format_numeric_unparseable.txt",
        "# format=v1\n# sent_id=B003\nx|0|root\n",
    )
    write_text(
        INJECTION / "semantic_bad_deprel.txt",
        "# format=v1\n# sent_id=B010\n1|0|not_a_deprel\n",
    )
    write_text(
        INJECTION / "semantic_head_range.txt",
        "# format=v1\n# sent_id=B011\n1|0|root\n2|99|obj\n",
    )
    write_text(
        INJECTION / "semantic_two_roots.txt",
        "# format=v1\n# sent_id=B012\n1|0|root\n2|0|root\n",
    )
    write_text(
        INJECTION / "semantic_cycle.txt",
        "# format=v1\n# sent_id=B013\n1|0|root\n2|3|obj\n3|2|nsubj\n",
    )
    write_text(
        INJECTION / "semantic_token_sequence.txt",
        "# format=v1\n# sent_id=B014\n1|0|root\n3|1|obj\n",
    )
    write_text(
        INJECTION / "semantic_duplicate_sent.txt",
        ("# format=v1\n# sent_id=B015\n1|0|root\n# sent_id=B015\n1|0|root\n"),
    )
    write_text(
        INJECTION / "roundtrip_precision_v2.txt",
        (
            "# format=v2\n# sent_id=R001\n# root=1\n"
            "token_id=1|head=0|deprel=root|confidence_low=0.1000000001|"
            "confidence_high=0.9000000001\n"
        ),
    )
    first_raw = raw_lines[0]
    write_text(
        INJECTION / "raw_duplicate_sent.jsonl",
        f"{first_raw}\n{first_raw}\n",
    )
    token_seq = json.loads(first_raw)
    token_seq["sent_id"] = "RAW_SEQ"
    token_seq["tokens"][1]["id"] = 3
    write_text(
        INJECTION / "raw_token_sequence.jsonl",
        json.dumps(token_seq, ensure_ascii=False, separators=(",", ":")) + "\n",
    )
    pos_invalid = json.loads(first_raw)
    pos_invalid["sent_id"] = "RAW_POS"
    pos_invalid["tokens"][0]["pos"] = "UNKNOWN"
    write_text(
        INJECTION / "raw_pos_invalid.jsonl",
        json.dumps(pos_invalid, ensure_ascii=False, separators=(",", ":")) + "\n",
    )

    cases = {
        "generated_by": "scripts/generate_samples.py",
        "seed": SEED,
        "cases": [
            {
                "name": "unknown_version",
                "kind": "format",
                "path": "injection/format_unknown_version.txt",
                "expected_code": "E_VERSION_UNSUPPORTED",
            },
            {
                "name": "truncated_header",
                "kind": "format",
                "path": "injection/format_truncated_header.txt",
                "expected_code": "E_FORMAT_CORRUPT",
            },
            {
                "name": "v1_bad_field_count",
                "kind": "format",
                "path": "injection/format_v1_bad_field_count.txt",
                "expected_code": "E_FORMAT_CORRUPT",
            },
            {
                "name": "v2_missing_field",
                "kind": "format",
                "path": "injection/format_v2_missing_field.txt",
                "expected_code": "E_FORMAT_CORRUPT",
            },
            {
                "name": "numeric_unparseable",
                "kind": "format",
                "path": "injection/format_numeric_unparseable.txt",
                "expected_code": "E_FORMAT_CORRUPT",
            },
            {
                "name": "deprel_invalid",
                "kind": "format",
                "path": "injection/semantic_bad_deprel.txt",
                "expected_code": "E_DEPREL_INVALID",
            },
            {
                "name": "head_range",
                "kind": "format",
                "path": "injection/semantic_head_range.txt",
                "expected_code": "E_HEAD_RANGE",
            },
            {
                "name": "root_violation",
                "kind": "format",
                "path": "injection/semantic_two_roots.txt",
                "expected_code": "E_ROOT_VIOLATION",
            },
            {
                "name": "cycle",
                "kind": "format",
                "path": "injection/semantic_cycle.txt",
                "expected_code": "E_CYCLE_DETECTED",
            },
            {
                "name": "token_sequence",
                "kind": "format",
                "path": "injection/semantic_token_sequence.txt",
                "expected_code": "E_TOKEN_SEQ",
            },
            {
                "name": "duplicate_sent",
                "kind": "format",
                "path": "injection/semantic_duplicate_sent.txt",
                "expected_code": "E_DUP_SENT",
            },
            {
                "name": "raw_duplicate_sent",
                "kind": "raw",
                "path": "injection/raw_duplicate_sent.jsonl",
                "expected_code": "E_DUP_SENT",
            },
            {
                "name": "raw_token_sequence",
                "kind": "raw",
                "path": "injection/raw_token_sequence.jsonl",
                "expected_code": "E_TOKEN_SEQ",
            },
            {
                "name": "raw_pos_invalid",
                "kind": "raw",
                "path": "injection/raw_pos_invalid.jsonl",
                "expected_code": "E_POS_INVALID",
            },
            {
                "name": "resource_limit",
                "kind": "resource",
                "path": "samples/dependency_samples.jsonl",
                "expected_code": "E_RESOURCE_LIMIT",
                "limits": {"max_total_nodes": 20, "max_tokens_per_sentence": 9},
            },
            {
                "name": "semantic_drift",
                "kind": "equivalence",
                "sources": {
                    "v1": "samples/arcs_v1.txt",
                    "v2": "samples/arcs_v2.txt",
                    "v3": "injection/semantic_drift_v3.json",
                },
                "expected_code": "E_SEMANTIC_DRIFT",
            },
            {
                "name": "roundtrip_precision",
                "kind": "roundtrip",
                "path": "injection/roundtrip_precision_v2.txt",
                "confidence_tolerance": 1e-12,
                "expected_code": "E_ROUNDTRIP_MISMATCH",
            },
            {
                "name": "snapshot_corrupt",
                "kind": "snapshot",
                "path": "config/controlled_snapshot_corrupt.json",
                "expected_code": "E_SNAPSHOT_CORRUPT",
            },
            {
                "name": "label_retired",
                "kind": "retirement",
                "path": "config/controlled_snapshot_retired_v3.json",
                "expected_code": "E_LABEL_RETIRED",
            },
            {
                "name": "selfcheck_inconsistent",
                "kind": "selfcheck",
                "sources": {
                    "v1": "samples/arcs_v1.txt",
                    "v2": "samples/arcs_v2.txt",
                    "v3": "injection/semantic_drift_v3.json",
                },
                "expected_code": "E_INCONSISTENT",
            },
        ],
    }
    write_json(INJECTION / "cases.json", cases)
    return {
        "seed": SEED,
        "raw_sentences": len(RAW_SENTENCES),
        "nodes": sum(len(sentence["tokens"]) for sentence in RAW_SENTENCES),
        "snapshot_v1": str((CONFIG / "controlled_snapshot_v1.json").relative_to(ROOT)),
        "snapshot_v2": str((CONFIG / "controlled_snapshot_v2.json").relative_to(ROOT)),
        "v1": str((SAMPLES / "arcs_v1.txt").relative_to(ROOT)),
        "v2": str((SAMPLES / "arcs_v2.txt").relative_to(ROOT)),
        "v3": str((SAMPLES / "arcs_v3.json").relative_to(ROOT)),
        "injection_cases": len(cases["cases"]),
    }


if __name__ == "__main__":
    summary = generate()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
