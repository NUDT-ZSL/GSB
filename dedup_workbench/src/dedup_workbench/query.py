"""Interactive doc_id query API and observed latency accounting."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import canonical_bytes, hash_json
from .errors import E_LATENCY_BUDGET, E_NOT_FOUND, WorkbenchError


def build_doc_record(
    document: dict[str, Any],
    signature: dict[str, Any],
    candidate_index: dict[str, list[str]],
    comparisons_by_pair: dict[str, dict[str, Any]],
    decision: dict[str, Any],
) -> dict[str, Any]:
    pair_ids = list(candidate_index.get(document["doc_id"], []))
    comparisons = [comparisons_by_pair[pair_id] for pair_id in pair_ids if pair_id in comparisons_by_pair]
    return {
        "schema": "dedup-workbench/doc-record/v1",
        "doc_id": document["doc_id"],
        "shard_id": document["shard_id"],
        "lang": document["lang"],
        "source": document["source"],
        "token_estimate": document["token_estimate"],
        "quality_score": document["quality_score"],
        "content_hash": document["content_hash"],
        "input_hash": document["input_hash"],
        "signature": signature,
        "candidate_trajectory": {
            "candidate_pair_count": len(pair_ids),
            "pair_ids": pair_ids,
            "comparisons": comparisons,
        },
        "decision": decision,
        "removal": {
            "removed": bool(decision["removed"]),
            "representative_doc_id": decision["representative_doc_id"],
            "component_id": decision["component_id"],
        },
    }


class QueryService:
    def __init__(self, doc_records_path: str | Path, config: dict[str, Any] | None = None) -> None:
        self.path = Path(doc_records_path)
        if not self.path.exists():
            raise WorkbenchError(E_NOT_FOUND, "Batch doc_records export does not exist", field=str(self.path))
        self._raw_by_id: dict[str, bytes] = {}
        self._records: dict[str, dict[str, Any]] = {}
        for line in self.path.read_bytes().splitlines():
            if not line.strip():
                continue
            record = json.loads(line.decode("utf-8"))
            self._records[record["doc_id"]] = record
            self._raw_by_id[record["doc_id"]] = line
        self.config = config or {"limits": {"query_loop_iterations": 41, "latency_budget_ms": 25.0}, "latency_model": {}}

    def doc_ids(self) -> list[str]:
        return sorted(self._raw_by_id)

    def raw_record_bytes(self, doc_id: str) -> bytes:
        if doc_id not in self._raw_by_id:
            raise WorkbenchError(E_NOT_FOUND, "Unknown doc_id", field="doc_id", details={"doc_id": doc_id})
        return self._raw_by_id[doc_id]

    def query(
        self,
        doc_id: str,
        *,
        budget_ms: float | None = None,
        iterations: int | None = None,
    ) -> dict[str, Any]:
        if doc_id not in self._raw_by_id:
            raise WorkbenchError(E_NOT_FOUND, "Unknown doc_id", field="doc_id", details={"doc_id": doc_id})

        loop_count = int(iterations or self.config["limits"]["query_loop_iterations"])
        if loop_count < 1:
            raise WorkbenchError("E_CONFIG_INVALID", "iterations must be positive", field="iterations")
        raw = self._raw_by_id[doc_id]
        durations_ms: list[float] = []
        record: dict[str, Any] | None = None
        for _ in range(loop_count):
            started = time.perf_counter_ns()
            record = json.loads(raw.decode("utf-8"))
            _ = record["doc_id"]
            finished = time.perf_counter_ns()
            durations_ms.append((finished - started) / 1_000_000.0)

        assert record is not None
        samples = np.asarray(durations_ms, dtype=np.float64)
        block_count = int(record["signature"]["block_count"])
        comparison_count = int(record["candidate_trajectory"]["candidate_pair_count"])
        model = self.config.get("latency_model", {})
        estimated_ms = (
            float(model.get("base_ms", 0.02))
            + block_count * float(model.get("per_block_ms", 0.0012))
            + comparison_count * float(model.get("per_comparison_ms", 0.004))
        )
        budget = float(budget_ms if budget_ms is not None else self.config["limits"]["latency_budget_ms"])
        timing = {
            "loop_iterations": loop_count,
            "observed_total_ms": round(float(samples.sum()), 6),
            "observed_avg_ms": round(float(samples.mean()), 6),
            "observed_median_ms": round(float(np.median(samples)), 6),
            "observed_p95_ms": round(float(np.percentile(samples, 95)), 6),
            "comparison_count": comparison_count,
            "signature_block_count": block_count,
            "estimated_ms": round(estimated_ms, 6),
            "latency_budget_ms": budget,
        }
        timing["within_budget"] = timing["observed_avg_ms"] <= budget and estimated_ms <= budget
        if not timing["within_budget"]:
            raise WorkbenchError(
                E_LATENCY_BUDGET,
                "doc_id query exceeded the configured latency budget",
                field="limits.latency_budget_ms",
                details={"doc_id": doc_id, "timing": timing},
            )

        canonical_record = canonical_bytes(record)
        return {
            "doc_id": doc_id,
            "record": record,
            "record_bytes": raw,
            "canonical_record_bytes": canonical_record,
            "batch_line_bytes": raw,
            "byte_identical_to_batch_line": canonical_record == raw,
            "record_sha256": hash_json(record),
            "timing": timing,
        }

    def query_or_error_dict(self, doc_id: str, **kwargs: Any) -> dict[str, Any]:
        try:
            return {"ok": True, "query": self.query(doc_id, **kwargs)}
        except WorkbenchError as exc:
            return exc.to_dict()
