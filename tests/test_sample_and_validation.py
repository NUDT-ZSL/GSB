from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from diagnostic_workbench.engine import Workbench
from diagnostic_workbench.errors import BusinessError
from diagnostic_workbench.schema import parse_sample

from .conftest import ROOT, SAMPLE_PATH


def test_sample_generation_is_reproducible() -> None:
    manifest = json.loads(
        (ROOT / "samples" / "manifest.json").read_text(encoding="utf-8")
    )
    sample = parse_sample(
        json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
    )
    assert sample.sample_digest == manifest["sample_digest"]
    assert sample.policy_digest == manifest["policy_digest"]
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "generate_samples.py"),
            "--check",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "reproducible" in result.stdout


def _assert_no_committed_state(state_dir: Path) -> None:
    database = state_dir / "workbench.sqlite3"
    assert database.exists()
    with sqlite3.connect(database) as connection:
        policy_count = connection.execute(
            "SELECT COUNT(*) FROM policy_versions"
        ).fetchone()[0]
        snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM snapshots"
        ).fetchone()[0]
        current_count = connection.execute(
            "SELECT COUNT(*) FROM current_snapshot"
        ).fetchone()[0]
    assert policy_count == 0
    assert snapshot_count == 0
    assert current_count == 0


def test_freeze_order_validation_has_stable_code_and_path(
    state_dir: Path,
    sample: dict,
) -> None:
    broken = copy.deepcopy(sample)
    broken["policy"]["freeze_order"] = ["zone-core", "zone-east"]
    workbench = Workbench(state_dir)
    with pytest.raises(BusinessError) as caught:
        workbench.load({"sample": broken})
    error = caught.value
    assert error.code == "FREEZE_ORDER_CONFLICT"
    assert error.field_path == "policy.freeze_order"
    assert error.context["domain"] == "zone-west"
    _assert_no_committed_state(state_dir)


def test_cycle_validation_rejects_upstream_cycle(
    state_dir: Path,
    sample: dict,
) -> None:
    broken = copy.deepcopy(sample)
    broken["topology"]["services"][0]["upstream"] = ["orders"]
    workbench = Workbench(state_dir)
    with pytest.raises(BusinessError) as caught:
        workbench.load({"sample": broken})
    assert caught.value.code == "INVALID_TOPOLOGY_CYCLE"
    assert caught.value.field_path == "topology.services[0].upstream"
    _assert_no_committed_state(state_dir)


def test_invalid_failback_ladder(
    state_dir: Path,
    sample: dict,
) -> None:
    broken = copy.deepcopy(sample)
    broken["policy"]["services"]["auth"]["failback_ladder"] = [1.0, 1.0, 0.0]
    workbench = Workbench(state_dir)
    with pytest.raises(BusinessError) as caught:
        workbench.load({"sample": broken})
    error = caught.value
    assert error.code == "INVALID_FAILBACK_LADDER"
    assert error.field_path == "policy.services.auth.failback_ladder[1]"
    assert error.context["service"] == "auth"
    assert error.context["domain"] == "zone-core"
    _assert_no_committed_state(state_dir)


def test_non_finite_metric_rejected(
    state_dir: Path,
    sample: dict,
) -> None:
    broken = copy.deepcopy(sample)
    broken["events"][0]["value"] = float("nan")
    workbench = Workbench(state_dir)
    with pytest.raises(BusinessError) as caught:
        workbench.load({"sample": broken})
    assert caught.value.code == "INVALID_EVENT"
    assert caught.value.field_path == "events[0].value"
    _assert_no_committed_state(state_dir)


def test_duplicate_instance_and_event_sequence(
    state_dir: Path,
    sample: dict,
) -> None:
    duplicate = copy.deepcopy(sample)
    duplicate["topology"]["services"][1]["instances"][0]["instance_id"] = (
        "auth-core-a"
    )
    with pytest.raises(BusinessError) as duplicate_error:
        parse_sample(duplicate)
    assert duplicate_error.value.code == "INVALID_TOPOLOGY"
    assert duplicate_error.value.field_path == "topology.services[1].instances[0].instance_id"

    sequence = copy.deepcopy(sample)
    sequence["events"][1]["seq"] = sequence["events"][0]["seq"]
    with pytest.raises(BusinessError) as sequence_error:
        parse_sample(sequence)
    assert sequence_error.value.code == "INVALID_EVENT_SEQUENCE"
    assert sequence_error.value.field_path == "events[1].seq"


def test_digest_mismatch_does_not_persist(
    state_dir: Path,
    sample: dict,
) -> None:
    workbench = Workbench(state_dir)
    with pytest.raises(BusinessError) as caught:
        workbench.load(
            {
                "sample": sample,
                "expected_sample_digest": "0" * 64,
            }
        )
    assert caught.value.code == "SAMPLE_DIGEST_MISMATCH"
    assert caught.value.http_status == 409
    _assert_no_committed_state(state_dir)

