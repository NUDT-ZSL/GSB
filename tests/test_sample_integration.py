import json
import os

import pytest

from sensitrace.checkpoint import CheckpointStore
from sensitrace.samplegen import generate_samples


@pytest.fixture()
def sample_dir(tmp_path):
    directory = tmp_path / "samples"
    generate_samples(str(directory))
    return directory


def _load_documents(sample_dir):
    documents = []
    for name in sorted(os.listdir(sample_dir)):
        if name == "markers.json" or not name.endswith(".json"):
            continue
        with open(os.path.join(sample_dir, name), encoding="utf-8") as handle:
            documents.append(json.load(handle))
    return documents


def test_marker_ground_truth_violations_and_levels(sample_dir):
    markers = json.load(open(os.path.join(sample_dir, "markers.json"), encoding="utf-8"))
    engine, applied, diagnostics, failed = CheckpointStore.build_from_documents(
        _load_documents(sample_dir)
    )

    # The invalid-version document cannot become a snapshot (inline: no path).
    assert failed == ["<inline>"]

    codes = {(d.code, d.version, d.field) for d in diagnostics}
    for violation in markers["expected_violations"]:
        match = [
            entry for entry in codes
            if entry[0] == violation["code"] and entry[1] == violation["version"]
            and entry[2] == violation.get("field", entry[2])
        ]
        assert match, f"missing expected violation {violation}"

    # Effective sensitivity must equal the marker ground truth.
    effective = {
        (node.version, node.key): node.effective
        for node in engine.nodes.values()
    }
    for version, mapping in markers["expected_effective"].items():
        for field, expected in mapping.items():
            assert effective[(version, field)] == expected

    # Added/removed field statistics match markers exactly.
    stats = {stat.version: stat for stat in engine.version_stats}
    for version, added in markers["expected_new_fields"].items():
        assert stats[version].added == added
    for version, removed in markers["expected_removed_fields"].items():
        assert stats[version].removed == removed


def test_single_bad_document_does_not_abort_batch(sample_dir):
    engine, applied, _diagnostics, failed = CheckpointStore.build_from_documents(
        _load_documents(sample_dir)
    )
    assert "1.0.0" in applied and "2.0.0" in applied
    assert len(failed) == 1
    assert len(engine.snapshots) == 5


def test_deterministic_output_is_byte_stable(sample_dir, tmp_path):
    docs = _load_documents(sample_dir)
    first = CheckpointStore.build_from_documents(docs)[0].to_dict()
    second = CheckpointStore.build_from_documents(list(reversed(docs)))[0].to_dict()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
