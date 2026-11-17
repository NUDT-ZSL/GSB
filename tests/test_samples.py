from __future__ import annotations

import hashlib
import json
from pathlib import Path

from klp_audit.config import AuditConfig
from klp_audit.engine import AuditEngine
from klp_audit.generator import generate_samples


def test_sample_generation_is_byte_deterministic(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = generate_samples(first)
    second_manifest = generate_samples(second)
    assert first_manifest.read_bytes() == second_manifest.read_bytes()
    for sample in sorted((first / "samples").glob("*.klp")):
        other = second / "samples" / sample.name
        assert sample.read_bytes() == other.read_bytes()
        assert hashlib.sha256(sample.read_bytes()).hexdigest() == hashlib.sha256(
            other.read_bytes()
        ).hexdigest()


def test_every_truth_hash_matches_sample_and_business_expectations(
    generated_root, manifest
):
    for _name, entry in manifest["files"].items():
        sample = generated_root / entry["sample"]
        truth = json.loads((generated_root / entry["truth"]).read_text(encoding="utf-8"))
        assert hashlib.sha256(sample.read_bytes()).hexdigest() == entry["sha256"]
        assert truth["sha256"] == entry["sha256"]
        assert truth["expected"] == entry["expected"]


def test_manifest_expectations_match_audit_conclusions(generated_root, manifest):
    for name, entry in manifest["files"].items():
        overrides = entry["expected"].get("config_overrides", {})
        config_values = {
            "concurrency": 2,
            "session_timeout_seconds": None,
            "max_derivations_per_session": 50_000,
            "scale_bytes_threshold": 1_048_576,
            "scale_derived_threshold": 10_000,
        }
        config_values.update(overrides)
        report = AuditEngine(
            AuditConfig(**config_values),
            workspace=generated_root,
        ).audit_file(generated_root / entry["sample"])
        assert (
            report["file"]["conclusion"] == entry["expected"]["file_conclusion"]
        ), name


def test_sync_markers_are_unique_except_explicit_idempotent_duplicate(
    generated_root, manifest
):
    for name, entry in manifest["files"].items():
        truth = json.loads((generated_root / entry["truth"]).read_text(encoding="utf-8"))
        markers = [
            event["sync_marker"]
            for events in truth["sessions"].values()
            for event in events
        ]
        if name == "idempotent":
            assert len(markers) - len(set(markers)) == 1
        else:
            assert len(markers) == len(set(markers))

