"""Pickle-safe worker entry points for spawn-mode execution."""

from __future__ import annotations

from typing import Any

from .errors import E_WORKER, RecallError, error_dict
from .inverted import accumulate_chunk
from .models import Document
from .projection import project_document_chunk
from .weights import WeightTable


def worker_entry(task: dict[str, Any]) -> dict[str, Any]:
    """Execute one deterministic chunk and always return a structured envelope."""

    operation = task.get("operation")
    batch_no = task.get("batch_no")
    batch_key = task.get("batch_key")
    stage = "projection" if operation == "project" else "inverted"
    try:
        injected = task.get("injected_failure")
        if injected:
            raise RecallError(
                injected.get("code", E_WORKER),
                stage,
                injected.get("message", "injected worker failure"),
                batch_no=batch_no,
                batch_kind=task.get("kind"),
            )
        weights = task["weights"]
        if not isinstance(weights, WeightTable):
            weights = WeightTable(**weights)
        documents = [
            Document(
                doc_id=row["doc_id"],
                batch_no=row["batch_no"],
                terms=dict(row["terms"]),
                doc_len=row["doc_len"],
            )
            for row in task["documents"]
        ]
        if operation == "statistics":
            payload = accumulate_chunk(
                documents,
                batch_no=int(batch_no),
                chunk_index=int(task["chunk_index"]),
                compensated=bool(task["compensated"]),
                decimals=int(task["decimals"]),
            )
        elif operation == "project":
            payload = project_document_chunk(
                documents,
                weights,
                chunk_index=int(task["chunk_index"]),
                decimals=int(task["decimals"]),
                subnormal_floor=float(task["subnormal_floor"]),
            )
        else:
            raise RecallError(
                E_WORKER,
                stage,
                f"unknown worker operation {operation!r}",
                batch_no=batch_no,
                batch_kind=task.get("kind"),
            )
        return {
            "ok": True,
            "operation": operation,
            "batch_key": batch_key,
            "batch_no": batch_no,
            "chunk_index": task["chunk_index"],
            "payload": payload,
        }
    except RecallError as exc:
        envelope = exc.to_dict()
        return {
            "ok": False,
            "operation": operation,
            "batch_key": batch_key,
            "batch_no": batch_no,
            "chunk_index": task.get("chunk_index"),
            "error": envelope,
        }
    except BaseException as exc:
        return {
            "ok": False,
            "operation": operation,
            "batch_key": batch_key,
            "batch_no": batch_no,
            "chunk_index": task.get("chunk_index"),
            "error": error_dict(
                E_WORKER,
                stage,
                f"{type(exc).__name__}: {exc}",
                batch_no=batch_no,
                batch_kind=task.get("kind"),
            ),
        }
