from __future__ import annotations

import json
from pathlib import Path

import pytest

from cm_pipeline.cache import CacheStore
from cm_pipeline.canonical import canonical_json_bytes
from cm_pipeline.config import load_config
from cm_pipeline.engine import Artifact, PipelineEngine
from cm_pipeline.errors import PipelineError
from cm_pipeline.replay import load_results_bytes, replay_hit, replay_snapshot
from cm_pipeline.samples import load_bundle
from cm_pipeline.state import StateStore
from cm_pipeline.verify import verify_snapshot

from conftest import make_engine, write_config_variant


def test_invalid_inputs_return_specific_codes(fixtures, tmp_path):
    expected = {
        "duplicate": "E_DUP_PAIR",
        "seed_shape": "E_SEED_SHAPE_MISMATCH",
        "text_empty": "E_TEXT_EMPTY",
        "mask_invalid": "E_MASK_INVALID",
        "embed_dim": "E_CONFIG_INVALID",
    }
    for name, code in expected.items():
        with pytest.raises(PipelineError) as caught:
            load_bundle(
                fixtures["invalid"][name],
                fixtures["pipeline"],
                fixtures["vocab"],
                fixtures["codebook_v1"],
            )
        assert caught.value.code == code
        assert caught.value.stage == "input_validation"
        assert caught.value.field is not None
        assert caught.value.pair_id is not None


def test_config_embed_dim_and_graph_cycle_are_rejected(fixtures, tmp_path):
    with Path(fixtures["pipeline"]).open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    config["image"]["embed_dim"] = 0
    embed_path = tmp_path / "bad_embed.json"
    embed_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(PipelineError) as caught:
        load_bundle(
            fixtures["samples"],
            str(embed_path),
            fixtures["vocab"],
            fixtures["codebook_v1"],
        )
    assert caught.value.code == "E_CONFIG_INVALID"
    assert caught.value.field == "image.embed_dim"

    cycle_path = write_config_variant(
        fixtures,
        tmp_path,
        name="cycle",
        graph_edges=[
            ["image_encoder", "align"],
            ["text_encoder", "align"],
            ["align", "index_build"],
            ["align", "retrieve"],
            ["index_build", "retrieve"],
            ["retrieve", "image_encoder"],
        ],
    )
    with pytest.raises(PipelineError) as caught:
        load_config(cycle_path)
    assert caught.value.code == "E_CONFIG_INVALID"
    assert caught.value.stage == "graph"


def test_full_build_verify_replay_and_cache_key_inspection(fixtures, tmp_path):
    engine = make_engine(fixtures, tmp_path / "run")
    report = engine.build_full()
    assert report["snapshot_version"] == 1
    assert len(report["recomputed_nodes"]) == 51
    assert report["reused_nodes"] == []
    verification = verify_snapshot(
        cache_root=str(tmp_path / "run" / "cache"),
        state_root=str(tmp_path / "run" / "state"),
    )
    assert verification["ok"] is True
    assert verification["counts"]["hits"] == 33
    replay = replay_snapshot(
        cache_root=str(tmp_path / "run" / "cache"),
        state_root=str(tmp_path / "run" / "state"),
    )
    assert replay["ok"] is True
    assert replay["replayed_hits"] == 33

    snapshot = StateStore(tmp_path / "run" / "state").load_snapshot(1)
    image_key = snapshot["nodes"]["image_encoder:PAIR-0001"]["key"]
    inspected = CacheStore(tmp_path / "run" / "cache").inspect(image_key)
    assert inspected["recomputed_key"] == image_key
    assert inspected["key_components"]["node_type"] == "image_encoder"
    assert inspected["key_components"]["pair_id"] == "PAIR-0001"
    assert inspected["product_hash"] == snapshot["nodes"]["image_encoder:PAIR-0001"][
        "product_hash"
    ]


def test_text_and_image_incremental_only_recomputes_suffix_and_matches_full(
    fixtures, tmp_path
):
    initial = make_engine(fixtures, tmp_path / "inc")
    initial.build_full()
    engine = make_engine(
        fixtures,
        tmp_path / "inc",
        data=fixtures["samples_updated"],
    )
    report = engine.build_incremental(expected_version=1)
    counts: dict[str, int] = {}
    for node in report["recomputed_nodes"]:
        counts[node["node_type"]] = counts.get(node["node_type"], 0) + 1
    assert counts == {
        "image_encoder": 1,
        "text_encoder": 1,
        "align": 2,
        "index_build": 1,
        "retrieve": 12,
    }
    assert report["recomputed_index_blocks"] == [2]
    assert report["reused_index_blocks"] == [0, 1]
    assert len(report["reused_nodes"]) == 34
    assert verify_snapshot(
        cache_root=str(tmp_path / "inc" / "cache"),
        state_root=str(tmp_path / "inc" / "state"),
    )["ok"]

    fresh = make_engine(
        fixtures,
        tmp_path / "fresh",
        data=fixtures["samples_updated"],
    )
    fresh.build_full()
    incremental_bytes = load_results_bytes(
        cache_root=str(tmp_path / "inc" / "cache"),
        state_root=str(tmp_path / "inc" / "state"),
        snapshot_version=2,
    )
    fresh_bytes = load_results_bytes(
        cache_root=str(tmp_path / "fresh" / "cache"),
        state_root=str(tmp_path / "fresh" / "state"),
        snapshot_version=1,
    )
    assert incremental_bytes == fresh_bytes


