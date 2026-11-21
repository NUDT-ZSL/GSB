from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Iterable

from .config import PipelineConfig
from .models import Document, QueryTurn
from .pipeline import Pipeline


def _run_one(payload: dict[str, Any]) -> dict[str, Any]:
    config = PipelineConfig(**payload["config"])
    query = QueryTurn.from_dict(payload["query"])
    queries = [QueryTurn.from_dict(item) for item in payload["queries"]]
    documents = {
        doc_id: Document.from_dict(document)
        for doc_id, document in payload["documents"].items()
    }
    pipeline = Pipeline(config=config, cache_root=Path(payload["cache_root"]))
    result = pipeline.run(
        query,
        queries,
        documents,
        force_recompute=bool(payload.get("force_recompute")),
    )
    return {
        "conv_id": result.package.conv_id,
        "turn_no": result.package.turn_no,
        "canonical_answer": result.canonical_answer,
        "byte_hash": result.byte_hash,
    }


def run_batch(
    queries_to_run: Iterable[QueryTurn],
    all_queries: Iterable[QueryTurn],
    documents: dict[str, Document],
    config: PipelineConfig,
    cache_root: Path,
    workers: int | None = None,
    force_recompute: bool = False,
) -> list[dict[str, Any]]:
    query_list = sorted(
        queries_to_run,
        key=lambda item: (item.conv_id, item.turn_no, item.query_text),
    )
    all_query_list = sorted(
        all_queries,
        key=lambda item: (item.conv_id, item.turn_no, item.query_text),
    )
    payloads = [
        {
            "query": query.to_dict(),
            "queries": [item.to_dict() for item in all_query_list],
            "documents": {
                doc_id: document.to_dict()
                for doc_id, document in sorted(documents.items())
            },
            "config": config.to_dict(),
            "cache_root": str(cache_root),
            "force_recompute": force_recompute,
        }
        for query in query_list
    ]
    if workers is None or workers <= 1:
        return [_run_one(payload) for payload in payloads]
    with ProcessPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(_run_one, payloads))
    return sorted(
        results,
        key=lambda item: (item["conv_id"], item["turn_no"]),
    )
