"""Local spawn process-pool execution with deterministic task ordering."""

from __future__ import annotations

import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Iterable, Sequence

from .inverted import chunk_documents
from .models import Document
from .weights import WeightTable
from .workers import worker_entry


def _document_rows(documents: Sequence[Document]) -> list[dict[str, Any]]:
    return [document.to_dict() for document in documents]


def build_statistics_tasks(
    documents: Sequence[Document],
    *,
    weights: WeightTable,
    chunk_size: int,
    compensated: bool,
    decimals: int,
    injected_failure: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not documents:
        return []
    batch_no = documents[0].batch_no
    tasks: list[dict[str, Any]] = []
    for chunk_index, chunk in enumerate(chunk_documents(documents, chunk_size)):
        tasks.append(
            {
                "operation": "statistics",
                "kind": "doc",
                "batch_key": f"doc:{batch_no}",
                "batch_no": batch_no,
                "chunk_index": chunk_index,
                "documents": _document_rows(chunk),
                "weights": weights,
                "compensated": compensated,
                "decimals": decimals,
                "injected_failure": injected_failure,
            }
        )
    return tasks


def build_projection_tasks(
    documents: Sequence[Document],
    *,
    weights: WeightTable,
    chunk_size: int,
    decimals: int,
    subnormal_floor: float,
    injected_failure: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not documents:
        return []
    batch_no = documents[0].batch_no
    tasks: list[dict[str, Any]] = []
    for chunk_index, chunk in enumerate(chunk_documents(documents, chunk_size)):
        tasks.append(
            {
                "operation": "project",
                "kind": "doc",
                "batch_key": f"doc:{batch_no}",
                "batch_no": batch_no,
                "chunk_index": chunk_index,
                "documents": _document_rows(chunk),
                "weights": weights,
                "decimals": decimals,
                "subnormal_floor": subnormal_floor,
                "injected_failure": injected_failure,
            }
        )
    return tasks


class LocalWorkerPool:
    """A reusable spawn pool; serial mode exists only for focused unit tests."""

    def __init__(self, max_workers: int, *, use_process_pool: bool = True) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self.max_workers = max_workers
        self.use_process_pool = use_process_pool
        self._pool: ProcessPoolExecutor | None = None
        self.start_method = "spawn" if use_process_pool else "serial"

    def __enter__(self) -> "LocalWorkerPool":
        if self.use_process_pool:
            context = multiprocessing.get_context("spawn")
            self._pool = ProcessPoolExecutor(
                max_workers=self.max_workers,
                mp_context=context,
            )
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=False)
            self._pool = None

    def map(self, tasks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        materialized = list(tasks)
        if not materialized:
            return []
        if self._pool is None:
            return [worker_entry(task) for task in materialized]
        return list(self._pool.map(worker_entry, materialized, chunksize=1))


def merge_worker_envelopes(
    envelopes: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return successful payloads and one aggregated error per failed batch."""

    successes: list[dict[str, Any]] = []
    failures: dict[str, dict[str, Any]] = {}
    for envelope in envelopes:
        batch_key = str(envelope.get("batch_key"))
        if envelope.get("ok"):
            successes.append(envelope)
        else:
            failures.setdefault(batch_key, envelope["error"])
    successes.sort(
        key=lambda item: (
            str(item["batch_key"]),
            int(item["chunk_index"]),
        )
    )
    return successes, failures
