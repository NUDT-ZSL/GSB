"""Deterministic local sample generation.

Everything is produced from :class:`random.Random` instances seeded with a
fixed integer, so repeated generation yields byte-identical JSONL/JSON files.
No network access and no external corpora are involved.

Vocabularies are deliberately structured so that each source term has known
cross-lingual bridge targets: term ``<lang>:t0007`` bridges to terms sharing
the same numeric id in the other languages.  This keeps the generated
retrieval chain meaningful while remaining fully synthetic.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .canonical import canonical_dumps
from .model import BridgeResource, Document, Query, write_jsonl
from .model import DocTerm


DEFAULT_SEED = 20261124
LANGUAGES = ("en", "de", "zh")
VOCAB_SIZE = 24
DOCS_PER_LANG = 24


@dataclass(frozen=True)
class GeneratedInputs:
    documents: List[Document]
    queries: List[Query]
    bridge: BridgeResource


def vocabulary(lang: str) -> List[str]:
    if lang == "zh":
        return [f"zh:词{index:04d}" for index in range(VOCAB_SIZE)]
    return [f"{lang}:t{index:04d}" for index in range(VOCAB_SIZE)]


def _term_id(term: str) -> int:
    head = term.split(":")[-1]
    digits = "".join(ch for ch in head if ch.isdigit())
    return int(digits)


def generate_documents(seed: int) -> List[Document]:
    rng = random.Random(seed + 101)
    documents: List[Document] = []
    for lang in LANGUAGES:
        vocab = vocabulary(lang)
        for index in range(DOCS_PER_LANG):
            doc_id = f"doc-{lang}-{index:03d}"
            term_count = rng.randint(6, 12)
            chosen = rng.sample(vocab, term_count)
            terms = []
            total_len = 0
            for term in sorted(chosen):
                tf = rng.randint(1, 5)
                terms.append(DocTerm(term=term, tf=tf))
                total_len += tf
            documents.append(
                Document(
                    doc_id=doc_id,
                    doc_lang=lang,
                    terms=terms,
                    doc_len=total_len,
                )
            )
    return documents


def generate_queries(seed: int) -> List[Query]:
    rng = random.Random(seed + 202)
    queries: List[Query] = []
    for index in range(12):
        source_lang = LANGUAGES[index % len(LANGUAGES)]
        vocab = vocabulary(source_lang)
        term_count = rng.randint(2, 3)
        chosen = sorted(rng.sample(vocab, term_count))
        queries.append(
            Query(
                query_id=f"q-{index:03d}",
                query_lang=source_lang,
                terms=chosen,
                top_k=5,
            )
        )
    return queries


def generate_bridge(seed: int, version: int = 1) -> BridgeResource:
    rng = random.Random(seed + 303)
    directions: List[Tuple[str, str]] = []
    for source in LANGUAGES:
        for target in LANGUAGES:
            if source != target:
                directions.append((source, target))

    entries: Dict[Tuple[str, str], List[Tuple[str, str, float]]] = {}
    confidence_grid = (0.95, 0.82, 0.67, 0.55, 0.43)
    for source in LANGUAGES:
        for term in vocabulary(source):
            term_id = _term_id(term)
            mappings: List[Tuple[str, str, float]] = []
            for target in LANGUAGES:
                if target == source:
                    continue
                target_vocab = vocabulary(target)
                # Primary aligned target plus one neighbour candidate.
                primary = target_vocab[term_id % VOCAB_SIZE]
                neighbour = target_vocab[(term_id + 1) % VOCAB_SIZE]
                mappings.append((target, primary, confidence_grid[rng.randrange(3)]))
                mappings.append((target, neighbour, confidence_grid[rng.randrange(3, 5)]))
            entries[(source, term)] = sorted(mappings)

    resource = BridgeResource(version=version, directions=sorted(directions), entries=entries)
    return resource


def generate_inputs(seed: int = DEFAULT_SEED) -> GeneratedInputs:
    documents = generate_documents(seed)
    queries = generate_queries(seed)
    bridge = generate_bridge(seed)
    return GeneratedInputs(documents=documents, queries=queries, bridge=bridge)


def write_inputs(output_dir, seed: int = DEFAULT_SEED) -> Dict[str, str]:
    from .snapshot import attach_checksum

    generated = generate_inputs(seed)
    attach_checksum(generated.bridge)

    docs_path = output_dir / "documents.jsonl"
    queries_path = output_dir / "queries.jsonl"
    bridge_path = output_dir / "bridge_v1.json"

    write_jsonl(docs_path, [document.to_dict() for document in generated.documents])
    write_jsonl(queries_path, [query.to_dict() for query in generated.queries])
    with open(bridge_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_dumps(generated.bridge.to_dict()))
        handle.write("\n")

    manifest = {
        "seed": seed,
        "languages": list(LANGUAGES),
        "document_count": len(generated.documents),
        "query_count": len(generated.queries),
        "files": {
            "documents": str(docs_path),
            "queries": str(queries_path),
            "bridge": str(bridge_path),
        },
    }
    with open(output_dir / "manifest.json", "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, sort_keys=True, indent=2)
    return manifest
