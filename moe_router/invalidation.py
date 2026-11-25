"""Targeted cache invalidation and suffix recomputation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .errors import E_INVALID_INPUT, E_REVISION_REGRESSION, MoeError
from .models import ResourceLimits, WeightBundle
from .routing import (
    BlockResult,
    RoutingSession,
    compute_block_for_index,
)
from .validation import estimate_block_resident_bytes, validate_revision


@dataclass
class InvalidationResult:
    plan: dict[str, Any]
    session: RoutingSession
    consistency_report: dict[str, Any] | None

    @property
    def outputs(self) -> Any:
        return self.session.assembled_outputs()

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan,
            "consistency": self.consistency_report,
        }


class InvalidationEngine:
    """Reuse unaffected committed blocks and recompute only an affected suffix."""

    def __init__(self, limits: ResourceLimits | None = None) -> None:
        self.limits = limits or ResourceLimits()

    @staticmethod
    def _changed_experts(
        old_session: RoutingSession, new_bundle: WeightBundle
    ) -> list[int]:
        old_bank = old_session.bundle.expert_bank
        new_bank = new_bundle.expert_bank
        if old_bank.num_experts != new_bank.num_experts:
            return list(range(max(old_bank.num_experts, new_bank.num_experts)))
        changed = []
        for expert_id in range(old_bank.num_experts):
            if (
                old_bank.revisions[expert_id] != new_bank.revisions[expert_id]
                or old_bank.expert_digest(expert_id)
                != new_bank.expert_digest(expert_id)
            ):
                changed.append(expert_id)
        return changed

    def _exact_affected_blocks(
        self,
        old_session: RoutingSession,
        new_bundle: WeightBundle,
        changed_experts: list[int],
        *,
        global_invalid: bool,
    ) -> list[int]:
        if global_invalid:
            return list(range(old_session.block_count))
        changed = set(changed_experts)
        if not changed:
            return []
        affected = []
        for block_index in range(old_session.block_count):
            block = old_session.blocks[block_index]
            selected = {int(value) for value in block.gate.selected_experts.ravel()}
            if selected & changed:
                affected.append(block_index)
        return affected

    def _precompute_parallel(
        self,
        new_bundle: WeightBundle,
        block_indices: list[int],
        workers: int,
    ) -> dict[int, BlockResult]:
        if not block_indices:
            return {}
        results: dict[int, BlockResult] = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(compute_block_for_index, new_bundle, block_index): block_index
                for block_index in block_indices
            }
            for future, block_index in futures.items():
                results[block_index] = future.result()
        return results

    def apply(
        self,
        *,
        old_session: RoutingSession,
        new_bundle: WeightBundle,
        new_cache: Any | None = None,
        strategy: str = "suffix",
        parallel_blocks: int = 1,
        check_consistency: bool = True,
        consistency_tolerance: float = 1e-12,
    ) -> InvalidationResult:
        if strategy not in {"suffix", "targeted"}:
            raise MoeError(
                code=E_INVALID_INPUT,
                message="invalidation strategy must be suffix or targeted",
                case_id=new_bundle.case.case_id,
                field="strategy",
                details={"value": strategy},
            )
        if not isinstance(parallel_blocks, int) or isinstance(parallel_blocks, bool) or parallel_blocks < 1:
            raise MoeError(
                code=E_INVALID_INPUT,
                message="parallel_blocks must be a positive integer",
                case_id=new_bundle.case.case_id,
                field="parallel_blocks",
                details={"value": parallel_blocks},
            )
        if len(old_session.blocks) != old_session.block_count:
            raise MoeError(
                code=E_INVALID_INPUT,
                message="old session must have every committed block before invalidation",
                case_id=new_bundle.case.case_id,
                field="old_session",
                details={
                    "ready_blocks": len(old_session.blocks),
                    "block_count": old_session.block_count,
                },
            )
        if new_bundle.case.case_id != old_session.case.case_id:
            raise MoeError(
                code=E_INVALID_INPUT,
                message="invalidation cannot change case_id",
                case_id=new_bundle.case.case_id,
                field="case_id",
            )
        validate_revision(
            old_session.case.router_revision, new_bundle.case.router_revision
        )
        old_expert_bank = old_session.bundle.expert_bank
        new_expert_bank = new_bundle.expert_bank
        if old_expert_bank.num_experts == new_expert_bank.num_experts:
            for expert_id in range(old_expert_bank.num_experts):
                if (
                    new_expert_bank.revisions[expert_id]
                    < old_expert_bank.revisions[expert_id]
                ):
                    raise MoeError(
                        code=E_REVISION_REGRESSION,
                        message="expert revision may not move backwards",
                        case_id=new_bundle.case.case_id,
                        field=f"expert_revisions[{expert_id}]",
                        details={
                            "previous_revision": old_expert_bank.revisions[expert_id],
                            "current_revision": new_expert_bank.revisions[expert_id],
                        },
                    )

        old_case = old_session.case
        new_case = new_bundle.case
        changed_experts = self._changed_experts(old_session, new_bundle)
        token_changed = (
            old_session.bundle.token_content_hash != new_bundle.token_content_hash
            or old_case.token_len != new_case.token_len
            or old_case.hidden_dim != new_case.hidden_dim
            or old_case.seed != new_case.seed
        )
        gate_changed = (
            old_session.bundle.gate_weights_digest
            != new_bundle.gate_weights_digest
        )
        policy_changed = (
            old_case.top_k != new_case.top_k
            or old_case.capacity_factor != new_case.capacity_factor
        )
        block_size_changed = old_case.block_size != new_case.block_size
        global_invalid = (
            token_changed
            or gate_changed
            or policy_changed
            or block_size_changed
            or old_case.num_experts != new_case.num_experts
        )
        affected_experts = (
            list(range(new_case.num_experts)) if global_invalid else changed_experts
        )
        exact_affected = self._exact_affected_blocks(
            old_session,
            new_bundle,
            changed_experts,
            global_invalid=global_invalid,
        )
        if strategy == "suffix":
            if exact_affected:
                first = exact_affected[0]
                recompute_blocks = list(range(first, new_case.block_count))
            else:
                recompute_blocks = []
        else:
            recompute_blocks = list(exact_affected)

        if block_size_changed:
            reuse_possible = False
        else:
            reuse_possible = True
        if not reuse_possible:
            recompute_blocks = list(range(new_case.block_count))
        reused_blocks = [
            index
            for index in range(new_case.block_count)
            if index not in set(recompute_blocks)
        ]

        effective_workers = parallel_blocks
        serialized_due_to_limit = False
        if parallel_blocks > self.limits.max_parallel_recompute_blocks:
            effective_workers = 1
            serialized_due_to_limit = True
        if len(recompute_blocks) <= 1:
            effective_workers = 1
        parallel_bytes = sum(
            estimate_block_resident_bytes(
                new_case,
                min(new_case.block_size, new_case.token_len - index * new_case.block_size),
            )
            for index in recompute_blocks
        )
        if effective_workers > 1 and parallel_bytes > self.limits.max_case_resident_bytes:
            effective_workers = 1
            serialized_due_to_limit = True

        target_cache = new_cache or old_session.cache
        new_session = RoutingSession(
            new_bundle,
            target_cache,
            limits=self.limits,
            previous_revision=old_case.router_revision,
        )

        precomputed: dict[int, BlockResult] = {}
        if effective_workers > 1:
            precomputed = self._precompute_parallel(
                new_bundle, recompute_blocks, effective_workers
            )

        recompute_set = set(recompute_blocks)
        reused_set = set(reused_blocks)
        for block_index in range(new_case.block_count):
            if block_index in precomputed:
                new_session.commit_computed_block(precomputed[block_index])
            elif block_index in recompute_set:
                new_session.compute_next_block()
            elif block_index in reused_set and block_index < old_session.block_count:
                old_block = old_session.blocks[block_index]
                new_session.adopt_reused_block(
                    old_block, old_block.cache_records
                )
            else:
                new_session.compute_next_block()

        consistency = None
        if check_consistency:
            consistency = new_session.consistency_report(
                tolerance=consistency_tolerance
            )

        plan = {
            "case_id": new_case.case_id,
            "strategy": strategy,
            "old_router_revision": old_case.router_revision,
            "new_router_revision": new_case.router_revision,
            "changed_experts": changed_experts,
            "affected_experts": affected_experts,
            "exact_affected_token_blocks": exact_affected,
            "affected_token_blocks": recompute_blocks,
            "recompute_blocks": recompute_blocks,
            "reused_blocks": reused_blocks,
            "recompute_suffix_from": recompute_blocks[0] if recompute_blocks else None,
            "token_changed": token_changed,
            "gate_changed": gate_changed,
            "policy_changed": policy_changed,
            "block_size_changed": block_size_changed,
            "requested_parallel_blocks": parallel_blocks,
            "effective_parallel_blocks": effective_workers,
            "serialized_due_to_limit": serialized_due_to_limit,
            "max_parallel_recompute_blocks": self.limits.max_parallel_recompute_blocks,
        }
        return InvalidationResult(
            plan=plan,
            session=new_session,
            consistency_report=consistency,
        )