def test_unaffected_queries_are_reused_after_single_image_replacement(
    fixtures, tmp_path
):
    samples = [
        json.loads(line)
        for line in Path(fixtures["samples"]).read_text(encoding="utf-8").splitlines()
    ]
    for sample in samples:
        if sample["pair_id"] == "PAIR-0005":
            sample["image_seed"] = 700005
    image_only_data = tmp_path / "samples_image_only.jsonl"
    with image_only_data.open("wb") as handle:
        for sample in samples:
            handle.write(canonical_json_bytes(sample) + b"\n")

    initial = make_engine(fixtures, tmp_path / "image_only")
    initial.build_full()
    report = make_engine(
        fixtures,
        tmp_path / "image_only",
        data=str(image_only_data),
    ).build_incremental(expected_version=1)
    counts: dict[str, int] = {}
    for node in report["recomputed_nodes"]:
        counts[node["node_type"]] = counts.get(node["node_type"], 0) + 1
    assert counts == {
        "image_encoder": 1,
        "align": 1,
        "retrieve": 1,
    }
    assert report["reused_index_blocks"] == [0, 1, 2]
    reused_retrieval = [
        node
        for node in report["reused_nodes"]
        if node["node_type"] == "retrieve"
    ]
    assert len(reused_retrieval) == 11
    assert verify_snapshot(
        cache_root=str(tmp_path / "image_only" / "cache"),
        state_root=str(tmp_path / "image_only" / "state"),
    )["ok"]
    fresh = make_engine(
        fixtures,
        tmp_path / "image_only_fresh",
        data=str(image_only_data),
    )
    fresh.build_full()
    assert load_results_bytes(
        cache_root=str(tmp_path / "image_only" / "cache"),
        state_root=str(tmp_path / "image_only" / "state"),
        snapshot_version=2,
    ) == load_results_bytes(
        cache_root=str(tmp_path / "image_only_fresh" / "cache"),
        state_root=str(tmp_path / "image_only_fresh" / "state"),
        snapshot_version=1,
    )


def test_alignment_change_reuses_both_encoders(fixtures, tmp_path):
    initial = make_engine(fixtures, tmp_path / "align")
    initial.build_full()
    engine = make_engine(
        fixtures,
        tmp_path / "align",
        config=fixtures["pipeline_align_v2"],
    )
    report = engine.build_incremental(expected_version=1)
    node_types = {node["node_type"] for node in report["recomputed_nodes"]}
    assert "image_encoder" not in node_types
    assert "text_encoder" not in node_types
    assert node_types == {"align", "index_build", "retrieve"}
    assert len(report["reused_nodes"]) == 24
    assert verify_snapshot(
        cache_root=str(tmp_path / "align" / "cache"),
        state_root=str(tmp_path / "align" / "state"),
    )["ok"]


def test_codebook_v2_replaces_snapshot_and_reuses_untouched_blocks(fixtures, tmp_path):
    initial = make_engine(fixtures, tmp_path / "cb")
    initial.build_full()
    engine = make_engine(
        fixtures,
        tmp_path / "cb",
        codebook=fixtures["codebook_v2"],
    )
    report = engine.build_incremental(expected_version=1)
    counts: dict[str, int] = {}
    for node in report["recomputed_nodes"]:
        counts[node["node_type"]] = counts.get(node["node_type"], 0) + 1
    assert counts == {"index_build": 1, "retrieve": 12}
    assert report["recomputed_index_blocks"] == [1]
    assert report["reused_index_blocks"] == [0, 2]
    snapshot = StateStore(tmp_path / "cb" / "state").load_snapshot(2)
    assert snapshot["codebook"]["version"] == 2
    versions = {
        block["block_id"]: block["version"]
        for block in snapshot["codebook"]["blocks"]
    }
    assert versions == {0: 1, 1: 2, 2: 1}
    assert verify_snapshot(
        cache_root=str(tmp_path / "cb" / "cache"),
        state_root=str(tmp_path / "cb" / "state"),
    )["ok"]

    fresh = make_engine(
        fixtures,
        tmp_path / "cb_fresh",
        codebook=fixtures["codebook_v2"],
    )
    fresh.build_full()
    assert load_results_bytes(
        cache_root=str(tmp_path / "cb" / "cache"),
        state_root=str(tmp_path / "cb" / "state"),
        snapshot_version=2,
    ) == load_results_bytes(
        cache_root=str(tmp_path / "cb_fresh" / "cache"),
        state_root=str(tmp_path / "cb_fresh" / "state"),
        snapshot_version=1,
    )


