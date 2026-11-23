"""Arbitration engine, DAG cache wiring, and deterministic run reports."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np

from .attention import (
    BlockScoreResult,
    NormalizationResult,
    compare_variant_scores,
    compute_block_scores,
    compute_online_normalization,
    estimate_attention_memory,
    hash_block_content,
)
from .cache import ContentAddressedCache, NodeAccess
from .errors import (
    DomainError,
    E_BLOCK_MISMATCH,
    E_MAPPING_DRIFT,
    E_MAPPING_UNDECLARED,
    E_RESOURCE_LIMIT,
    as_domain_error,
)
from .hashing import hash_arrays
from .models import CaseSpec, ResolvedCase, VariantSnapshot
from .variants import (
    MappingJudgement,
    MappingRegistry,
    PAIRING_CONVENTION,
    ROPE_IMPLEMENTATION,
    adjudicate_angles,
    build_builtin_mapping_registry,
    compute_angles,
    generate_qkv,
    rotate_qk,
)


NODE_NAMES = ("angles", "rotated_qk", "block_scores", "normalization_stats")
NODE_KEY_SCHEMA = "rope-node-key-v1"


@dataclass(frozen=True)
class EngineLimits:
    max_attention_memory_bytes: int = 128 * 1024 * 1024
    max_parallel_recompute_nodes: int = 4
    mapping_tolerance: float = 1e-9
    block_tolerance: float = 1e-9
    score_tolerance: float = 1e-6

    def __post_init__(self) -> None:
        if self.max_attention_memory_bytes < 1:
            raise ValueError("max_attention_memory_bytes must be positive")
        if self.max_parallel_recompute_nodes < 1:
            raise ValueError("max_parallel_recompute_nodes must be positive")
        for value in (
            self.mapping_tolerance,
            self.block_tolerance,
            self.score_tolerance,
        ):
            if value < 0:
                raise ValueError("tolerances must be non-negative")


@dataclass(frozen=True)
class _CaseExecution:
    case_id: str
    report: dict[str, Any]
    accesses: tuple[NodeAccess, ...]


def _node_key_components(
    *,
    node: str,
    case: ResolvedCase,
    variant: str,
    content_hashes: Mapping[str, str] | None = None,
    block_content_hash: str | None = None,
    upstream: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    content_hashes = dict(content_hashes or {})
    upstream = {name: dict(value) for name, value in (upstream or {}).items()}
    uses_scale = variant in {"position_interpolation", "ntk_aware"}
    uses_freq_block = variant == "block_frequency"
    uses_block_size = node in {"block_scores", "normalization_stats"}
    return {
        "schema": NODE_KEY_SCHEMA,
        "node": node,
        "case_id": case.case_id,
        "variant": variant,
        "base_theta": case.base_theta,
        "scale_ratio": case.scale_ratio if uses_scale else None,
        "freq_block": list(case.freq_block) if uses_freq_block else None,
        "block_size": case.block_size if uses_block_size else None,
        "causal": case.causal,
        "seq_len": case.seq_len,
        "head_dim": case.head_dim,
        "num_heads": case.num_heads,
        "content_hashes": content_hashes,
        "block_content_hash": block_content_hash,
        "upstream_hashes": {
            name: value["artifact_hash"] for name, value in upstream.items()
        },
        "upstream": upstream,
        # Snapshot version is deliberately absent.
    }


def _upstream_reference(access: NodeAccess) -> dict[str, str]:
    return {"key": access.key, "artifact_hash": access.artifact_hash}


class ArbiterEngine:
    def __init__(
        self,
        cache: ContentAddressedCache,
        mapping_registry: MappingRegistry | None = None,
        limits: EngineLimits | None = None,
    ):
        self.cache = cache
        self.mapping_registry = mapping_registry or build_builtin_mapping_registry()
        self.limits = limits or EngineLimits()

    def adjudicate(self, case: CaseSpec, snapshot: VariantSnapshot) -> dict[str, Any]:
        return self._execute_case(case, snapshot).report

    def run(
        self,
        cases: Iterable[CaseSpec],
        snapshot: VariantSnapshot,
        parallelism: int = 1,
    ) -> dict[str, Any]:
        case_list = list(cases)
        if parallelism < 1:
            raise ValueError("parallelism must be positive")
        effective_parallelism = min(
            parallelism,
            self.limits.max_parallel_recompute_nodes,
            max(len(case_list), 1),
        )
        degradations: list[dict[str, Any]] = []
        if parallelism > self.limits.max_parallel_recompute_nodes:
            degradations.append(
                {
                    "category": "PARALLEL_RECOMPUTE_REDUCED",
                    "requested_parallel_nodes": parallelism,
                    "applied_parallel_nodes": effective_parallelism,
                    "limit": self.limits.max_parallel_recompute_nodes,
                    "policy": "reduce_parallelism_keep_node_hashes",
                }
            )
        snapshot.verify()
        executions: list[_CaseExecution] = []
        if effective_parallelism == 1:
            for case in case_list:
                executions.append(self._execute_case_safe(case, snapshot))
        else:
            with ThreadPoolExecutor(max_workers=effective_parallelism) as executor:
                futures = [
                    executor.submit(self._execute_case_safe, case, snapshot)
                    for case in case_list
                ]
                for future in futures:
                    executions.append(future.result())

        recomputed: list[dict[str, Any]] = []
        reused: list[dict[str, Any]] = []
        node_hashes: dict[str, str] = {}
        node_keys: dict[str, str] = {}
        for execution in executions:
            for access in execution.accesses:
                node_id = f"{access.case_id}::{access.variant}::{access.node}"
                summary = access.summary()
                summary["node_id"] = node_id
                if access.reused:
                    reused.append(summary)
                else:
                    recomputed.append(summary)
                node_hashes[node_id] = access.artifact_hash
                node_keys[node_id] = access.key
        recomputed.sort(key=lambda item: item["node_id"])
        reused.sort(key=lambda item: item["node_id"])
        node_hashes = dict(sorted(node_hashes.items()))
        node_keys = dict(sorted(node_keys.items()))

        errors: list[dict[str, Any]] = []
        for execution in executions:
            errors.extend(execution.report.get("errors", []))
        errors.sort(
            key=lambda item: (
                item.get("context", {}).get("case_id", ""),
                item.get("code", ""),
            )
        )
        business_codes = sorted(
            {
                execution.report.get("business_code", "OK")
                for execution in executions
            }
            | {error["code"] for error in errors}
        )
        recompute_cases = sorted(
            {item["case_id"] for item in recomputed}
        )
        reuse_cases = sorted({item["case_id"] for item in reused})
        all_nodes = [item["node_id"] for item in recomputed + reused]
        return {
            "schema": "rope-arbitration-run-v1",
            "snapshot": {
                "version": snapshot.version,
                "content_checksum": snapshot.content_checksum,
                "case_count": len(snapshot.cases),
            },
            "execution": {
                "requested_parallel_nodes": parallelism,
                "applied_parallel_nodes": effective_parallelism,
                "degradations": degradations,
            },
            "case_count": len(executions),
            "cases": [execution.report for execution in executions],
            "recomputed_nodes": recomputed,
            "reused_nodes": reused,
            "recompute_cases": recompute_cases,
            "reuse_cases": reuse_cases,
            "recompute_set": sorted(
                item["node_id"] for item in recomputed
            ),
            "reuse_set": sorted(item["node_id"] for item in reused),
            "all_node_ids": sorted(all_nodes),
            "node_hashes": node_hashes,
            "node_keys": node_keys,
            "business_codes": business_codes or ["OK"],
            "errors": errors,
            "cache": {
                "entry_count": self.cache.entry_count,
                "entry_limit": self.cache.max_entries,
            },
        }

    def _execute_case_safe(
        self, case: CaseSpec, snapshot: VariantSnapshot
    ) -> _CaseExecution:
        try:
            return self._execute_case(case, snapshot)
        except DomainError as exc:
            if exc.code in {E_RESOURCE_LIMIT}:
                raise
            return _CaseExecution(
                case_id=case.case_id,
                report={
                    "case_id": case.case_id,
                    "status": "ERROR",
                    "business_code": exc.code,
                    "errors": [exc.to_dict()],
                    "variants": {},
                },
                accesses=(),
            )
        except Exception as exc:
            error = as_domain_error(exc)
            return _CaseExecution(
                case_id=case.case_id,
                report={
                    "case_id": case.case_id,
                    "status": "ERROR",
                    "business_code": error.code,
                    "errors": [error.to_dict()],
                    "variants": {},
                },
                accesses=(),
            )

    def _execute_case(
        self, case_spec: CaseSpec, snapshot: VariantSnapshot
    ) -> _CaseExecution:
        case = ResolvedCase.resolve(case_spec, snapshot)
        qkv = generate_qkv(case)
        qkv_hashes = {
            name: hash_arrays({name: array}) for name, array in qkv.items()
        }
        variant_reports: dict[str, Any] = {}
        accesses: list[NodeAccess] = []
        angle_arrays: dict[str, np.ndarray] = {}
        score_arrays: dict[str, np.ndarray] = {}
        output_arrays: dict[str, np.ndarray] = {}
        normalization_reports: dict[str, Any] = {}
        block_reports: dict[str, Any] = {}
        case_errors: list[dict[str, Any]] = []

        memory_estimate = estimate_attention_memory(case)
        if memory_estimate["peak_bytes"] > self.limits.max_attention_memory_bytes:
            raise DomainError(
                E_RESOURCE_LIMIT,
                "attention memory estimate exceeds the configured limit",
                {
                    "case_id": case.case_id,
                    "variant": "*",
                    "required_bytes": memory_estimate["peak_bytes"],
                    "limit_bytes": self.limits.max_attention_memory_bytes,
                    "estimate": memory_estimate,
                    "degraded_category": "NONE",
                },
            )

        for variant in case.variant_pair:
            angle_components = _node_key_components(
                node="angles", case=case, variant=variant
            )
            angle_access = self.cache.get_or_compute(
                case_id=case.case_id,
                variant=variant,
                node="angles",
                key_components=angle_components,
                snapshot_version=snapshot.version,
                compute=lambda variant=variant: (
                    {
                        "variant": variant,
                        "pairing_convention": PAIRING_CONVENTION,
                        "rope_implementation": ROPE_IMPLEMENTATION,
                        "base_theta": case.base_theta,
                        "scale_ratio": (
                            case.scale_ratio
                            if variant in {"position_interpolation", "ntk_aware"}
                            else None
                        ),
                        "freq_block": (
                            list(case.freq_block)
                            if variant == "block_frequency"
                            else None
                        ),
                    },
                    {"angles": compute_angles(case, variant)},
                ),
            )
            accesses.append(angle_access)
            angles = angle_access.arrays["angles"]
            angle_arrays[variant] = angles

            rotated_components = _node_key_components(
                node="rotated_qk",
                case=case,
                variant=variant,
                content_hashes={"q": qkv_hashes["q"], "k": qkv_hashes["k"]},
                upstream={"angles": _upstream_reference(angle_access)},
            )
            rotated_access = self.cache.get_or_compute(
                case_id=case.case_id,
                variant=variant,
                node="rotated_qk",
                key_components=rotated_components,
                snapshot_version=snapshot.version,
                compute=lambda angles=angles: self._rotated_artifact(qkv, angles),
            )
            accesses.append(rotated_access)
            q_rotated = rotated_access.arrays["q_rotated"]
            k_rotated = rotated_access.arrays["k_rotated"]

            block_content_hash = hash_block_content(
                q_rotated, k_rotated, case.block_size, case.causal
            )
            block_components = _node_key_components(
                node="block_scores",
                case=case,
                variant=variant,
                content_hashes={"q": qkv_hashes["q"], "k": qkv_hashes["k"]},
                block_content_hash=block_content_hash,
                upstream={"rotated_qk": _upstream_reference(rotated_access)},
            )
            block_access = self.cache.get_or_compute(
                case_id=case.case_id,
                variant=variant,
                node="block_scores",
                key_components=block_components,
                snapshot_version=snapshot.version,
                compute=lambda q_rotated=q_rotated, k_rotated=k_rotated: (
                    self._block_artifact(
                        compute_block_scores(
                            q_rotated,
                            k_rotated,
                            case.block_size,
                            case.causal,
                            tolerance=self.limits.block_tolerance,
                        )
                    )
                ),
            )
            accesses.append(block_access)
            block_result = self._restore_block_result(block_access)
            score_arrays[variant] = block_result.scores

            norm_components = _node_key_components(
                node="normalization_stats",
                case=case,
                variant=variant,
                content_hashes={
                    "q": qkv_hashes["q"],
                    "k": qkv_hashes["k"],
                    "v": qkv_hashes["v"],
                },
                block_content_hash=block_content_hash,
                upstream={
                    "rotated_qk": _upstream_reference(rotated_access),
                    "block_scores": _upstream_reference(block_access),
                },
            )
            norm_access = self.cache.get_or_compute(
                case_id=case.case_id,
                variant=variant,
                node="normalization_stats",
                key_components=norm_components,
                snapshot_version=snapshot.version,
                compute=lambda block_result=block_result: self._norm_artifact(
                    compute_online_normalization(
                        block_result,
                        qkv["v"],
                        case.causal,
                        tolerance=self.limits.block_tolerance,
                    )
                ),
            )
            accesses.append(norm_access)
            normalization = self._restore_normalization(norm_access)
            output_arrays[variant] = normalization.attention_output
            normalization_reports[variant] = {
                "code": normalization.code,
                "equivalent": normalization.equivalent,
                "block_score_max_abs_error": normalization.block_score_max_abs_error,
                "attention_output_max_abs_error": normalization.attention_output_max_abs_error,
                "normalization_max_abs_error": normalization.normalization_max_abs_error,
                "first_mismatch": normalization.first_mismatch,
            }
            block_reports[variant] = {
                "code": block_result.code,
                "equivalent": block_result.code == "OK",
                "block_score_max_abs_error": block_result.block_score_max_abs_error,
                "first_mismatch": block_result.first_score_mismatch,
                "effective_block_size": block_result.effective_block_size,
                "block_count": len(block_result.query_block_starts),
                "block_content_hash": block_result.block_content_hash,
            }
            variant_reports[variant] = {
                "variant": variant,
                "nodes": {
                    "angles": angle_access.summary(),
                    "rotated_qk": rotated_access.summary(),
                    "block_scores": block_access.summary(),
                    "normalization_stats": norm_access.summary(),
                },
                "attention": {
                    "shape": list(normalization.attention_output.shape),
                    "output_hash": hash_arrays(
                        {"attention_output": normalization.attention_output}
                    ),
                },
            }
            if block_result.code != "OK":
                case_errors.append(
                    {
                        "code": E_BLOCK_MISMATCH,
                        "message": "block path and full score matrix disagree",
                        "context": {
                            "case_id": case.case_id,
                            "variant": variant,
                            **block_result.first_score_mismatch,
                        },
                    }
                )
            if normalization.code != "OK":
                case_errors.append(
                    {
                        "code": E_BLOCK_MISMATCH,
                        "message": "block path and full attention path disagree",
                        "context": {
                            "case_id": case.case_id,
                            "variant": variant,
                            **normalization.first_mismatch,
                        },
                    }
                )

        source_variant, target_variant = case.variant_pair
        mapping = adjudicate_angles(
            case,
            source_variant,
            target_variant,
            angle_arrays[source_variant],
            angle_arrays[target_variant],
            self.mapping_registry,
            tolerance=self.limits.mapping_tolerance,
        )
        if mapping.code == E_MAPPING_UNDECLARED:
            case_errors.append(
                {
                    "code": E_MAPPING_UNDECLARED,
                    "message": "no angle mapping declaration exists for the variant pair",
                    "context": {
                        "case_id": case.case_id,
                        "variant": target_variant,
                        "source_variant": source_variant,
                        "dimension_index": 0,
                        "deviation": 0.0,
                    },
                }
            )
        elif mapping.code == E_MAPPING_DRIFT:
            case_errors.append(
                {
                    "code": E_MAPPING_DRIFT,
                    "message": "observed angle mapping exceeds tolerance",
                    "context": {
                        "case_id": case.case_id,
                        "variant": target_variant,
                        **(mapping.first_violation or {}),
                    },
                }
            )

        cross_scores = compare_variant_scores(
            score_arrays[source_variant],
            score_arrays[target_variant],
            case.causal,
            self.limits.score_tolerance,
        )
        output_difference = np.abs(
            output_arrays[source_variant] - output_arrays[target_variant]
        )
        output_max_deviation = float(np.max(output_difference))
        output_equivalent = output_max_deviation <= self.limits.score_tolerance
        block_path_equivalent = all(
            item["equivalent"] for item in block_reports.values()
        ) and all(item["equivalent"] for item in normalization_reports.values())
        angle_equivalent = mapping.equivalent
        equivalent = (
            angle_equivalent
            and block_path_equivalent
            and cross_scores["equivalent"]
            and output_equivalent
        )
        business_code = case_errors[0]["code"] if case_errors else "OK"
        report = {
            "case_id": case.case_id,
            "status": "OK" if not case_errors else "ERROR",
            "business_code": business_code,
            "case": case_spec.to_dict(),
            "resolved": {
                "base_theta": case.base_theta,
                "scale_ratio": case.scale_ratio,
                "freq_block": list(case.freq_block),
                "block_size": case.block_size,
                "causal": case.causal,
                "seed": case.seed,
            },
            "pairing_convention": PAIRING_CONVENTION,
            "qkv_hashes": qkv_hashes,
            "variants": variant_reports,
            "angle_mapping": mapping.to_dict(),
            "block_paths": block_reports,
            "normalization": normalization_reports,
            "cross_variant": {
                "score_max_abs_deviation": cross_scores["max_abs_deviation"],
                "score_equivalent": cross_scores["equivalent"],
                "score_tolerance": cross_scores["tolerance"],
                "ranking_consistency_ratio": cross_scores["ranking_consistency_ratio"],
                "attention_output_max_abs_deviation": output_max_deviation,
                "attention_output_equivalent": output_equivalent,
            },
            "equivalence": {
                "angle_mapping_equivalent": angle_equivalent,
                "block_path_equivalent": block_path_equivalent,
                "score_equivalent": cross_scores["equivalent"],
                "attention_output_equivalent": output_equivalent,
                "equivalent": equivalent,
                "conclusion": "EQUIVALENT" if equivalent else "NOT_EQUIVALENT",
            },
            "errors": case_errors,
            "memory_estimate": memory_estimate,
        }
        return _CaseExecution(
            case_id=case.case_id,
            report=report,
            accesses=tuple(accesses),
        )

    @staticmethod
    def _rotated_artifact(
        qkv: Mapping[str, np.ndarray], angles: np.ndarray
    ) -> tuple[Mapping[str, Any], Mapping[str, np.ndarray]]:
        q_rotated, k_rotated = rotate_qk(qkv["q"], qkv["k"], angles)
        return (
            {
                "pairing_convention": PAIRING_CONVENTION,
                "rope_implementation": ROPE_IMPLEMENTATION,
                "shape": list(q_rotated.shape),
            },
            {"q_rotated": q_rotated, "k_rotated": k_rotated},
        )

    @staticmethod
    def _block_artifact(
        result: BlockScoreResult,
    ) -> tuple[Mapping[str, Any], Mapping[str, np.ndarray]]:
        return result.to_artifact()

    @staticmethod
    def _norm_artifact(
        result: NormalizationResult,
    ) -> tuple[Mapping[str, Any], Mapping[str, np.ndarray]]:
        return result.to_artifact()

    @staticmethod
    def _restore_block_result(access: NodeAccess) -> BlockScoreResult:
        meta = access.meta
        return BlockScoreResult(
            scores=access.arrays["scores"],
            blocks=access.arrays["blocks"],
            query_block_starts=np.asarray(meta["query_block_starts"], dtype=np.int64),
            key_block_starts=np.asarray(meta["key_block_starts"], dtype=np.int64),
            effective_block_size=int(meta["effective_block_size"]),
            block_content_hash=str(meta["block_content_hash"]),
            memory_estimate=dict(meta["memory_estimate"]),
            block_score_max_abs_error=float(meta["block_score_max_abs_error"]),
            first_score_mismatch=meta.get("first_score_mismatch"),
            code=str(meta["code"]),
        )

    @staticmethod
    def _restore_normalization(access: NodeAccess) -> NormalizationResult:
        meta = access.meta
        arrays = access.arrays
        return NormalizationResult(
            attention_output=arrays["attention_output"],
            row_max=arrays["row_max"],
            row_sumexp=arrays["row_sumexp"],
            logsumexp=arrays["logsumexp"],
            entropy=arrays["entropy"],
            block_score_max_abs_error=float(meta["block_score_max_abs_error"]),
            attention_output_max_abs_error=float(
                meta["attention_output_max_abs_error"]
            ),
            normalization_max_abs_error=float(meta["normalization_max_abs_error"]),
            first_mismatch=meta.get("first_mismatch"),
            code=str(meta["code"]),
            equivalent=bool(meta["equivalent"]),
        )
