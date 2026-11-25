"""Deterministic gating, capacity allocation, expert execution, and audits."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .codec import array_digest, canonical_hash
from .errors import (
    E_BLOCK_MISMATCH,
    E_BLOCK_NOT_READY,
    E_INVALID_INPUT,
    E_RESOURCE_LIMIT,
    MoeError,
)
from .models import CaseSpec, ExpertBank, ResourceLimits, WeightBundle
from .validation import (
    enforce_memory_limit,
    validate_case,
    validate_expert_bank,
    validate_revision,
    validate_tokens_and_gate,
)


FORMAT_VERSION = "moe-router-v1"
EPSILON = 1e-15


@dataclass
class GateResult:
    raw_scores: np.ndarray
    probabilities: np.ndarray
    selected_experts: np.ndarray
    selected_gate: np.ndarray

    def slice(self, start: int, stop: int) -> "GateResult":
        return GateResult(
            raw_scores=self.raw_scores[start:stop],
            probabilities=self.probabilities[start:stop],
            selected_experts=self.selected_experts[start:stop],
            selected_gate=self.selected_gate[start:stop],
        )


@dataclass
class CapacityAssignment:
    capacity_per_expert: int
    accepted_mask: np.ndarray
    accepted_per_expert: tuple[int, ...]
    overflow: tuple[dict[str, Any], ...]
    capacity_summary: dict[str, Any]


@dataclass
class BlockResult:
    block_index: int
    token_start: int
    token_end: int
    gate: GateResult
    assignment: CapacityAssignment
    outputs: np.ndarray
    contributions: np.ndarray
    expert_revisions_used: dict[str, int]
    accepted_expert_revisions: dict[str, int]
    metadata: dict[str, Any] = field(default_factory=dict)
    cache_records: dict[str, Any] = field(default_factory=dict)
    ready_ns: int = 0
    reused: bool = False

    @property
    def token_count(self) -> int:
        return self.token_end - self.token_start


@dataclass
class FullReferenceResult:
    case_id: str
    outputs: np.ndarray
    gate: GateResult
    assignments: tuple[CapacityAssignment, ...]
    contributions: np.ndarray
    expert_revisions_used_by_block: tuple[dict[str, int], ...]


def _stable_dot(left: np.ndarray, right: np.ndarray) -> float:
    return math.fsum(
        float(a) * float(b)
        for a, b in zip(left.tolist(), right.tolist(), strict=True)
    )


def _softmax_row(scores: list[float]) -> list[float]:
    maximum = max(scores)
    exponentials = [math.exp(value - maximum) for value in scores]
    denominator = math.fsum(exponentials)
    return [value / denominator for value in exponentials]


def gate_tokens(
    tokens: np.ndarray,
    gate_weights: np.ndarray,
    top_k: int,
) -> GateResult:
    """Compute deterministic full gate distributions and top-k selections."""

    token_array = np.asarray(tokens, dtype=np.float64)
    weight_array = np.asarray(gate_weights, dtype=np.float64)
    token_count = token_array.shape[0]
    expert_count = weight_array.shape[1]
    if not 1 <= top_k <= expert_count:
        raise MoeError(
            code=E_INVALID_INPUT,
            message="top_k must be between 1 and num_experts",
            field="top_k",
            details={"top_k": top_k, "num_experts": expert_count},
        )

    raw_scores = np.empty((token_count, expert_count), dtype=np.float64)
    probabilities = np.empty((token_count, expert_count), dtype=np.float64)
    selected_experts = np.empty((token_count, top_k), dtype=np.int64)
    selected_gate = np.empty((token_count, top_k), dtype=np.float64)

    for token_index in range(token_count):
        token_scores = [
            _stable_dot(token_array[token_index], weight_array[:, expert_id])
            for expert_id in range(expert_count)
        ]
        selected = sorted(
            range(expert_count),
            key=lambda expert_id: (-token_scores[expert_id], expert_id),
        )[:top_k]
        token_probabilities = _softmax_row(token_scores)
        selected_probabilities = [token_probabilities[expert_id] for expert_id in selected]
        selected_denominator = math.fsum(selected_probabilities)
        normalized_selected = [
            probability / selected_denominator for probability in selected_probabilities
        ]
        raw_scores[token_index] = token_scores
        probabilities[token_index] = token_probabilities
        selected_experts[token_index] = selected
        selected_gate[token_index] = normalized_selected

    return GateResult(
        raw_scores=raw_scores,
        probabilities=probabilities,
        selected_experts=selected_experts,
        selected_gate=selected_gate,
    )


def assign_capacity(
    gate: GateResult,
    *,
    token_start: int,
    block_index: int,
    capacity_factor: float,
    num_experts: int,
) -> CapacityAssignment:
    """Apply a fresh per-block capacity boundary for every expert."""

    block_token_count, top_k = gate.selected_experts.shape
    capacity_per_expert = max(
        1,
        int(math.ceil(float(capacity_factor) * float(block_token_count) / float(num_experts))),
    )
    accepted_mask = np.zeros((block_token_count, top_k), dtype=np.bool_)
    accepted_per_expert = [0] * num_experts

    for expert_id in range(num_experts):
        candidates: list[tuple[float, int, int]] = []
        for token_in_block in range(block_token_count):
            for rank in range(top_k):
                if int(gate.selected_experts[token_in_block, rank]) == expert_id:
                    candidates.append(
                        (
                            float(gate.selected_gate[token_in_block, rank]),
                            token_in_block,
                            rank,
                        )
                    )
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        for _, token_in_block, rank in candidates[:capacity_per_expert]:
            accepted_mask[token_in_block, rank] = True
            accepted_per_expert[expert_id] += 1

    overflow_records: list[dict[str, Any]] = []
    overflow_per_expert = [0] * num_experts
    token_overflow: set[int] = set()
    for expert_id in range(num_experts):
        candidates = []
        for token_in_block in range(block_token_count):
            for rank in range(top_k):
                if (
                    int(gate.selected_experts[token_in_block, rank]) == expert_id
                    and not bool(accepted_mask[token_in_block, rank])
                ):
                    score = float(gate.selected_gate[token_in_block, rank])
                    candidates.append((score, token_in_block, rank))
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        for score, token_in_block, rank in candidates:
            global_token = token_start + token_in_block
            token_overflow.add(global_token)
            overflow_per_expert[expert_id] += 1
            overflow_records.append(
                {
                    "block_index": block_index,
                    "token_index": global_token,
                    "token_in_block": token_in_block,
                    "expert_id": expert_id,
                    "top_k_rank": rank,
                    "gate_score": score,
                }
            )

    overflow_records.sort(
        key=lambda item: (
            item["expert_id"],
            -item["gate_score"],
            item["token_index"],
            item["top_k_rank"],
        )
    )
    summary = {
        "block_index": block_index,
        "token_start": token_start,
        "token_end": token_start + block_token_count,
        "block_token_len": block_token_count,
        "capacity_per_expert": capacity_per_expert,
        "capacity_factor": float(capacity_factor),
        "accepted_per_expert": accepted_per_expert,
        "overflow_per_expert": overflow_per_expert,
        "total_selected": int(block_token_count * top_k),
        "total_accepted": int(accepted_mask.sum()),
        "total_overflow": len(overflow_records),
        "overflow_token_positions": sorted(token_overflow),
    }
    return CapacityAssignment(
        capacity_per_expert=capacity_per_expert,
        accepted_mask=accepted_mask,
        accepted_per_expert=tuple(accepted_per_expert),
        overflow=tuple(overflow_records),
        capacity_summary=summary,
    )


def _expert_output(token_vector: np.ndarray, weight: np.ndarray) -> np.ndarray:
    hidden_dim = weight.shape[0]
    result = np.empty(hidden_dim, dtype=np.float64)
    for output_dim in range(hidden_dim):
        result[output_dim] = _stable_dot(token_vector, weight[:, output_dim])
    return result


def execute_block_experts(
    tokens: np.ndarray,
    gate: GateResult,
    assignment: CapacityAssignment,
    expert_bank: ExpertBank,
) -> tuple[np.ndarray, np.ndarray]:
    """Execute accepted expert paths using one shared arithmetic order.

    Experts are ordered by expert id, and each final output coordinate is
    accumulated with ``math.fsum``. Block and full-reference paths both call
    this function, so arithmetic order does not change with block boundaries.
    """

    token_array = np.asarray(tokens, dtype=np.float64)
    block_token_count, top_k = gate.selected_experts.shape
    hidden_dim = token_array.shape[1]
    contributions = np.zeros((block_token_count, top_k, hidden_dim), dtype=np.float64)
    outputs = np.zeros((block_token_count, hidden_dim), dtype=np.float64)

    for token_in_block in range(block_token_count):
        order = sorted(
            range(top_k),
            key=lambda rank: int(gate.selected_experts[token_in_block, rank]),
        )
        for rank in order:
            if not bool(assignment.accepted_mask[token_in_block, rank]):
                continue
            expert_id = int(gate.selected_experts[token_in_block, rank])
            expert_value = _expert_output(
                token_array[token_in_block], expert_bank.weights[expert_id]
            )
            contributions[token_in_block, rank] = (
                float(gate.selected_gate[token_in_block, rank]) * expert_value
            )
        for output_dim in range(hidden_dim):
            outputs[token_in_block, output_dim] = math.fsum(
                float(contributions[token_in_block, rank, output_dim])
                for rank in order
                if bool(assignment.accepted_mask[token_in_block, rank])
            )

    return outputs, contributions


def _expert_revision_maps(
    gate: GateResult, assignment: CapacityAssignment, expert_bank: ExpertBank
) -> tuple[dict[str, int], dict[str, int]]:
    selected_ids = sorted({int(value) for value in gate.selected_experts.ravel()})
    accepted_ids = sorted(
        {
            int(gate.selected_experts[token_index, rank])
            for token_index in range(gate.selected_experts.shape[0])
            for rank in range(gate.selected_experts.shape[1])
            if bool(assignment.accepted_mask[token_index, rank])
        }
    )
    selected_revisions = {
        str(expert_id): expert_bank.revisions[expert_id] for expert_id in selected_ids
    }
    accepted_revisions = {
        str(expert_id): expert_bank.revisions[expert_id] for expert_id in accepted_ids
    }
    return selected_revisions, accepted_revisions


def build_block_result(
    *,
    block_index: int,
    token_start: int,
    tokens: np.ndarray,
    gate: GateResult,
    assignment: CapacityAssignment,
    expert_bank: ExpertBank,
) -> BlockResult:
    outputs, contributions = execute_block_experts(
        tokens, gate, assignment, expert_bank
    )
    selected_revisions, accepted_revisions = _expert_revision_maps(
        gate, assignment, expert_bank
    )
    return BlockResult(
        block_index=block_index,
        token_start=token_start,
        token_end=token_start + tokens.shape[0],
        gate=gate,
        assignment=assignment,
        outputs=outputs,
        contributions=contributions,
        expert_revisions_used=selected_revisions,
        accepted_expert_revisions=accepted_revisions,
        ready_ns=time.monotonic_ns(),
    )


def compute_full_reference(bundle: WeightBundle) -> FullReferenceResult:
    """Compute one contiguous full-sequence reference pass.

    Capacity boundaries remain block-local because that is part of the required
    routing semantics. Once assignments exist, expert execution happens in one
    uninterrupted full-sequence pass using the shared deterministic kernel.
    """

    case = bundle.case
    validate_tokens_and_gate(case, bundle.tokens, bundle.gate_weights)
    validate_expert_bank(case, bundle.expert_bank)
    gate = gate_tokens(bundle.tokens, bundle.gate_weights, case.top_k)
    assignments: list[CapacityAssignment] = []
    outputs: list[np.ndarray] = []
    contributions: list[np.ndarray] = []
    revision_maps: list[dict[str, int]] = []

    for block_index, token_start in enumerate(
        range(0, case.token_len, case.block_size)
    ):
        token_end = min(case.token_len, token_start + case.block_size)
        block_gate = gate.slice(token_start, token_end)
        assignment = assign_capacity(
            block_gate,
            token_start=token_start,
            block_index=block_index,
            capacity_factor=case.capacity_factor,
            num_experts=case.num_experts,
        )
        block_outputs, block_contributions = execute_block_experts(
            bundle.tokens[token_start:token_end],
            block_gate,
            assignment,
            bundle.expert_bank,
        )
        selected_revisions, _ = _expert_revision_maps(
            block_gate, assignment, bundle.expert_bank
        )
        assignments.append(assignment)
        outputs.append(block_outputs)
        contributions.append(block_contributions)
        revision_maps.append(selected_revisions)

    return FullReferenceResult(
        case_id=case.case_id,
        outputs=np.concatenate(outputs, axis=0),
        gate=gate,
        assignments=tuple(assignments),
        contributions=np.concatenate(contributions, axis=0),
        expert_revisions_used_by_block=tuple(revision_maps),
    )


def _diff_stats(actual: np.ndarray, reference: np.ndarray) -> tuple[float, float, np.ndarray]:
    actual_array = np.asarray(actual, dtype=np.float64)
    reference_array = np.asarray(reference, dtype=np.float64)
    delta = np.abs(actual_array - reference_array)
    relative = delta / np.maximum(np.abs(reference_array), EPSILON)
    if delta.ndim == 1:
        per_token = delta
    else:
        per_token = delta.reshape(delta.shape[0], -1).max(axis=1)
    return float(delta.max(initial=0.0)), float(relative.max(initial=0.0)), per_token


def _assignment_token_differences(
    actual: tuple[CapacityAssignment, ...],
    reference: tuple[CapacityAssignment, ...],
    actual_gate: GateResult,
    reference_gate: GateResult,
    block_size: int,
) -> list[int]:
    differences: list[int] = []
    for block_index, (actual_block, reference_block) in enumerate(
        zip(actual, reference, strict=True)
    ):
        mask_equal = np.array_equal(
            actual_block.accepted_mask, reference_block.accepted_mask
        )
        selected_start = block_index * block_size
        selected_stop = selected_start + actual_block.accepted_mask.shape[0]
        selection_equal = np.array_equal(
            actual_gate.selected_experts[selected_start:selected_stop],
            reference_gate.selected_experts[selected_start:selected_stop],
        )
        capacity_equal = (
            actual_block.capacity_per_expert == reference_block.capacity_per_expert
        )
        if not mask_equal or not selection_equal or not capacity_equal:
            for token_in_block in range(actual_block.accepted_mask.shape[0]):
                global_token = selected_start + token_in_block
                if (
                    not np.array_equal(
                        actual_block.accepted_mask[token_in_block],
                        reference_block.accepted_mask[token_in_block],
                    )
                    or not np.array_equal(
                        actual_gate.selected_experts[global_token],
                        reference_gate.selected_experts[global_token],
                    )
                    or not capacity_equal
                ):
                    differences.append(global_token)
    return differences


def compare_routes(
    case: CaseSpec,
    *,
    actual_outputs: np.ndarray,
    actual_gate: GateResult,
    actual_assignments: tuple[CapacityAssignment, ...],
    actual_contributions: np.ndarray,
    reference: FullReferenceResult,
    tolerance: float = 1e-12,
    raise_on_mismatch: bool = True,
) -> dict[str, Any]:
    gate_abs, gate_rel, gate_per_token = _diff_stats(
        actual_gate.probabilities, reference.gate.probabilities
    )
    expert_abs, expert_rel, expert_per_token = _diff_stats(
        actual_contributions, reference.contributions
    )
    output_abs, output_rel, output_per_token = _diff_stats(
        actual_outputs, reference.outputs
    )
    assignment_differences = _assignment_token_differences(
        actual_assignments,
        reference.assignments,
        actual_gate,
        reference.gate,
        case.block_size,
    )
    selection_equal = np.array_equal(
        actual_gate.selected_experts, reference.gate.selected_experts
    )
    capacity_equal = all(
        actual_block.capacity_per_expert == reference_block.capacity_per_expert
        for actual_block, reference_block in zip(
            actual_assignments, reference.assignments, strict=True
        )
    )
    token_max_abs = np.maximum.reduce(
        [gate_per_token, expert_per_token, output_per_token]
    )
    bad_tokens = [
        index
        for index, value in enumerate(token_max_abs.tolist())
        if value > tolerance
    ]
    first_diff_token: int | None = None
    if bad_tokens:
        first_diff_token = bad_tokens[0]
    elif assignment_differences:
        first_diff_token = assignment_differences[0]

    consistent = (
        gate_abs <= tolerance
        and expert_abs <= tolerance
        and output_abs <= tolerance
        and not assignment_differences
        and selection_equal
        and capacity_equal
    )
    report: dict[str, Any] = {
        "case_id": case.case_id,
        "consistent": consistent,
        "tolerance": tolerance,
        "max_abs_diff_gate": gate_abs,
        "max_rel_diff_gate": gate_rel,
        "max_abs_diff_expert": expert_abs,
        "max_rel_diff_expert": expert_rel,
        "max_abs_diff_output": output_abs,
        "max_rel_diff_output": output_rel,
        "first_diff_token": first_diff_token,
        "assignment_equal": not assignment_differences,
        "selection_equal": selection_equal,
        "capacity_equal": capacity_equal,
        "token_max_abs_diff": token_max_abs.tolist(),
    }

    if not consistent and first_diff_token is not None:
        token_index = first_diff_token
        block_index = token_index // case.block_size
        token_in_block = token_index - block_index * case.block_size
        actual_block = actual_assignments[block_index]
        reference_block = reference.assignments[block_index]
        rank_details = []
        for rank in range(case.top_k):
            rank_details.append(
                {
                    "rank": rank,
                    "actual_expert": int(
                        actual_gate.selected_experts[token_index, rank]
                    ),
                    "reference_expert": int(
                        reference.gate.selected_experts[token_index, rank]
                    ),
                    "actual_gate": float(
                        actual_gate.selected_gate[token_index, rank]
                    ),
                    "reference_gate": float(
                        reference.gate.selected_gate[token_index, rank]
                    ),
                    "actual_accepted": bool(
                        actual_block.accepted_mask[token_in_block, rank]
                    ),
                    "reference_accepted": bool(
                        reference_block.accepted_mask[token_in_block, rank]
                    ),
                    "actual_contribution": actual_contributions[
                        token_index, rank
                    ].tolist(),
                    "reference_contribution": reference.contributions[
                        token_index, rank
                    ].tolist(),
                }
            )
        report["first_diff_details"] = {
            "token_index": token_index,
            "block_index": block_index,
            "token_in_block": token_in_block,
            "actual_gate_distribution": actual_gate.probabilities[
                token_index
            ].tolist(),
            "reference_gate_distribution": reference.gate.probabilities[
                token_index
            ].tolist(),
            "actual_output": actual_outputs[token_index].tolist(),
            "reference_output": reference.outputs[token_index].tolist(),
            "expert_contributions": rank_details,
        }
        if raise_on_mismatch:
            raise MoeError(
                code=E_BLOCK_MISMATCH,
                message="block-routed output does not match full reference",
                case_id=case.case_id,
                block_index=block_index,
                details=report,
            )
    return report


def _gate_artifact(gate: GateResult) -> dict[str, Any]:
    return {
        "raw_scores": gate.raw_scores,
        "probabilities": gate.probabilities,
        "selected_experts": gate.selected_experts,
        "selected_gate": gate.selected_gate,
    }


def _assignment_artifact(assignment: CapacityAssignment) -> dict[str, Any]:
    return {
        "capacity_per_expert": assignment.capacity_per_expert,
        "accepted_mask": assignment.accepted_mask,
        "accepted_per_expert": list(assignment.accepted_per_expert),
        "overflow": list(assignment.overflow),
        "capacity_summary": assignment.capacity_summary,
    }


def _output_artifact(block: BlockResult) -> dict[str, Any]:
    return {
        "outputs": block.outputs,
        "expert_revisions_used": block.expert_revisions_used,
        "accepted_expert_revisions": block.accepted_expert_revisions,
    }


def build_dependencies(
    case: CaseSpec,
    bundle: WeightBundle,
    *,
    block_index: int,
    token_start: int,
    token_end: int,
) -> dict[str, Any]:
    return {
        "format_version": FORMAT_VERSION,
        "case_id": case.case_id,
        "block_index": block_index,
        "token_start": token_start,
        "token_end": token_end,
        "token_content_hash": bundle.token_content_hash,
        "gate_weights_digest": bundle.gate_weights_digest,
        "router_revision": case.router_revision,
        "expert_revisions": bundle.expert_bank.revision_map,
        "expert_bank_digest": bundle.expert_bank.digest,
        "top_k": case.top_k,
        "capacity_factor": case.capacity_factor,
        "block_size": case.block_size,
        "hidden_dim": case.hidden_dim,
        "num_experts": case.num_experts,
        "dtype": case.dtype,
    }


def compute_block_for_index(
    bundle: WeightBundle, block_index: int
) -> BlockResult:
    case = bundle.case
    token_start = block_index * case.block_size
    token_end = min(case.token_len, token_start + case.block_size)
    block_gate = gate_tokens(
        bundle.tokens[token_start:token_end],
        bundle.gate_weights,
        case.top_k,
    )
    assignment = assign_capacity(
        block_gate,
        token_start=token_start,
        block_index=block_index,
        capacity_factor=case.capacity_factor,
        num_experts=case.num_experts,
    )
    return build_block_result(
        block_index=block_index,
        token_start=token_start,
        tokens=bundle.tokens[token_start:token_end],
        gate=block_gate,
        assignment=assignment,
        expert_bank=bundle.expert_bank,
    )


class RoutingSession:
    """Incremental block router with per-block readiness queries."""

    def __init__(
        self,
        bundle: WeightBundle,
        cache: Any,
        *,
        limits: ResourceLimits | None = None,
        previous_revision: int | None = None,
    ) -> None:
        self.bundle = bundle
        self.case = bundle.case
        self.cache = cache
        self.limits = limits or ResourceLimits()
        validate_case(self.case)
        validate_tokens_and_gate(self.case, bundle.tokens, bundle.gate_weights)
        validate_expert_bank(self.case, bundle.expert_bank)
        validate_revision(previous_revision, self.case.router_revision)
        self.block_count = self.case.block_count
        self.blocks: dict[int, BlockResult] = {}
        self.next_block_index = 0
        self.resident_bytes = 0
        self.started_ns = time.monotonic_ns()
        self.first_block_ready_ns: int | None = None
        self.first_block_query_ns: int | None = None

    def _dependencies(self, block_index: int) -> tuple[int, int, dict[str, Any]]:
        token_start = block_index * self.case.block_size
        token_end = min(self.case.token_len, token_start + self.case.block_size)
        return (
            token_start,
            token_end,
            build_dependencies(
                self.case,
                self.bundle,
                block_index=block_index,
                token_start=token_start,
                token_end=token_end,
            ),
        )

    def _store_block(
        self,
        block: BlockResult,
        dependencies: dict[str, Any],
        *,
        reused_from: dict[str, str] | None = None,
    ) -> None:
        gate_record = self.cache.store_artifact(
            kind="gate_distribution",
            case_id=self.case.case_id,
            block_index=block.block_index,
            dependencies=dependencies,
            artifact=_gate_artifact(block.gate),
            reused_from=(reused_from or {}).get("gate_distribution"),
        )
        assignment_record = self.cache.store_artifact(
            kind="expert_assignment",
            case_id=self.case.case_id,
            block_index=block.block_index,
            dependencies=dependencies,
            artifact=_assignment_artifact(block.assignment),
            reused_from=(reused_from or {}).get("expert_assignment"),
        )
        output_record = self.cache.store_artifact(
            kind="block_output",
            case_id=self.case.case_id,
            block_index=block.block_index,
            dependencies=dependencies,
            artifact=_output_artifact(block),
            reused_from=(reused_from or {}).get("block_output"),
        )
        metadata = {
            "case_id": self.case.case_id,
            "block_index": block.block_index,
            "token_start": block.token_start,
            "token_end": block.token_end,
            "router_revision": self.case.router_revision,
            "expert_bank_digest": self.bundle.expert_bank.digest,
            "expert_revisions": block.expert_revisions_used,
            "accepted_expert_revisions": block.accepted_expert_revisions,
            "capacity_summary": block.assignment.capacity_summary,
            "gate_content_hash": gate_record["content_hash"],
            "assignment_content_hash": assignment_record["content_hash"],
            "output_content_hash": output_record["content_hash"],
        }
        metadata_record = self.cache.store_artifact(
            kind="block_metadata",
            case_id=self.case.case_id,
            block_index=block.block_index,
            dependencies=dependencies,
            artifact=metadata,
            reused_from=(reused_from or {}).get("block_metadata"),
        )
        block.metadata = metadata
        block.cache_records = {
            "gate_distribution": gate_record,
            "expert_assignment": assignment_record,
            "block_output": output_record,
            "block_metadata": metadata_record,
        }

    def compute_next_block(self) -> BlockResult:
        if self.next_block_index >= self.block_count:
            raise MoeError(
                code="E_NO_MORE_BLOCKS",
                message="all routing blocks are already ready",
                case_id=self.case.case_id,
                details={"block_count": self.block_count},
            )
        block_index = self.next_block_index
        token_start, token_end, dependencies = self._dependencies(block_index)
        projected = enforce_memory_limit(
            self.resident_bytes, token_end - token_start, self.case, self.limits
        )
        block = compute_block_for_index(self.bundle, block_index)
        self._store_block(block, dependencies)
        self.blocks[block_index] = block
        self.next_block_index += 1
        self.resident_bytes = projected
        if block_index == 0:
            self.first_block_ready_ns = time.monotonic_ns()
        return block

    def commit_computed_block(self, block: BlockResult) -> BlockResult:
        """Commit a block computed elsewhere, preserving declaration order."""

        if block.block_index != self.next_block_index:
            raise MoeError(
                code=E_INVALID_INPUT,
                message="computed blocks must be committed in token order",
                case_id=self.case.case_id,
                block_index=block.block_index,
                field="block_index",
                details={"expected_block_index": self.next_block_index},
            )
        token_start, token_end, dependencies = self._dependencies(block.block_index)
        if block.token_start != token_start or block.token_end != token_end:
            raise MoeError(
                code=E_INVALID_INPUT,
                message="computed block token range does not match the new case",
                case_id=self.case.case_id,
                block_index=block.block_index,
                field="block_index",
            )
        projected = enforce_memory_limit(
            self.resident_bytes, token_end - token_start, self.case, self.limits
        )
        block.ready_ns = time.monotonic_ns()
        self._store_block(block, dependencies)
        self.blocks[block.block_index] = block
        self.next_block_index += 1
        self.resident_bytes = projected
        if block.block_index == 0:
            self.first_block_ready_ns = time.monotonic_ns()
        return block

    def adopt_reused_block(
        self,
        block: BlockResult,
        source_records: dict[str, dict[str, Any]],
    ) -> BlockResult:
        """Re-address an unaffected committed block under the new cache key."""

        if block.block_index != self.next_block_index:
            raise MoeError(
                code=E_INVALID_INPUT,
                message="reused blocks must be adopted in token order",
                case_id=self.case.case_id,
                block_index=block.block_index,
                field="block_index",
            )
        _token_start, token_end, dependencies = self._dependencies(block.block_index)
        projected = enforce_memory_limit(
            self.resident_bytes, token_end - block.token_start, self.case, self.limits
        )
        reused_from = {
            kind: record["key"] for kind, record in source_records.items()
        }
        adapted = BlockResult(
            block_index=block.block_index,
            token_start=block.token_start,
            token_end=block.token_end,
            gate=block.gate,
            assignment=block.assignment,
            outputs=block.outputs,
            contributions=block.contributions,
            expert_revisions_used=block.expert_revisions_used,
            accepted_expert_revisions=block.accepted_expert_revisions,
            ready_ns=time.monotonic_ns(),
            reused=True,
        )
        self._store_block(adapted, dependencies, reused_from=reused_from)
        self.blocks[adapted.block_index] = adapted
        self.next_block_index += 1
        self.resident_bytes = projected
        if adapted.block_index == 0:
            self.first_block_ready_ns = time.monotonic_ns()
        return adapted

    def route_all(self) -> "RoutingSession":
        while self.next_block_index < self.block_count:
            self.compute_next_block()
        return self

    def query_block(self, block_index: int) -> dict[str, Any]:
        if not 0 <= block_index < self.block_count:
            raise MoeError(
                code=E_INVALID_INPUT,
                message="block_index is outside this case",
                case_id=self.case.case_id,
                field="block_index",
                block_index=block_index,
                details={"block_count": self.block_count},
            )
        block = self.blocks.get(block_index)
        if block is None:
            raise MoeError(
                code=E_BLOCK_NOT_READY,
                message="block output is not ready yet",
                case_id=self.case.case_id,
                block_index=block_index,
                details={
                    "ready_blocks": sorted(self.blocks),
                    "next_block_index": self.next_block_index,
                },
            )
        self.first_block_query_ns = time.monotonic_ns()
        query_ms = None
        if block_index == 0 and self.first_block_ready_ns is not None:
            query_ms = (self.first_block_query_ns - self.started_ns) / 1_000_000.0
        return {
            "case_id": self.case.case_id,
            "block_index": block_index,
            "token_start": block.token_start,
            "token_end": block.token_end,
            "outputs": block.outputs.copy(),
            "gate": {
                "probabilities": block.gate.probabilities.copy(),
                "selected_experts": block.gate.selected_experts.copy(),
                "selected_gate": block.gate.selected_gate.copy(),
            },
            "assignment": {
                "capacity_per_expert": block.assignment.capacity_per_expert,
                "accepted_mask": block.assignment.accepted_mask.copy(),
                "accepted_per_expert": list(block.assignment.accepted_per_expert),
                "overflow": list(block.assignment.overflow),
                "capacity_summary": block.assignment.capacity_summary,
            },
            "expert_revisions_used": dict(block.expert_revisions_used),
            "accepted_expert_revisions": dict(block.accepted_expert_revisions),
            "capacity_summary": block.assignment.capacity_summary,
            "metadata": dict(block.metadata),
            "reused": block.reused,
            "reused_from": {
                kind: record.get("reused_from")
                for kind, record in block.cache_records.items()
            },
            "cache_records": {
                kind: dict(record) for kind, record in block.cache_records.items()
            },
            "first_block_query_ms": query_ms,
            "ready": True,
        }

    def iter_ready_blocks(self) -> list[dict[str, Any]]:
        return [self.query_block(index) for index in sorted(self.blocks)]

    def assembled_outputs(self) -> np.ndarray:
        if len(self.blocks) != self.block_count:
            raise MoeError(
                code=E_BLOCK_NOT_READY,
                message="cannot assemble outputs before every block is ready",
                case_id=self.case.case_id,
                details={
                    "ready_blocks": len(self.blocks),
                    "block_count": self.block_count,
                },
            )
        return np.concatenate(
            [self.blocks[index].outputs for index in range(self.block_count)], axis=0
        )

    def assembled_gate(self) -> GateResult:
        if len(self.blocks) != self.block_count:
            raise MoeError(
                code=E_BLOCK_NOT_READY,
                message="cannot assemble gate results before every block is ready",
                case_id=self.case.case_id,
            )
        return GateResult(
            raw_scores=np.concatenate(
                [self.blocks[index].gate.raw_scores for index in range(self.block_count)],
                axis=0,
            ),
            probabilities=np.concatenate(
                [
                    self.blocks[index].gate.probabilities
                    for index in range(self.block_count)
                ],
                axis=0,
            ),
            selected_experts=np.concatenate(
                [
                    self.blocks[index].gate.selected_experts
                    for index in range(self.block_count)
                ],
                axis=0,
            ),
            selected_gate=np.concatenate(
                [
                    self.blocks[index].gate.selected_gate
                    for index in range(self.block_count)
                ],
                axis=0,
            ),
        )

    def assembled_assignments(self) -> tuple[CapacityAssignment, ...]:
        return tuple(self.blocks[index].assignment for index in range(self.block_count))

    def assembled_contributions(self) -> np.ndarray:
        return np.concatenate(
            [
                self.blocks[index].contributions
                for index in range(self.block_count)
            ],
            axis=0,
        )

    def consistency_report(
        self, *, tolerance: float = 1e-12, raise_on_mismatch: bool = True
    ) -> dict[str, Any]:
        reference = compute_full_reference(self.bundle)
        return compare_routes(
            self.case,
            actual_outputs=self.assembled_outputs(),
            actual_gate=self.assembled_gate(),
            actual_assignments=self.assembled_assignments(),
            actual_contributions=self.assembled_contributions(),
            reference=reference,
            tolerance=tolerance,
            raise_on_mismatch=raise_on_mismatch,
        )

    @property
    def first_block_query_ms(self) -> float | None:
        first = self.blocks.get(0)
        if first is None or self.first_block_ready_ns is None:
            return None
        return (self.first_block_ready_ns - self.started_ns) / 1_000_000.0
