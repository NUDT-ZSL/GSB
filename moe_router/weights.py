"""Deterministic local sample and weight generation."""

from __future__ import annotations

from typing import Iterable, Mapping

import numpy as np

from .codec import array_digest, canonical_hash
from .models import CaseSpec, ExpertBank, WeightBundle
from .validation import validate_case


_TOKEN_TAG = 11
_GATE_TAG = 23
_EXPERT_TAG = 37


def _rng(seed: int, *tags: int) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(np.random.SeedSequence([seed, *tags])))


def generate_tokens(case: CaseSpec) -> np.ndarray:
    rng = _rng(case.seed, _TOKEN_TAG)
    return np.asarray(
        rng.standard_normal((case.token_len, case.hidden_dim), dtype=np.float64),
        dtype=np.float64,
    )


def generate_gate_weights(case: CaseSpec) -> np.ndarray:
    rng = _rng(case.seed, _GATE_TAG)
    scale = 1.0 / np.sqrt(float(case.hidden_dim))
    return np.asarray(
        rng.standard_normal((case.hidden_dim, case.num_experts), dtype=np.float64) * scale,
        dtype=np.float64,
    )


def generate_expert_weights(
    seed: int, hidden_dim: int, expert_id: int, revision: int
) -> np.ndarray:
    rng = _rng(seed, _EXPERT_TAG, expert_id, revision)
    scale = 1.0 / np.sqrt(float(hidden_dim))
    return np.asarray(
        rng.standard_normal((hidden_dim, hidden_dim), dtype=np.float64) * scale,
        dtype=np.float64,
    )


def build_expert_bank(
    case: CaseSpec, revisions: Mapping[int, int] | None = None
) -> ExpertBank:
    overrides = {int(expert_id): int(rev) for expert_id, rev in (revisions or {}).items()}
    for expert_id, revision in overrides.items():
        if not 0 <= expert_id < case.num_experts:
            raise ValueError(f"expert_id out of range: {expert_id}")
        if revision < 0:
            raise ValueError("expert revision must be non-negative")
    resolved = tuple(
        overrides.get(expert_id, case.router_revision)
        for expert_id in range(case.num_experts)
    )
    weights = tuple(
        generate_expert_weights(case.seed, case.hidden_dim, expert_id, resolved[expert_id])
        for expert_id in range(case.num_experts)
    )
    digest_payload = {
        "seed": case.seed,
        "hidden_dim": case.hidden_dim,
        "num_experts": case.num_experts,
        "experts": [
            {
                "expert_id": expert_id,
                "revision": resolved[expert_id],
                "weight_digest": array_digest(weights[expert_id]),
            }
            for expert_id in range(case.num_experts)
        ],
    }
    return ExpertBank(
        seed=case.seed,
        num_experts=case.num_experts,
        hidden_dim=case.hidden_dim,
        revisions=resolved,
        weights=weights,
        digest=canonical_hash(digest_payload),
    )


def build_weight_bundle(
    case: CaseSpec, revisions: Mapping[int, int] | None = None
) -> WeightBundle:
    validate_case(case)
    tokens = generate_tokens(case)
    gate_weights = generate_gate_weights(case)
    expert_bank = build_expert_bank(case, revisions)
    return WeightBundle(
        case=case,
        tokens=tokens,
        gate_weights=gate_weights,
        expert_bank=expert_bank,
        token_content_hash=array_digest(tokens),
        gate_weights_digest=array_digest(gate_weights),
    )


def generate_cases(seed: int = 20261126, count: int = 4) -> list[CaseSpec]:
    """Build a small, deterministic manifest that exercises overflow and blocks."""

    if count < 1:
        raise ValueError("count must be positive")
    templates = [
        dict(token_len=24, hidden_dim=16, num_experts=4, top_k=2,
             capacity_factor=0.75, block_size=8),
        dict(token_len=31, hidden_dim=20, num_experts=5, top_k=2,
             capacity_factor=0.9, block_size=8),
        dict(token_len=40, hidden_dim=24, num_experts=6, top_k=3,
             capacity_factor=1.0, block_size=10),
        dict(token_len=27, hidden_dim=12, num_experts=4, top_k=1,
             capacity_factor=1.25, block_size=7),
        dict(token_len=33, hidden_dim=28, num_experts=7, top_k=2,
             capacity_factor=0.8, block_size=9),
        dict(token_len=19, hidden_dim=10, num_experts=3, top_k=2,
             capacity_factor=1.1, block_size=6),
    ]
    cases: list[CaseSpec] = []
    for index in range(count):
        template = templates[index % len(templates)]
        cases.append(
            CaseSpec(
                case_id=f"moe-case-{index + 1:02d}",
                router_revision=1,
                dtype="float64",
                seed=seed + index * 101,
                **template,
            )
        )
    return cases


def manifest_rows(cases: Iterable[CaseSpec]) -> list[dict[str, object]]:
    rows = []
    for case in cases:
        bundle = build_weight_bundle(case)
        rows.append(
            {
                "case_id": case.case_id,
                "token_content_hash": bundle.token_content_hash,
                "gate_weights_digest": bundle.gate_weights_digest,
                "expert_bank_digest": bundle.expert_bank.digest,
                "expert_revisions": bundle.expert_bank.revision_map,
            }
        )
    return rows
