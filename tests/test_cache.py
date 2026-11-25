from __future__ import annotations

import hashlib

import pytest

from moe_router.cache import ContentAddressedCache
from moe_router.errors import E_CACHE_CORRUPT, E_STALE_RESULT, MoeError
from moe_router.routing import RoutingSession, build_dependencies
from moe_router.weights import build_expert_bank, build_weight_bundle, generate_cases


def test_checked_cache_round_trip_and_stale_rejection(tmp_path) -> None:
    case = generate_cases(count=1)[0]
    bundle = build_weight_bundle(case)
    cache = ContentAddressedCache(tmp_path / "cache")
    session = RoutingSession(bundle, cache)
    session.compute_next_block()
    dependencies = build_dependencies(
        case,
        bundle,
        block_index=0,
        token_start=0,
        token_end=case.block_size,
    )
    loaded = cache.load_checked_artifact(
        case_id=case.case_id,
        block_index=0,
        kind="block_output",
        expected_dependencies=dependencies,
    )
    assert loaded is not None
    assert loaded["artifact"]["outputs"].shape == (case.block_size, case.hidden_dim)

    altered = dict(dependencies)
    altered["top_k"] = case.top_k + 1
    with pytest.raises(MoeError) as exc_info:
        cache.load_checked_artifact(
            case_id=case.case_id,
            block_index=0,
            kind="block_output",
            expected_dependencies=altered,
        )
    assert exc_info.value.code == E_STALE_RESULT
    assert exc_info.value.case_id == case.case_id
    assert exc_info.value.block_index == 0


def test_each_artifact_is_content_addressed(tmp_path) -> None:
    case = generate_cases(count=1)[0]
    bundle = build_weight_bundle(case)
    cache = ContentAddressedCache(tmp_path / "cache")
    session = RoutingSession(bundle, cache).route_all()
    objects = list((cache.root / "objects").rglob("*.json"))
    assert objects
    for path in objects:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == path.stem
    assert cache.verify_all_objects()["ok"] is True
    for block in session.blocks.values():
        assert set(block.cache_records) == set(ContentAddressedCache.KINDS)


def test_tampered_object_is_rejected(tmp_path) -> None:
    case = generate_cases(count=1)[0]
    bundle = build_weight_bundle(case)
    cache = ContentAddressedCache(tmp_path / "cache")
    session = RoutingSession(bundle, cache).route_all()
    record = session.blocks[0].cache_records["block_output"]
    object_path = cache._object_path(record["content_hash"])
    object_path.write_bytes(object_path.read_bytes() + b" ")
    with pytest.raises(MoeError) as exc_info:
        cache.load_by_key(record["key"])
    assert exc_info.value.code == E_CACHE_CORRUPT


def test_expert_revision_changes_expected_key(tmp_path) -> None:
    case = generate_cases(count=1)[0]
    bundle = build_weight_bundle(case)
    cache = ContentAddressedCache(tmp_path / "cache")
    RoutingSession(bundle, cache).route_all()
    old_dependencies = build_dependencies(
        case, bundle, block_index=0, token_start=0, token_end=case.block_size
    )
    new_case = case.with_updates(router_revision=case.router_revision + 1)
    revisions = {
        expert_id: (
            case.router_revision + 1 if expert_id == 0 else revision
        )
        for expert_id, revision in enumerate(bundle.expert_bank.revisions)
    }
    new_bank = build_expert_bank(new_case, revisions)
    new_bundle = build_weight_bundle(new_case, revisions)
    assert new_bundle.expert_bank.digest == new_bank.digest
    new_dependencies = build_dependencies(
        new_case, new_bundle, block_index=0, token_start=0, token_end=case.block_size
    )
    with pytest.raises(MoeError) as exc_info:
        cache.load_checked_artifact(
            case_id=case.case_id,
            block_index=0,
            kind="block_output",
            expected_dependencies=new_dependencies,
        )
    assert exc_info.value.code == E_STALE_RESULT
    assert old_dependencies != new_dependencies
