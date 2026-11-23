"""End-to-end sample workflow and directional invalidation verification."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .cache import ContentAddressedCache
from .engine import ArbiterEngine, EngineLimits, NODE_NAMES
from .models import CaseSpec, SnapshotStore, VariantSnapshot
from .variants import MappingRegistry, build_builtin_mapping_registry


@dataclass(frozen=True)
class SnapshotStep:
    name: str
    description: str
    snapshot: VariantSnapshot
    changed_parameters: Mapping[str, Mapping[str, Any]]


def _find_case(
    cases: list[CaseSpec], variant: str, *, exclude: set[str] | None = None
) -> CaseSpec:
    exclude = exclude or set()
    for case in cases:
        if case.case_id not in exclude and variant in case.variant_pair:
            return case
    raise ValueError(f"sample has no case using variant {variant}")


def build_snapshot_steps(
    cases: Iterable[CaseSpec],
    start_version: int,
) -> list[SnapshotStep]:
    case_list = list(cases)
    current = VariantSnapshot.from_cases(case_list, start_version)
    steps = [
        SnapshotStep(
            name="baseline",
            description="initial full variant snapshot",
            snapshot=current,
            changed_parameters={},
        )
    ]
    used: set[str] = set()

    def append(
        name: str,
        description: str,
        case: CaseSpec,
        changes: Mapping[str, Any],
    ) -> None:
        nonlocal current
        previous = current
        current = VariantSnapshot.with_overrides(
            current,
            current.version + 1,
            {case.case_id: changes},
        )
        steps.append(
            SnapshotStep(
                name=name,
                description=description,
                snapshot=current,
                changed_parameters={case.case_id: dict(changes)},
            )
        )
        used.add(case.case_id)

    pi_case = _find_case(case_list, "position_interpolation")
    append(
        "scale_ratio_pi",
        "change scale_ratio for the position-interpolation case",
        pi_case,
        {"scale_ratio": current.cases[pi_case.case_id].scale_ratio * 2.0},
    )
    used.add(pi_case.case_id)

    ntk_case = _find_case(case_list, "ntk_aware", exclude=used)
    append(
        "scale_ratio_ntk",
        "change scale_ratio for the NTK-aware case",
        ntk_case,
        {"scale_ratio": current.cases[ntk_case.case_id].scale_ratio * 2.0},
    )

    block_case = _find_case(case_list, "block_frequency")
    old_blocks = current.cases[block_case.case_id].freq_block
    new_blocks = tuple(max(0.05, value * 0.8) for value in old_blocks)
    append(
        "freq_block",
        "change the frequency-block scale table only",
        block_case,
        {"freq_block": new_blocks},
    )

    size_case = case_list[0]
    append(
        "block_size",
        "change attention block_size for both variants of one case",
        size_case,
        {"block_size": current.cases[size_case.case_id].block_size + 1},
    )

    theta_case = case_list[0]
    append(
        "base_theta",
        "change base_theta, invalidating every variant node and downstream node",
        theta_case,
        {"base_theta": current.cases[theta_case.case_id].base_theta * 2.0},
    )

    noop = VariantSnapshot.create(current.version + 1, current.cases)
    steps.append(
        SnapshotStep(
            name="version_only",
            description="increment the snapshot version without semantic changes",
            snapshot=noop,
            changed_parameters={},
        )
    )
    current = noop
    return steps


def expected_recomputed_nodes(
    cases: Iterable[CaseSpec],
    changed_parameters: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    cases_by_id = {case.case_id: case for case in cases}
    expected: set[str] = set()
    for case_id, changes in changed_parameters.items():
        case = cases_by_id[case_id]
        for variant in case.variant_pair:
            affected_nodes: set[str] = set()
            if "base_theta" in changes:
                affected_nodes.update(NODE_NAMES)
            else:
                if "scale_ratio" in changes and variant in {
                    "position_interpolation",
                    "ntk_aware",
                }:
                    affected_nodes.update(NODE_NAMES)
                if "freq_block" in changes and variant == "block_frequency":
                    affected_nodes.update(NODE_NAMES)
                if "block_size" in changes:
                    affected_nodes.update(
                        {"block_scores", "normalization_stats"}
                    )
            for node in affected_nodes:
                expected.add(f"{case_id}::{variant}::{node}")
    return expected


def _fresh_recompute_hashes(
    cases: list[CaseSpec],
    snapshot: VariantSnapshot,
    limits: EngineLimits,
    parallelism: int,
    registry: MappingRegistry,
    minimum_cache_entries: int,
) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="rope-full-recompute-") as temporary:
        cache = ContentAddressedCache(
            temporary,
            max_entries=max(minimum_cache_entries, 16),
        )
        engine = ArbiterEngine(
            cache,
            mapping_registry=registry,
            limits=limits,
        )
        report = engine.run(cases, snapshot, parallelism=1)
        if report["reuse_set"]:
            raise AssertionError("a fresh cache unexpectedly reported reused nodes")
        return report["node_hashes"]


def run_snapshot_workflow(
    *,
    cases: list[CaseSpec],
    cache: ContentAddressedCache,
    stage_store: SnapshotStore,
    limits: EngineLimits,
    parallelism: int,
    mapping_registry: MappingRegistry | None = None,
) -> dict[str, Any]:
    from .diagnostics import run_self_check
    from .models import SnapshotStage

    registry = mapping_registry or build_builtin_mapping_registry()
    stage = SnapshotStage(store=stage_store)
    existing = stage.active_snapshot()
    existing_version = existing.version if existing is not None else 0
    start_version = existing_version + 1
    steps = build_snapshot_steps(cases, start_version)
    engine = ArbiterEngine(cache, mapping_registry=registry, limits=limits)

    transition_reports: list[dict[str, Any]] = []
    previous_hashes: dict[str, str] = {}
    previous_version: int | None = None
    for step in steps:
        receipt = stage.try_commit(step.snapshot)
        run = engine.run(cases, stage.snapshot(), parallelism=parallelism)
        fresh_hashes = _fresh_recompute_hashes(
            cases,
            step.snapshot,
            limits,
            parallelism,
            registry,
            minimum_cache_entries=max(cache.max_entries, 64),
        )
        actual = set(run["recompute_set"])
        reused = set(run["reuse_set"])
        directional_applicable = previous_version is not None
        if directional_applicable:
            invalidated = expected_recomputed_nodes(cases, step.changed_parameters)
            unexpected = sorted(actual - invalidated)
            affected_but_reused = sorted(reused & invalidated)
        else:
            # The initial load has no previous snapshot. On a cold cache every
            # node is naturally a miss; on a warm cache every node may be a hit.
            invalidated = set(actual)
            unexpected = []
            affected_but_reused = []
        hash_mismatches = sorted(
            node_id
            for node_id, artifact_hash in fresh_hashes.items()
            if run["node_hashes"].get(node_id) != artifact_hash
        )
        unchanged_reused = reused - invalidated
        reused_hash_errors = sorted(
            node_id
            for node_id in unchanged_reused
            if node_id in previous_hashes
            and previous_hashes[node_id] != run["node_hashes"][node_id]
        )
        self_check = run_self_check(run)
        transition_reports.append(
            {
                "name": step.name,
                "description": step.description,
                "snapshot_version": step.snapshot.version,
                "snapshot_checksum": step.snapshot.content_checksum,
                "previous_snapshot_version": previous_version,
                "changed_parameters": {
                    case_id: dict(changes)
                    for case_id, changes in step.changed_parameters.items()
                },
                "commit": receipt.to_dict(),
                "expected_invalidated_set": sorted(invalidated),
                "actual_recomputed_set": sorted(actual),
                "actual_reuse_set": sorted(reused),
                "affected_but_reused_from_cache": affected_but_reused,
                "recompute_cases": run["recompute_cases"],
                "reuse_cases": run["reuse_cases"],
                "unexpected_recomputed": unexpected,
                "directional_invalidation_applicable": directional_applicable,
                "directional_invalidation_ok": not directional_applicable
                or not unexpected,
                "reused_artifact_hash_stable": not reused_hash_errors,
                "reused_hash_errors": reused_hash_errors,
                "full_recompute_hash_equivalent": not hash_mismatches,
                "full_recompute_hash_mismatches": hash_mismatches,
                "business_codes": run["business_codes"],
                "degradations": run["execution"]["degradations"],
                "self_check": self_check,
                "run": run,
            }
        )
        previous_hashes = dict(run["node_hashes"])
        previous_version = step.snapshot.version

    final_run = transition_reports[-1]["run"]
    diagnostic_sample: dict[str, Any] = {}
    if final_run["cases"]:
        first_case = final_run["cases"][0]
        for variant in first_case.get("variants", {}):
            for node in NODE_NAMES:
                diagnostic_sample[f"{first_case['case_id']}::{variant}::{node}"] = (
                    cache.diagnose(
                        case_id=first_case["case_id"],
                        variant=variant,
                        node=node,
                    )
                )
    all_transitions_ok = all(
        (not item["directional_invalidation_applicable"] or item["directional_invalidation_ok"])
        and item["reused_artifact_hash_stable"]
        and item["full_recompute_hash_equivalent"]
        and item["self_check"]["ok"]
        for item in transition_reports
    )
    report = {
        "schema": "rope-arbitration-report-v1",
        "sample_case_file": "data/sample_cases.jsonl",
        "pairing_convention": (
            "half_split:dim[i] pairs with dim[i+head_dim/2]; Q and K share the same angle"
        ),
        "snapshot_transitions": [
            {
                key: value
                for key, value in item.items()
                if key != "run"
            }
            for item in transition_reports
        ],
        "runs": {
            str(item["snapshot_version"]): item["run"]
            for item in transition_reports
        },
        "cache_diagnostics": diagnostic_sample,
        "self_check": {
            "ok": all_transitions_ok,
            "transition_count": len(transition_reports),
            "failed_transitions": [
                item["name"]
                for item in transition_reports
                if not (
                    (
                        not item["directional_invalidation_applicable"]
                        or item["directional_invalidation_ok"]
                    )
                    and item["reused_artifact_hash_stable"]
                    and item["full_recompute_hash_equivalent"]
                    and item["self_check"]["ok"]
                )
            ],
        },
        "business_codes": sorted(
            {
                code
                for item in transition_reports
                for code in item["business_codes"]
            }
        ),
        "degradation_categories": sorted(
            {
                degradation["category"]
                for item in transition_reports
                for degradation in item["degradations"]
            }
        ),
        "degradations": [
            {
                "snapshot_version": item["snapshot_version"],
                **degradation,
            }
            for item in transition_reports
            for degradation in item["degradations"]
        ],
        "final_snapshot": {
            "version": transition_reports[-1]["snapshot_version"],
            "checksum": transition_reports[-1]["snapshot_checksum"],
            "cache_entry_count": cache.entry_count,
        },
    }
    return report
