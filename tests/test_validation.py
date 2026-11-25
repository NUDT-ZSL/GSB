from __future__ import annotations

import json

import numpy as np
import pytest

from moe_router.errors import (
    E_DUPLICATE_CASE_ID,
    E_EXPERT_COUNT_MISMATCH,
    E_GATE_DIMENSION_MISMATCH,
    E_INVALID_BLOCK_SIZE,
    E_REVISION_REGRESSION,
    E_TOKEN_DIMENSION_MISMATCH,
    MoeError,
)
from moe_router.models import ResourceLimits
from moe_router.validation import (
    enforce_memory_limit,
    load_cases,
    validate_expert_bank,
    validate_revision,
    validate_tokens_and_gate,
)
from moe_router.weights import build_expert_bank, build_weight_bundle, generate_cases


def test_duplicate_case_id_is_rejected(tmp_path) -> None:
    case = generate_cases(count=1)[0]
    path = tmp_path / "cases.jsonl"
    row = json.dumps(case.to_dict())
    path.write_text(row + "\n" + row + "\n", encoding="utf-8")
    with pytest.raises(MoeError) as exc_info:
        load_cases(path)
    error = exc_info.value
    assert error.code == E_DUPLICATE_CASE_ID
    assert error.case_id == case.case_id
    assert error.field == "case_id"


def test_token_and_gate_dimensions_are_checked() -> None:
    case = generate_cases(count=1)[0]
    bundle = build_weight_bundle(case)
    with pytest.raises(MoeError) as exc_info:
        validate_tokens_and_gate(case, bundle.tokens[:-1], bundle.gate_weights)
    assert exc_info.value.code == E_TOKEN_DIMENSION_MISMATCH
    assert exc_info.value.field == "token_len"

    with pytest.raises(MoeError) as exc_info:
        validate_tokens_and_gate(case, bundle.tokens, bundle.gate_weights[:, :-1])
    assert exc_info.value.code == E_GATE_DIMENSION_MISMATCH
    assert exc_info.value.field == "gate_weights"


def test_expert_weight_count_is_checked() -> None:
    case = generate_cases(count=1)[0]
    bank = build_expert_bank(case)
    truncated = bank.__class__(
        seed=bank.seed,
        num_experts=bank.num_experts,
        hidden_dim=bank.hidden_dim,
        revisions=bank.revisions[:-1],
        weights=bank.weights[:-1],
        digest=bank.digest,
    )
    with pytest.raises(MoeError) as exc_info:
        validate_expert_bank(case, truncated)
    assert exc_info.value.code == E_EXPERT_COUNT_MISMATCH
    assert exc_info.value.field == "num_experts"


def test_non_positive_block_size_is_rejected() -> None:
    case = generate_cases(count=1)[0].with_updates(block_size=0)
    with pytest.raises(MoeError) as exc_info:
        build_weight_bundle(case)
    assert exc_info.value.code == E_INVALID_BLOCK_SIZE
    assert exc_info.value.field == "block_size"


def test_revision_rollback_is_rejected() -> None:
    with pytest.raises(MoeError) as exc_info:
        validate_revision(5, 4)
    assert exc_info.value.code == E_REVISION_REGRESSION
    assert exc_info.value.field == "router_revision"


def test_memory_limit_is_rejected() -> None:
    case = generate_cases(count=1)[0]
    with pytest.raises(MoeError) as exc_info:
        enforce_memory_limit(
            0,
            case.block_size,
            case,
            ResourceLimits(max_case_resident_bytes=1),
        )
    assert exc_info.value.code == "E_RESOURCE_LIMIT"
    assert exc_info.value.field == "max_case_resident_bytes"