def test_stale_lineage_rejected_but_historical_replay_allowed(fixtures, tmp_path):
    initial = make_engine(fixtures, tmp_path / "lineage")
    initial.build_full()
    cache_root = str(tmp_path / "lineage" / "cache")
    state_root = str(tmp_path / "lineage" / "state")
    from cm_pipeline.verify import _load_manifest

    snapshot = StateStore(state_root).load_snapshot(1)
    result_doc, _ = _load_manifest(CacheStore(cache_root), snapshot)
    old_hit = result_doc["queries"][0]["hits"][0]

    engine = make_engine(
        fixtures,
        tmp_path / "lineage",
        data=fixtures["samples_updated"],
    )
    engine.build_incremental(expected_version=1)
    with pytest.raises(PipelineError) as caught:
        replay_hit(
            old_hit,
            cache_root=cache_root,
            state_root=state_root,
            require_current=True,
        )
    assert caught.value.code == "E_LINEAGE_BROKEN"
    historical = replay_hit(
        old_hit,
        cache_root=cache_root,
        state_root=state_root,
        require_current=False,
    )
    assert historical["matches"] is True
    assert verify_snapshot(
        cache_root=cache_root,
        state_root=state_root,
        snapshot_version=1,
        require_current=False,
    )["ok"]


def test_snapshot_and_codebook_rollback_are_rejected(fixtures, tmp_path):
    initial = make_engine(fixtures, tmp_path / "rollback")
    initial.build_full()
    engine = make_engine(
        fixtures,
        tmp_path / "rollback",
        codebook=fixtures["codebook_v2"],
    )
    engine.build_incremental(expected_version=1)
    with pytest.raises(PipelineError) as caught:
        make_engine(
            fixtures,
            tmp_path / "rollback",
            codebook=fixtures["codebook_v1"],
        ).build_incremental(expected_version=2)
    assert caught.value.code == "E_SNAPSHOT_ROLLBACK"
    with pytest.raises(PipelineError) as caught:
        make_engine(
            fixtures,
            tmp_path / "rollback",
            codebook=fixtures["codebook_v2"],
        ).build_incremental(expected_version=1)
    assert caught.value.code == "E_SNAPSHOT_ROLLBACK"
    with pytest.raises(PipelineError) as caught:
        make_engine(
            fixtures,
            tmp_path / "rollback",
            codebook=fixtures["codebook_v2"],
        ).build_full()
    assert caught.value.code == "E_SNAPSHOT_ROLLBACK"


def test_concurrent_commit_returns_busy_immediately(fixtures, tmp_path):
    initial = make_engine(fixtures, tmp_path / "busy")
    initial.build_full()
    state = StateStore(tmp_path / "busy" / "state")
    with state.commit_lock():
        with pytest.raises(PipelineError) as caught:
            make_engine(
                fixtures,
                tmp_path / "busy",
                data=fixtures["samples_updated"],
            ).build_incremental(expected_version=1)
    assert caught.value.code == "E_STAGE_BUSY"
    assert state.read_state()["current_version"] == 1


def test_resource_limits_error_and_degrade(fixtures, tmp_path):
    initial = make_engine(fixtures, tmp_path / "res")
    initial.build_full()
    strict_config = write_config_variant(
        fixtures,
        tmp_path,
        name="strict",
        resources={"max_recompute_pairs": 1, "on_limit": "error"},
    )
    with pytest.raises(PipelineError) as caught:
        make_engine(
            fixtures,
            tmp_path / "res",
            data=fixtures["samples_updated"],
            config=strict_config,
        ).build_incremental(expected_version=1)
    assert caught.value.code == "E_RESOURCE_LIMIT"
    assert caught.value.details["category"] == "pair_recompute_pairs"

    degrade_config = write_config_variant(
        fixtures,
        tmp_path,
        name="degrade",
        resources={"max_recompute_pairs": 1, "on_limit": "degrade"},
    )
    report = make_engine(
        fixtures,
        tmp_path / "res",
        data=fixtures["samples_updated"],
        config=degrade_config,
    ).build_incremental(expected_version=1)
    categories = {item["category"] for item in report["degraded"]}
    assert "pair_recompute_pairs" in categories
    assert verify_snapshot(
        cache_root=str(tmp_path / "res" / "cache"),
        state_root=str(tmp_path / "res" / "state"),
    )["ok"]

    memory_config = write_config_variant(
        fixtures,
        tmp_path,
        name="memory",
        resources={"max_codebook_bytes": 1, "on_limit": "error"},
    )
    with pytest.raises(PipelineError) as caught:
        make_engine(
            fixtures,
            tmp_path / "memory",
            config=memory_config,
        ).build_full()
    assert caught.value.code == "E_RESOURCE_LIMIT"
    assert caught.value.details["category"] == "codebook_memory"


