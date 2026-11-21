"""Deterministic scale-aware exact/approximate path selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .budget import BudgetPolicy, admit_budget, estimate_path_cost
from .util import sha256_obj


PATH_EXACT = "exact"
PATH_APPROX = "pruned_approx"


@dataclass(frozen=True)
class ScalePolicy:
    exact_max_queries: int = 128
    exact_max_dictionary_entries: int = 256
    exact_max_avg_tokens: float = 12.0
    exact_min_memory_mb: float = 64.0
    priority_order: tuple[str, ...] = ("precision", "memory", "progress")

    @classmethod
    def from_dict(cls, row: dict[str, Any] | None) -> "ScalePolicy":
        row = row or {}
        return cls(
            exact_max_queries=int(row.get("exact_max_queries", 128)),
            exact_max_dictionary_entries=int(
                row.get("exact_max_dictionary_entries", 256)
            ),
            exact_max_avg_tokens=float(row.get("exact_max_avg_tokens", 12.0)),
            exact_min_memory_mb=float(row.get("exact_min_memory_mb", 64.0)),
            priority_order=tuple(
                row.get("priority_order", ["precision", "memory", "progress"])
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "exact_max_queries": self.exact_max_queries,
            "exact_max_dictionary_entries": self.exact_max_dictionary_entries,
            "exact_max_avg_tokens": self.exact_max_avg_tokens,
            "exact_min_memory_mb": self.exact_min_memory_mb,
            "priority_order": list(self.priority_order),
        }


def observe_scale(
    *,
    query_count: int,
    dictionary_entry_count: int,
    total_tokens: int,
    memory_limit_mb: float,
    resident_memory_mb: float,
    memory_reserve_mb: float = 32.0,
    resident_memory_source: str = "explicit",
) -> dict[str, Any]:
    avg_tokens = round(total_tokens / query_count, 6) if query_count else 0.0
    available = round(
        max(
            0.0,
            memory_limit_mb - resident_memory_mb - memory_reserve_mb,
        ),
        6,
    )
    return {
        "query_count": query_count,
        "dictionary_entry_count": dictionary_entry_count,
        "total_tokens": total_tokens,
        "avg_tokens": avg_tokens,
        "memory_limit_mb": round(memory_limit_mb, 6),
        "resident_memory_mb": round(resident_memory_mb, 6),
        "resident_memory_source": resident_memory_source,
        "memory_reserve_mb": round(memory_reserve_mb, 6),
        "memory_available_mb": available,
    }


def choose_path(
    observation: dict[str, Any],
    scale_policy: ScalePolicy,
    budget_policy: BudgetPolicy,
    *,
    allow_downbatch: bool = True,
) -> dict[str, Any]:
    exact_reasons: list[str] = []
    if int(observation["query_count"]) > scale_policy.exact_max_queries:
        exact_reasons.append("QUERY_COUNT_OVER_EXACT_LIMIT")
    if int(observation["dictionary_entry_count"]) > scale_policy.exact_max_dictionary_entries:
        exact_reasons.append("DICTIONARY_OVER_EXACT_LIMIT")
    if float(observation["avg_tokens"]) > scale_policy.exact_max_avg_tokens:
        exact_reasons.append("AVG_TOKENS_OVER_EXACT_LIMIT")
    if float(observation["memory_available_mb"]) < scale_policy.exact_min_memory_mb:
        exact_reasons.append("MEMORY_MARGIN_BELOW_EXACT_FLOOR")

    exact_admission = admit_budget(
        observation,
        PATH_EXACT,
        budget_policy,
        allow_downbatch=allow_downbatch,
    )
    approx_admission = admit_budget(
        observation,
        PATH_APPROX,
        budget_policy,
        allow_downbatch=allow_downbatch,
    )
    budget_blocks_exact = not exact_admission["ok"]
    if not exact_reasons and not budget_blocks_exact:
        selected = PATH_EXACT
        admission = exact_admission
        selected_reasons = [
            "all exact-path scale thresholds are satisfied",
            "full-dictionary exact matching plus two-stage disambiguation is admissible",
        ]
    else:
        selected = PATH_APPROX
        admission = approx_admission
        selected_reasons = [
            "at least one exact-path scale threshold or budget check failed",
            "restricted candidate pruning plus approximate disambiguation is selected",
        ]
        if not exact_admission["ok"]:
            exact_reasons.append(str(exact_admission["error_code"]))

    record: dict[str, Any] = {
        "schema_version": 1,
        "observation": observation,
        "scale_policy": scale_policy.to_dict(),
        "budget_policy": budget_policy.to_dict(),
        "exact_threshold_failures": exact_reasons,
        "cost_estimates": {
            PATH_EXACT: estimate_path_cost(observation, PATH_EXACT, budget_policy),
            PATH_APPROX: estimate_path_cost(observation, PATH_APPROX, budget_policy),
        },
        "admissions": {
            PATH_EXACT: exact_admission,
            PATH_APPROX: approx_admission,
        },
        "selected_path": selected,
        "selected_reasons": selected_reasons,
        "admission": admission,
        "priority_order": list(scale_policy.priority_order),
        "ok": bool(admission["ok"]),
        "error_code": admission.get("error_code"),
    }
    record["decision_checksum"] = sha256_obj(
        {
            "observation": observation,
            "scale_policy": scale_policy.to_dict(),
            "budget_policy": budget_policy.to_dict(),
            "selected_path": selected,
            "admission": admission,
        }
    )
    return record

