from __future__ import annotations

import json
from pathlib import Path

from wadiag.engine import DiagnosticEngine
from wadiag.segments import estimate_lookup, parse_segment_payload
from wadiag.serialization import canonical_json_text

from .conftest import cli_json, load_sample, run_cli


def test_segment_and_manifest_generation_is_byte_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    load_sample(first)
    load_sample(second)
    first_flush = run_cli(first, "flush")
    second_flush = run_cli(second, "flush")
    assert first_flush.returncode == 0
    assert second_flush.returncode == 0
    first_payload = cli_json(first_flush)
    second_payload = cli_json(second_flush)
    assert first_payload["segments_written"] == 14
    assert first_payload["segment_uids"] == second_payload["segment_uids"]
    assert first_payload["manifest_digest"] == second_payload["manifest_digest"]

    first_files = {
        path.relative_to(first): path.read_bytes()
        for path in (first / "segments").rglob("*")
        if path.is_file()
    }
    assert all(len(payload) <= 800 for payload in first_files.values())
    second_files = {
        path.relative_to(second): path.read_bytes()
        for path in (second / "segments").rglob("*")
        if path.is_file()
    }
    assert first_files == second_files
    first_manifests = {
        path.relative_to(first): path.read_bytes()
        for path in (first / "manifests").rglob("*")
        if path.is_file()
    }
    second_manifests = {
        path.relative_to(second): path.read_bytes()
        for path in (second / "manifests").rglob("*")
        if path.is_file()
    }
    assert first_manifests == second_manifests

    repeated = run_cli(first, "flush")
    assert repeated.returncode == 0
    repeated_payload = cli_json(repeated)
    assert repeated_payload["segments_written"] == 0
    assert repeated_payload["idempotent_replay"] is True


def test_sparse_index_bloom_and_lookup_cost_are_reproducible(
    loaded_workspace: Path,
) -> None:
    assert run_cli(loaded_workspace, "flush").returncode == 0
    engine = DiagnosticEngine(loaded_workspace)
    run_id = engine.store.active_run_id()
    levels, storage = engine.store.load_level_segments(run_id)
    segments = levels[0]
    for segment in segments:
        assert segment.meta.index_entries == segment.meta.block_count
        first_keys = [entry.first_key for entry in segment.meta.index]
        assert first_keys == sorted(first_keys)
        assert segment.meta.bloom_bits >= 64
        assert segment.meta.bloom_fp_upper.startswith("0.")
    first = estimate_lookup("user:0007", segments, sample_ratio="0.5")
    second = estimate_lookup("user:0007", segments, sample_ratio="0.5")
    assert canonical_json_text(first) == canonical_json_text(second)
    assert first["key"] == "user:0007"
    assert first["estimated_segment_reads"] >= 1
    assert first["estimated_block_reads"] == first["estimated_segment_reads"]
    for observation in first["observations"]:
        for field in (
            "segment_no",
            "min_key",
            "max_key",
            "index_entries",
            "bloom_bits",
            "bloom_hashes",
            "decision",
        ):
            assert field in observation

    path = engine.store.segment_path(run_id, segments[0].meta.filename)
    parsed = parse_segment_payload(
        path.read_bytes(),
        run_id=run_id,
        filename=segments[0].meta.filename,
    )
    assert parsed.meta.to_manifest() == segments[0].meta.to_manifest()


def test_dual_path_ledger_and_precision_flags(tmp_path: Path) -> None:
    workspace = tmp_path / "precision"
    load_sample(workspace)
    assert run_cli(workspace, "flush").returncode == 0
    first = run_cli(workspace, "diagnose", "--query-key", "user:0007")
    assert first.returncode == 0
    payload = cli_json(first)
    assert payload["ok"] is True
    layers = payload["layers"]
    assert len(layers) == 4
    for layer in layers:
        assert "fixed_path" in layer["wa_contribution"]
        assert "truth_path" in layer["wa_contribution"]
        assert "absolute_deviation" in layer["wa_contribution"]
        assert "relative_deviation" in layer["wa_contribution"]
        assert layer["error_source_chain"]
        assert all(step["layer"] == layer["level"] for step in layer["error_source_chain"])
        assert layer["error_source_chain"][0]["scale"] == 6
        assert layer["error_source_chain"][0]["rounding"] == "ROUND_HALF_UP"
        assert "raw_value" in layer["error_source_chain"][0]
    whole = payload["whole_store_write_amplification"]
    assert whole["accumulation_order"] == [0, 1, 2, 3]
    assert len(whole["cumulative_steps"]) == 4
    assert payload["precision"]["precision_sensitive"] is True

    second = run_cli(workspace, "diagnose", "--query-key", "user:0007")
    assert second.returncode == 0
    assert first.stdout == second.stdout

    for _ in range(6):
        compact = run_cli(workspace, "compact")
        assert compact.returncode == 0, compact.stdout + compact.stderr
    final_run = run_cli(workspace, "diagnose", "--query-key", "user:0002")
    assert final_run.returncode == 0
    final_payload = cli_json(final_run)
    assert final_payload["precision"]["precision_sensitive"] is True
    assert final_payload["precision"]["precision_sensitive_levels"]
    assert final_payload["cross_level_key_repetition"]["distinct_keys"] > 0
    assert final_payload["whole_store_write_amplification"]["truth_path"]
    verify = run_cli(workspace, "verify")
    assert verify.returncode == 0
    assert cli_json(verify)["ok"] is True
