from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from certlab.clock import LogicalClock
from certlab.errors import CertLabError, ErrorCode
from certlab.snapshot import SnapshotLoader, merge_snapshots
from tools.generate_samples import BASE_MS

from .conftest import CLOCK, run_cli


def test_merge_prefers_v2_and_preserves_decisions(snapshots, merged_view):
    assert merged_view["generation"] == 3
    by_alias = {item["alias"]: item for item in merged_view["anchors"]}
    assert by_alias["root-alpha"]["source"]["version"] == "v2"
    assert by_alias["root-shared"]["source"]["version"] == "v2"
    assert by_alias["root-legacy"]["tombstone"] == "tombstoned"
    assert by_alias["root-legacy"]["enabled"] is False
    assert by_alias["root-alpha"]["decision"]["reason"] == "higher_snapshot_logical_clock"
    assert len(by_alias["root-alpha"]["decision"]["candidates"]) == 2


def test_v1_relative_time_conversion_is_explicit(sample_dir):
    v1 = SnapshotLoader.load_v1(sample_dir / "trust-v1.json", CLOCK)
    view = merge_snapshots([v1], CLOCK)
    anchor = next(item for item in view["anchors"] if item["alias"] == "root-alpha")
    assert anchor["effective_at_ms"] == BASE_MS - 120_000
    assert anchor["raw_time"] == {
        "kind": "relative_seconds",
        "value": 120,
        "reference": "injected_logical_clock",
    }
    assert anchor["time_conversion"] == {
        "reference_clock_ms": BASE_MS,
        "scale_ms": 1000,
        "operator": "subtract",
        "rounding": "exact",
    }


def test_same_clock_prefers_v2_format(snapshots):
    v1, v2 = snapshots
    v1_only = copy.deepcopy(v1)
    tie_v2 = copy.deepcopy(v2)
    v1_anchor = next(item for item in v1_only.anchors if item.alias == "root-shared")
    tie_v2.anchors = [
        replace(
            v1_anchor,
            source_version="v2",
            source_format="shards+sqlite",
        )
    ]
    tie_v2.logical_clock_ms = v1_anchor.logical_clock_ms
    tie_v2.trust_generation = 0
    view = merge_snapshots([tie_v2, v1_only], CLOCK)
    anchor = next(item for item in view["anchors"] if item["alias"] == "root-shared")
    assert anchor["source"]["version"] == "v2"
    assert anchor["decision"]["reason"].startswith("source_format_priority")


def test_v1_bad_fingerprint_has_business_code(tmp_path, sample_dir):
    payload = json.loads((sample_dir / "trust-v1.json").read_text())
    payload["certificates"][0]["fingerprint_sha256"] = "0" * 64
    path = tmp_path / "bad-v1.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(CertLabError) as caught:
        SnapshotLoader.load_v1(path, CLOCK)
    assert caught.value.code == ErrorCode.HASH_MISMATCH
    assert caught.value.details["alias"] == payload["certificates"][0]["alias"]


def test_invalid_v2_does_not_create_state_database(tmp_path, sample_dir):
    v2 = tmp_path / "trust-v2"
    v2.mkdir()
    for name in ("manifest.json", "shard-000.json", "shard-001.json", "metadata.sqlite"):
        (v2 / name).write_bytes((sample_dir / "trust-v2" / name).read_bytes())
    with (v2 / "shard-000.json").open("ab") as handle:
        handle.write(b"tamper")
    database = tmp_path / "never-created.sqlite"
    completed = run_cli(
        [
            "load",
            "--db",
            str(database),
            "--v2",
            str(v2),
            "--sample",
            str(sample_dir),
            "--clock-ms",
            str(BASE_MS),
        ],
        check=False,
    )
    assert completed.returncode == 2
    response = json.loads(completed.stdout)
    assert response["error"]["code"] == ErrorCode.HASH_MISMATCH
    assert not database.exists()


def test_replacement_generation_and_clock_rollback(tmp_path, merged_view, sample_dir):
    from certlab.store import StateStore

    store = StateStore(tmp_path / "state.sqlite")
    store.initialize(merged_view, sample_dir=sample_dir, clock=CLOCK)
    with pytest.raises(CertLabError) as generation:
        store.initialize(
            {
                **merged_view,
                "view_digest": "different-at-same-generation",
            },
            sample_dir=sample_dir,
            clock=LogicalClock(BASE_MS),
        )
    assert generation.value.code == ErrorCode.GENERATION_ROLLBACK
    with pytest.raises(CertLabError) as clock:
        store.initialize(
            merged_view,
            sample_dir=sample_dir,
            clock=LogicalClock(BASE_MS - 10_000),
        )
    assert clock.value.code == ErrorCode.CLOCK_ROLLBACK
    with pytest.raises(CertLabError) as clock:
        store.initialize(
            {**merged_view, "generation": 4, "view_digest": "different"},
            sample_dir=sample_dir,
            clock=LogicalClock(BASE_MS - 10_000),
        )
    assert clock.value.code == ErrorCode.CLOCK_ROLLBACK

