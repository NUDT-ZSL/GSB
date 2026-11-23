"""Deterministic admission planning under slot, FMA, and memory budgets."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .canonical import stable_float
from .errors import (
    E_COMPUTE_BUDGET,
    E_CONFIG,
    E_MEMORY_BUDGET,
    E_SLOT_LIMIT,
    RecallError,
)
from .ingest import IngestResult
from .weights import WeightTable


ADMITTED = "admitted"
DEGRADED = "degraded"
REJECTED = "rejected"


@dataclass(frozen=True)
class SchedulerConfig:
    slots: int
    fma_budget: int
    memory_budget: int
    worker_shards: int = 1
    chunk_size: int = 8
    query_candidate_docs: int = 0
    avg_doc_nnz: int = 1

    def validate(self) -> None:
        if self.slots < 0:
            raise RecallError(E_CONFIG, "scheduler", "slots must be >= 0")
        if self.fma_budget < 0:
            raise RecallError(E_CONFIG, "scheduler", "fma_budget must be >= 0")
        if self.memory_budget < 0:
            raise RecallError(E_CONFIG, "scheduler", "memory_budget must be >= 0")
        if self.worker_shards < 1:
            raise RecallError(E_CONFIG, "scheduler", "worker_shards must be >= 1")
        if self.chunk_size < 1:
            raise RecallError(E_CONFIG, "scheduler", "chunk_size must be >= 1")

    def to_dict(self) -> dict[str, int]:
        return {
            "slots": self.slots,
            "fma_budget": self.fma_budget,
            "memory_budget": self.memory_budget,
            "worker_shards": self.worker_shards,
            "chunk_size": self.chunk_size,
            "query_candidate_docs": self.query_candidate_docs,
            "avg_doc_nnz": self.avg_doc_nnz,
        }


def make_scheduler_config(
    ingest: IngestResult,
    weights: WeightTable,
    *,
    slots: int,
    fma_budget: int,
    memory_budget: int,
    workers: int = 2,
    chunk_size: int = 8,
) -> SchedulerConfig:
    average_terms = (
        sum(len(document.terms) for document in ingest.documents)
        / len(ingest.documents)
        if ingest.documents
        else 1.0
    )
    return SchedulerConfig(
        slots=slots,
        fma_budget=fma_budget,
        memory_budget=memory_budget,
        worker_shards=max(1, workers),
        chunk_size=chunk_size,
        query_candidate_docs=len(ingest.documents),
        avg_doc_nnz=max(1, int(round(average_terms * weights.nnz_per_term))),
    )


POLICY = {
    "order": [
        "query_priority_desc",
        "term_dedup_savings_desc",
        "batch_size_asc",
        "batch_kind_doc_before_query",
        "batch_no_asc",
    ],
    "degrade_order": [
        "reduce_parallel_shards",
        "reduce_document_scale",
        "reduce_query_scale",
        "reduce_top_k",
    ],
    "reject_codes": [E_COMPUTE_BUDGET, E_MEMORY_BUDGET, E_SLOT_LIMIT],
    "reduction_order": "batch_no,chunk_index,doc_id,term",
    "tie_break": "doc_id_asc",
}


def _base_memory(unique_terms: int, nnz_per_term: int) -> int:
    return 2048 + unique_terms * (32 + nnz_per_term * 16)


def _doc_prefix(
    units: Sequence[tuple[str, frozenset[str], int]],
    count: int,
    *,
    nnz_per_term: int,
) -> dict[str, int]:
    selected = units[:count]
    unique: set[str] = set()
    term_entries = 0
    for _, terms, entries in selected:
        unique.update(terms)
        term_entries += entries
    memory = (
        _base_memory(len(unique), nnz_per_term)
        + count * 128
        + term_entries * 16
    )
    return {
        "count": count,
        "term_entries": term_entries,
        "unique_terms": len(unique),
        "fma": term_entries * nnz_per_term,
        "memory_bytes": memory,
    }


def _query_prefix(
    units: Sequence[tuple[str, frozenset[str], int, int]],
    count: int,
    top_k: int,
    *,
    nnz_per_term: int,
    candidate_docs: int,
    avg_doc_nnz: int,
) -> dict[str, int]:
    selected = units[:count]
    unique: set[str] = set()
    term_entries = 0
    requested_top_k = 0
    for _, terms, entries, query_top_k in selected:
        unique.update(terms)
        term_entries += entries
        requested_top_k += query_top_k
    effective_top_k = sum(min(query_top_k, top_k) for _, _, _, query_top_k in selected)
    fma = (
        term_entries * nnz_per_term
        + count * candidate_docs * max(1, avg_doc_nnz)
        + effective_top_k
    )
    memory = (
        _base_memory(len(unique), nnz_per_term)
        + count * 256
        + term_entries * 16
        + effective_top_k * 48
    )
    return {
        "count": count,
        "top_k": top_k,
        "term_entries": term_entries,
        "unique_terms": len(unique),
        "fma": fma,
        "memory_bytes": memory,
        "requested_top_k_sum": requested_top_k,
    }


def _max_doc_count(units: Sequence[tuple[str, frozenset[str], int]], fma: int, memory: int, nnz: int) -> tuple[int, int, int]:
    low = 0
    high = len(units)
    while low < high:
        middle = (low + high + 1) // 2
        estimate = _doc_prefix(units, middle, nnz_per_term=nnz)
        if estimate["fma"] <= fma and estimate["memory_bytes"] <= memory:
            low = middle
        else:
            high = middle - 1
    one = _doc_prefix(units, 1, nnz_per_term=nnz)
    if low == 0:
        if one["fma"] > fma:
            return 0, E_COMPUTE_BUDGET, 0
        return 0, E_MEMORY_BUDGET, 0
    if one["fma"] > fma:
        return low, E_COMPUTE_BUDGET, 0
    if one["memory_bytes"] > memory:
        return low, E_MEMORY_BUDGET, 0
    next_estimate = _doc_prefix(units, min(low + 1, len(units)), nnz_per_term=nnz)
    if low < len(units) and next_estimate["fma"] > fma and next_estimate["memory_bytes"] > memory:
        return low, E_COMPUTE_BUDGET, 0
    if low < len(units) and next_estimate["fma"] > fma:
        return low, E_COMPUTE_BUDGET, 0
    if low < len(units) and next_estimate["memory_bytes"] > memory:
        return low, E_MEMORY_BUDGET, 0
    return low, "", 0


def _max_query_scale(
    units: Sequence[tuple[str, frozenset[str], int, int]],
    fma: int,
    memory: int,
    nnz: int,
    candidate_docs: int,
    avg_doc_nnz: int,
) -> tuple[int, int, str]:
    max_top_k = max((item[3] for item in units), default=1)
    low = 0
    high = len(units)
    while low < high:
        middle = (low + high + 1) // 2
        estimate = _query_prefix(
            units,
            middle,
            max_top_k,
            nnz_per_term=nnz,
            candidate_docs=candidate_docs,
            avg_doc_nnz=avg_doc_nnz,
        )
        if estimate["fma"] <= fma and estimate["memory_bytes"] <= memory:
            low = middle
        else:
            high = middle - 1
    if low == 0:
        one = _query_prefix(
            units,
            1,
            max_top_k,
            nnz_per_term=nnz,
            candidate_docs=candidate_docs,
            avg_doc_nnz=avg_doc_nnz,
        )
        if one["fma"] > fma and one["memory_bytes"] > memory:
            return 0, 1, E_COMPUTE_BUDGET
        if one["fma"] > fma:
            return 0, 1, E_COMPUTE_BUDGET
        return 0, 1, E_MEMORY_BUDGET

    selected = units[:low]
    estimate = _query_prefix(
        units,
        low,
        max_top_k,
        nnz_per_term=nnz,
        candidate_docs=candidate_docs,
        avg_doc_nnz=avg_doc_nnz,
    )
    remaining_fma = max(0, fma - (estimate["fma"] - estimate["requested_top_k_sum"]))
    remaining_memory = max(0, memory - (estimate["memory_bytes"] - estimate["requested_top_k_sum"] * 48))
    max_top_k_by_fma = max(
        (query_top_k for _, _, _, query_top_k in selected if query_top_k > 0),
        default=1,
    )
    effective_top_k = min(
        max_top_k_by_fma,
        max(1, remaining_fma // max(1, low)),
        max(1, remaining_memory // max(1, low * 48)),
    )
    next_estimate = _query_prefix(
        units,
        min(low + 1, len(units)),
        max_top_k,
        nnz_per_term=nnz,
        candidate_docs=candidate_docs,
        avg_doc_nnz=avg_doc_nnz,
    )
    code = ""
    if low < len(units) and next_estimate["fma"] > fma:
        code = E_COMPUTE_BUDGET
    elif low < len(units) and next_estimate["memory_bytes"] > memory:
        code = E_MEMORY_BUDGET
    return low, effective_top_k, code


def _observation(
    *,
    kind: str,
    batch_no: int,
    count: int,
    unique_terms: int,
    term_entries: int,
    total_frequency: float,
    full_fma: int,
    full_memory: int,
    priority: int,
    dedup_savings_fma: int,
    requested_shards: int,
    max_top_k: int = 0,
    requested_top_k_sum: int = 0,
    invalid_rows: int = 0,
) -> dict[str, Any]:
    return {
        "batch_key": f"{kind}:{batch_no}",
        "kind": kind,
        "batch_no": batch_no,
        "valid_count": count,
        "invalid_rows": invalid_rows,
        "unique_terms": unique_terms,
        "term_entries": term_entries,
        "term_frequency_sum": total_frequency,
        "query_priority": priority,
        "dedup_savings_fma": dedup_savings_fma,
        "requested_shards": requested_shards,
        "max_top_k": max_top_k,
        "requested_top_k_sum": requested_top_k_sum,
        "full_fma": full_fma,
        "full_memory_bytes": full_memory,
    }


def build_observations(
    ingest: IngestResult,
    weights: WeightTable,
    config: SchedulerConfig,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    invalid_doc_by_batch: dict[int, int] = {}
    invalid_query_by_batch: dict[int, int] = {}
    for error in ingest.errors:
        batch_no = error.get("batch_no")
        if not isinstance(batch_no, int):
            continue
        if error.get("batch_kind") == "doc":
            invalid_doc_by_batch[batch_no] = invalid_doc_by_batch.get(batch_no, 0) + 1
        elif error.get("batch_kind") == "query":
            invalid_query_by_batch[batch_no] = invalid_query_by_batch.get(batch_no, 0) + 1

    for batch_no in sorted(ingest.doc_batches):
        docs = ingest.doc_batches[batch_no]
        if not docs:
            continue
        unique: set[str] = set()
        entries = 0
        for document in docs:
            unique.update(document.terms)
            entries += len(document.terms)
        prefix = _doc_prefix(
            [(doc.doc_id, frozenset(doc.terms), len(doc.terms)) for doc in docs],
            len(docs),
            nnz_per_term=weights.nnz_per_term,
        )
        observations.append(
            _observation(
                kind="doc",
                batch_no=batch_no,
                count=len(docs),
                unique_terms=len(unique),
                term_entries=entries,
                total_frequency=stable_float(
                    math.fsum(math.fsum(doc.terms.values()) for doc in docs)
                ),
                full_fma=prefix["fma"],
                full_memory=prefix["memory_bytes"],
                priority=0,
                dedup_savings_fma=max(0, entries - len(unique)) * weights.nnz_per_term,
                requested_shards=min(
                    config.worker_shards,
                    max(1, math.ceil(len(docs) / config.chunk_size)),
                ),
                invalid_rows=invalid_doc_by_batch.get(batch_no, 0),
            )
        )

    for batch_no in sorted(ingest.query_batches):
        queries = ingest.query_batches[batch_no]
        if not queries:
            continue
        units = [
            (
                query.query_id,
                frozenset(query.terms),
                len(query.terms),
                query.top_k,
            )
            for query in queries
        ]
        unique = {term for query in queries for term in query.terms}
        entries = sum(len(query.terms) for query in queries)
        max_top_k = max(query.top_k for query in queries)
        prefix = _query_prefix(
            units,
            len(units),
            max_top_k,
            nnz_per_term=weights.nnz_per_term,
            candidate_docs=config.query_candidate_docs,
            avg_doc_nnz=config.avg_doc_nnz,
        )
        observations.append(
            _observation(
                kind="query",
                batch_no=batch_no,
                count=len(queries),
                unique_terms=len(unique),
                term_entries=entries,
                total_frequency=stable_float(
                    math.fsum(math.fsum(query.terms.values()) for query in queries)
                ),
                full_fma=prefix["fma"],
                full_memory=prefix["memory_bytes"],
                priority=max(query.priority for query in queries),
                dedup_savings_fma=max(0, entries - len(unique)) * weights.nnz_per_term,
                requested_shards=1,
                max_top_k=max_top_k,
                requested_top_k_sum=sum(query.top_k for query in queries),
                invalid_rows=invalid_query_by_batch.get(batch_no, 0),
            )
        )
    return observations


def _priority_key(observation: dict[str, Any]) -> tuple[Any, ...]:
    kind_rank = 0 if observation["kind"] == "doc" else 1
    return (
        -int(observation["query_priority"]),
        -int(observation["dedup_savings_fma"]),
        int(observation["valid_count"]),
        kind_rank,
        int(observation["batch_no"]),
    )


def build_plan(
    ingest: IngestResult,
    weights: WeightTable,
    config: SchedulerConfig,
) -> dict[str, Any]:
    config.validate()
    observations = build_observations(ingest, weights, config)
    observations.sort(key=lambda item: (item["kind"], int(item["batch_no"])))
    ordered = sorted(observations, key=_priority_key)

    decisions: list[dict[str, Any]] = []
    remaining_fma = config.fma_budget
    seen_terms: set[str] = set()
    material_units: dict[str, Sequence[tuple[Any, ...]]] = {}

    for batch_no in sorted(ingest.doc_batches):
        docs = ingest.doc_batches[batch_no]
        material_units[f"doc:{batch_no}"] = [
            (doc.doc_id, frozenset(doc.terms), len(doc.terms)) for doc in docs
        ]
    for batch_no in sorted(ingest.query_batches):
        queries = ingest.query_batches[batch_no]
        material_units[f"query:{batch_no}"] = [
            (query.query_id, frozenset(query.terms), len(query.terms), query.top_k)
            for query in queries
        ]

    accepted: list[dict[str, Any]] = []
    for observation in ordered:
        batch_key = observation["batch_key"]
        kind = observation["kind"]
        batch_no = int(observation["batch_no"])
        units = material_units[batch_key]
        reason_codes: list[str] = []
        reasons: list[str] = []
        status = ADMITTED
        code = ""
        requested_shards = int(observation["requested_shards"])
        effective_shards = requested_shards
        execution = {
            "count": 0,
            "top_k": 0,
            "shards": 0,
            "fma": 0,
            "memory_bytes": 0,
            "wave": None,
        }

        if config.slots == 0:
            status = REJECTED
            code = E_SLOT_LIMIT
            reason_codes.append(E_SLOT_LIMIT)
            reasons.append("no admission slot is available")
            execution = {
                "count": 0,
                "top_k": 0,
                "shards": 0,
                "fma": 0,
                "memory_bytes": 0,
                "wave": None,
            }
        else:
            if requested_shards > config.slots:
                effective_shards = config.slots
                status = DEGRADED
                code = E_SLOT_LIMIT
                reason_codes.append(E_SLOT_LIMIT)
                reasons.append(
                    f"parallel shards reduced from {requested_shards} to {effective_shards}"
                )

            if kind == "doc":
                count, budget_code, _ = _max_doc_count(
                    units, remaining_fma, config.memory_budget, weights.nnz_per_term
                )
                if count == 0:
                    status = REJECTED
                    code = budget_code or E_COMPUTE_BUDGET
                    reason_codes.append(code)
                    reasons.append("the first document exceeds a remaining budget")
                else:
                    prefix = _doc_prefix(units, count, nnz_per_term=weights.nnz_per_term)
                    if budget_code:
                        status = DEGRADED
                        code = budget_code if not reason_codes else reason_codes[0]
                        reason_codes.append(budget_code)
                        reasons.append(
                            f"document scale reduced to {count}/{len(units)}"
                        )
                    execution = {
                        "count": count,
                        "top_k": 0,
                        "shards": effective_shards,
                        "fma": prefix["fma"],
                        "memory_bytes": prefix["memory_bytes"],
                        "wave": None,
                    }
            else:
                count, top_k, budget_code = _max_query_scale(
                    units,
                    remaining_fma,
                    config.memory_budget,
                    weights.nnz_per_term,
                    config.query_candidate_docs,
                    config.avg_doc_nnz,
                )
                if count == 0:
                    status = REJECTED
                    code = budget_code or E_COMPUTE_BUDGET
                    reason_codes.append(code)
                    reasons.append("the first query exceeds a remaining budget")
                else:
                    prefix = _query_prefix(
                        units,
                        count,
                        top_k,
                        nnz_per_term=weights.nnz_per_term,
                        candidate_docs=config.query_candidate_docs,
                        avg_doc_nnz=config.avg_doc_nnz,
                    )
                    max_top_k = int(observation["max_top_k"])
                    if budget_code:
                        status = DEGRADED
                        code = budget_code if not reason_codes else reason_codes[0]
                        reason_codes.append(budget_code)
                        reasons.append(f"query scale reduced to {count}/{len(units)}")
                    if top_k < max_top_k:
                        status = DEGRADED
                        if not code:
                            code = E_COMPUTE_BUDGET
                            reason_codes.append(code)
                        reasons.append(f"top_k reduced to at most {top_k}")
                    execution = {
                        "count": count,
                        "top_k": top_k,
                        "shards": effective_shards,
                        "fma": prefix["fma"],
                        "memory_bytes": prefix["memory_bytes"],
                        "wave": None,
                    }

        executed_units = units[: int(execution["count"])]
        batch_terms = {term for unit in executed_units for term in unit[1]}
        shared_terms = batch_terms & seen_terms
        shared_weight_bytes = len(shared_terms) * weights.nnz_per_term * 16
        decision = {
            "batch_key": batch_key,
            "kind": kind,
            "batch_no": batch_no,
            "status": status,
            "code": code or None,
            "reason_codes": sorted(set(reason_codes)),
            "reasons": reasons,
            "priority": {
                "query_priority": int(observation["query_priority"]),
                "dedup_savings_fma": int(observation["dedup_savings_fma"]),
                "batch_size": int(observation["valid_count"]),
            },
            "estimate": {
                "full_fma": int(observation["full_fma"]),
                "full_memory_bytes": int(observation["full_memory_bytes"]),
                "unique_terms": int(observation["unique_terms"]),
                "term_entries": int(observation["term_entries"]),
                "requested_shards": requested_shards,
            },
            "weight_reuse": {
                "shared_terms": len(shared_terms),
                "shared_weight_bytes": shared_weight_bytes,
            },
            "execution": execution,
        }
        if status != REJECTED:
            remaining_fma -= int(execution["fma"])
            seen_terms.update(batch_terms)
            accepted.append(decision)
        decisions.append(decision)

    waves: list[dict[str, Any]] = []
    for decision in accepted:
        shards = int(decision["execution"]["shards"])
        memory = int(decision["execution"]["memory_bytes"])
        placed = False
        for wave in waves:
            if (
                int(wave["slots_used"]) + shards <= config.slots
                and int(wave["resident_memory_bytes"]) + memory <= config.memory_budget
            ):
                wave["batches"].append(decision["batch_key"])
                wave["slots_used"] += shards
                wave["resident_memory_bytes"] += memory
                wave["fma"] += int(decision["execution"]["fma"])
                decision["execution"]["wave"] = wave["wave"]
                placed = True
                break
        if not placed:
            wave = {
                "wave": len(waves),
                "batches": [decision["batch_key"]],
                "slots_used": shards,
                "resident_memory_bytes": memory,
                "fma": int(decision["execution"]["fma"]),
            }
            waves.append(wave)
            decision["execution"]["wave"] = wave["wave"]

    execution_order = [decision["batch_key"] for decision in accepted]
    totals = {
        "observed_batches": len(observations),
        "admitted_batches": sum(1 for item in decisions if item["status"] == ADMITTED),
        "degraded_batches": sum(1 for item in decisions if item["status"] == DEGRADED),
        "rejected_batches": sum(1 for item in decisions if item["status"] == REJECTED),
        "executed_fma": sum(int(item["execution"]["fma"]) for item in accepted),
        "remaining_fma": remaining_fma,
        "max_concurrency": max((wave["slots_used"] for wave in waves), default=0),
        "peak_resident_memory_bytes": max(
            (wave["resident_memory_bytes"] for wave in waves), default=0
        ),
        "shared_terms_cached": len(seen_terms),
    }
    return {
        "schema": "sparse-recall.plan.v1",
        "policy": POLICY,
        "config": config.to_dict(),
        "observations": observations,
        "decisions": decisions,
        "execution_order": execution_order,
        "waves": waves,
        "totals": totals,
    }


def accepted_decisions(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in plan["decisions"]
        if item["status"] in {ADMITTED, DEGRADED}
    ]


def decision_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["batch_key"]: item for item in accepted_decisions(plan)}


def execution_ids(decision: dict[str, Any], documents: Iterable[Any]) -> list[str]:
    ordered = sorted(documents, key=lambda item: item.doc_id)
    count = int(decision["execution"]["count"])
    return [document.doc_id for document in ordered[:count]]