def test_tampered_cache_fails_verification(fixtures, tmp_path):
    engine = make_engine(fixtures, tmp_path / "tamper")
    engine.build_full()
    cache_root = tmp_path / "tamper" / "cache"
    snapshot = StateStore(tmp_path / "tamper" / "state").load_snapshot(1)
    key = snapshot["nodes"]["text_encoder:PAIR-0001"]["key"]
    payload = cache_root / "objects" / key[:2] / key / "payload.bin"
    payload.write_bytes(payload.read_bytes() + b"tampered")
    with pytest.raises(PipelineError) as caught:
        verify_snapshot(
            cache_root=str(cache_root),
            state_root=str(tmp_path / "tamper" / "state"),
        )
    assert caught.value.code == "E_VERIFY_FAIL"
    assert caught.value.details["category"] in {
        "cache_key_unreadable",
        "node_product_hash",
    }
    assert caught.value.details["location"].startswith("text_encoder:PAIR-0001")


def test_serial_and_process_pool_results_are_byte_identical(fixtures, tmp_path):
    serial = make_engine(fixtures, tmp_path / "serial", workers=1)
    serial.build_full()
    pooled = make_engine(fixtures, tmp_path / "pooled", workers=2)
    pooled.build_full()
    assert load_results_bytes(
        cache_root=str(tmp_path / "serial" / "cache"),
        state_root=str(tmp_path / "serial" / "state"),
    ) == load_results_bytes(
        cache_root=str(tmp_path / "pooled" / "cache"),
        state_root=str(tmp_path / "pooled" / "state"),
    )


def test_index_code_residual_mismatch_reports_block_and_version(fixtures, tmp_path):
    engine = make_engine(fixtures, tmp_path / "index_guard")
    engine.build_full()
    snapshot = StateStore(tmp_path / "index_guard" / "state").load_snapshot(1)
    align_by_pair: dict[str, Artifact] = {}
    for sample in snapshot["samples"]:
        pair_id = sample["pair_id"]
        node = snapshot["nodes"][f"align:{pair_id}"]
        arrays, meta, cache_meta = engine.cache.get(node["key"])
        align_by_pair[pair_id] = Artifact(
            instance=f"align:{pair_id}",
            node_type="align",
            pair_id=pair_id,
            key=node["key"],
            product_hash=cache_meta["product_hash"],
            arrays=arrays,
            meta=meta,
        )
    block_id = 0
    entry = snapshot["index_blocks"][str(block_id)]
    arrays, meta, cache_meta = engine.cache.get(entry["key"])
    arrays = {name: value.copy() for name, value in arrays.items()}
    arrays["codes"][0] = (int(arrays["codes"][0]) + 1) % arrays["centroids"].shape[0]
    corrupted = Artifact(
        instance=f"index_build:block-{block_id}",
        node_type="index_build",
        pair_id=None,
        key=entry["key"],
        product_hash=cache_meta["product_hash"],
        arrays=arrays,
        meta=meta,
    )
    block = next(
        block
        for block in engine.bundle.codebook["blocks"]
        if block["block_id"] == block_id
    )
    with pytest.raises(PipelineError) as caught:
        engine._validate_index_artifact(
            corrupted,
            pair_ids=list(meta["pair_ids"]),
            align_by_pair=align_by_pair,
            block=block,
        )
    assert caught.value.code == "E_INCONSISTENT"
    assert caught.value.details["block_id"] == block_id
    assert "codebook_version" in caught.value.details


def test_cache_entry_limit_returns_resource_error(fixtures, tmp_path):
    config_path = write_config_variant(
        fixtures,
        tmp_path,
        name="cache_limit",
        resources={"max_cache_entries": 1, "on_limit": "error"},
    )
    with pytest.raises(PipelineError) as caught:
        make_engine(
            fixtures,
            tmp_path / "cache_limit",
            config=config_path,
        ).build_full()
    assert caught.value.code == "E_RESOURCE_LIMIT"
    assert caught.value.details["category"] == "cache_entries"

