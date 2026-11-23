from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from sparse_recall.ingest import ingest_dataset
from sparse_recall.sample_data import generate_dataset
from sparse_recall.scheduler import SchedulerConfig
from sparse_recall.weights import load_weight_table


def make_dataset(
    *,
    doc_count: int = 72,
    doc_batches: int = 6,
    query_count: int = 30,
    query_batches: int = 3,
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temporary = tempfile.TemporaryDirectory(prefix="sparse-recall-test-")
    root = Path(temporary.name) / "data"
    generate_dataset(
        root,
        doc_count=doc_count,
        doc_batches=doc_batches,
        query_count=query_count,
        query_batches=query_batches,
    )
    return temporary, root


def load_inputs(root: Path):
    weights = load_weight_table(str(root / "term_weights.json"))
    ingested = ingest_dataset(root / "documents.jsonl", root / "queries.jsonl", weights)
    return weights, ingested


def scheduler_config(
    ingested,
    *,
    slots: int = 2,
    fma_budget: int = 26000,
    memory_budget: int = 20000,
    workers: int = 2,
    chunk_size: int = 8,
) -> SchedulerConfig:
    average_terms = (
        sum(len(document.terms) for document in ingested.documents)
        / len(ingested.documents)
        if ingested.documents
        else 1.0
    )
    return SchedulerConfig(
        slots=slots,
        fma_budget=fma_budget,
        memory_budget=memory_budget,
        worker_shards=workers,
        chunk_size=chunk_size,
        query_candidate_docs=len(ingested.documents),
        avg_doc_nnz=max(1, round(average_terms * 3)),
    )


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
