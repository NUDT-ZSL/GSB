"""Experiment session orchestration and snapshot replacement/recovery."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional

from .errors import BenchError, ErrorCode
from .execution import (
    FUSED,
    NAIVE,
    QueryResult,
    assert_byte_identical,
    run_fused,
    run_naive,
)
from .logging_utils import StructuredLogger
from .model import DataBlock, QuerySet
from .parser import parse_expression
from .planner import Decision, choose_plan
from .typecheck import annotate

SNAPSHOT_FORMAT_VERSION = 1


@dataclass
class QueryEvaluation:
    query_index: int
    expression: str
    canonical: str
    structure_digest: str
    decision: Decision
    naive: QueryResult
    fused: QueryResult
    selected: QueryResult

    def result_digest(self) -> str:
        return self.selected.result_summary().digest

    def to_report(self) -> dict[str, Any]:
        return {
            "query_index": self.query_index,
            "expression": self.expression,
            "canonical": self.canonical,
            "structure_digest": self.structure_digest,
            "decision": self.decision.to_dict(),
            "selected_path": self.selected.path,
            "selected_report": self.selected.to_report(),
            "naive_report": self.naive.to_report(),
            "fused_report": self.fused.to_report(),
            "consistency": {
                "byte_identical": True,
                "naive_result_digest": self.naive.result_summary().digest,
                "fused_result_digest": self.fused.result_summary().digest,
            },
        }


class ExperimentSession:
    def __init__(self, block: DataBlock, queries: QuerySet, logger: Optional[StructuredLogger] = None):
        self.block = block
        self.queries = queries
        self.logger = logger or StructuredLogger(None)
        self.evaluations: list[QueryEvaluation] = []

    @property
    def data_version(self) -> str:
        explicit = self.block.version
        return explicit or self.block.content_digest()

    def _log(self, name: str, payload: Optional[dict[str, Any]] = None) -> None:
        self.logger.event(name, payload)

    def evaluate_all(self, batch_candidates: Optional[tuple[int, ...]] = None) -> list[QueryEvaluation]:
        self.evaluations = []
        self._log("session.evaluate.start", {
            "block": self.block.name,
            "rows": self.block.row_count,
            "columns": len(self.block.columns),
            "queries": len(self.queries.expressions),
            "data_version": self.data_version,
        })
        for query_index, expression in enumerate(self.queries.expressions):
            root = parse_expression(expression, query_index)
            analyzer = annotate(root, self.block, query_index)
            decision = choose_plan(root, self.block, analyzer, batch_candidates) \
                if batch_candidates else choose_plan(root, self.block, analyzer)
            naive = run_naive(root, self.block, analyzer, query_index, decision.batch_size)
            fused = run_fused(root, self.block, analyzer, query_index, decision.batch_size)
            assert_byte_identical(naive, fused)
            selected = fused if decision.selected_path == FUSED else naive
            evaluation = QueryEvaluation(
                query_index=query_index,
                expression=expression,
                canonical=root.canonical_text(),
                structure_digest=root.structural_digest(),
                decision=decision,
                naive=naive,
                fused=fused,
                selected=selected,
            )
            self.evaluations.append(evaluation)
            self._log("query.evaluated", {
                "query_index": query_index,
                "canonical": evaluation.canonical,
                "selected_path": selected.path,
                "batch_size": decision.batch_size,
                "naive_elapsed": naive.elapsed_seconds,
                "fused_elapsed": fused.elapsed_seconds,
                "naive_peak": naive.peak_bytes_observed,
                "fused_peak": fused.peak_bytes_observed,
            })
        self._log("session.evaluate.complete", {"evaluations": len(self.evaluations)})
        return list(self.evaluations)

    def build_report(self) -> dict[str, Any]:
        return {
            "format_version": SNAPSHOT_FORMAT_VERSION,
            "block": {
                "name": self.block.name,
                "row_count": self.block.row_count,
                "column_names": self.block.column_names,
                "content_digest": self.block.content_digest(),
                "data_version": self.data_version,
            },
            "query_set": self.queries.name,
            "expressions": list(self.queries.expressions),
            "results": [evaluation.to_report() for evaluation in self.evaluations],
        }

    def snapshot(self, path: str) -> dict[str, Any]:
        report = self.build_report()
        record = {
            "format_version": SNAPSHOT_FORMAT_VERSION,
            "data_version": self.data_version,
            "block_content_digest": self.block.content_digest(),
            "block": {
                "name": self.block.name,
                "row_count": self.block.row_count,
                "columns": [_serialize_column(column) for column in self.block.columns],
                "version": self.block.version,
            },
            "queries": {"name": self.queries.name, "expressions": list(self.queries.expressions)},
            "scale": [evaluation.decision.scale.__dict__ for evaluation in self.evaluations],
            "selected_paths": [evaluation.selected.path for evaluation in self.evaluations],
            "candidate_ranking": [
                [item.__dict__ for item in evaluation.decision.ranking]
                for evaluation in self.evaluations
            ],
            "result_summaries": [
                evaluation.selected.result_summary().to_dict() for evaluation in self.evaluations
            ],
            "report": report,
        }
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True, indent=2, ensure_ascii=False)
        self._log("snapshot.written", {"path": path, "data_version": self.data_version})
        return record


def _serialize_column(column) -> dict[str, Any]:
    return {
        "name": column.name,
        "type": column.type_name,
        "values": column.values.tolist(),
        "nullmap": column.nullmap.tolist(),
    }


def _restore_block(payload: dict[str, Any]) -> DataBlock:
    from .model import _build_column  # reuse strict scalar/nullmap validation

    row_count = int(payload["row_count"])
    columns = [
        _build_column(payload["name"], row_count, column_payload, index)
        for index, column_payload in enumerate(payload["columns"])
    ]
    return DataBlock(payload["name"], row_count, columns, str(payload.get("version", "")))


def open_snapshot(path: str, current_block: DataBlock, current_queries: QuerySet,
                  logger: Optional[StructuredLogger] = None) -> tuple[ExperimentSession, str]:
    """Open a snapshot and replace (never incrementally merge) session state."""
    logger = logger or StructuredLogger(None)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError as exc:
        raise BenchError(ErrorCode.E_DATA, "snapshot file not found", {"path": path}) from exc
    except json.JSONDecodeError as exc:
        raise BenchError(ErrorCode.E_DATA, "snapshot is not valid JSON",
                         {"path": path, "line": exc.lineno}) from exc
    snapshot_version = str(record.get("data_version", ""))
    current_version = current_block.version or current_block.content_digest()
    replacement_mode = "matched" if snapshot_version == current_version else "full_replace"
    logger.event("snapshot.opened", {
        "path": path,
        "snapshot_version": snapshot_version,
        "current_version": current_version,
        "mode": replacement_mode,
    })
    # Whole-state replacement: reconstruct block/queries from the snapshot itself.
    restored_block = _restore_block(record["block"])
    restored_queries = QuerySet(
        record["queries"]["name"], tuple(record["queries"]["expressions"])
    )
    session = ExperimentSession(restored_block, restored_queries, logger)
    evaluations = session.evaluate_all()
    expected_summaries = record.get("result_summaries", [])
    expected_paths = record.get("selected_paths", [])
    for index, evaluation in enumerate(evaluations):
        if index >= len(expected_summaries):
            raise BenchError(ErrorCode.E_REPLAY, "snapshot is missing a result summary",
                             {"query_index": index})
        actual = evaluation.selected.result_summary().to_dict()
        expected = expected_summaries[index]
        if actual != expected or evaluation.selected.path != expected_paths[index]:
            raise BenchError(
                ErrorCode.E_REPLAY,
                "replay diverged from snapshot",
                {"query_index": index,
                 "expected_path": expected_paths[index],
                 "actual_path": evaluation.selected.path,
                 "expected_digest": expected.get("digest"),
                 "actual_digest": actual.get("digest")},
            )
    logger.event("snapshot.replay.verified", {
        "path": path,
        "mode": replacement_mode,
        "queries": len(evaluations),
    })
    return session, replacement_mode
