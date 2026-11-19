from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from shardroute.errors import BizError
from shardroute.router import build_rewrite, merge_params
from shardroute.sample import RulePackage

from .conftest import cli_json, run_cli


def test_apply_switches_whole_snapshot_and_binds_old_snapshot(state, sample):
    old = state.read_snapshot()
    template = state.query_template("orders_eq")
    old_rewrite = build_rewrite(
        old.snapshot,
        template,
        merge_params(template, None),
        old.version,
    )
    assert old_rewrite.target_shards == (2,)
    applied = state.apply_rule(sample.rule("r2"), 2000)
    assert applied["activated"] is True
    assert applied["generation"] == 2
    new = state.read_snapshot()
    new_rewrite = build_rewrite(
        new.snapshot,
        template,
        merge_params(template, None),
        new.version,
    )
    assert new.version == "r2"
    assert new_rewrite.target_shards == (0,)
    assert new_rewrite.shards[0].table == "orders_v2_00"
    assert old_rewrite.target_shards == (2,)
    assert old_rewrite.shards[0].table == "orders_02"


def test_reapplying_current_version_is_idempotent(state, sample):
    first = state.apply_rule(sample.rule("r2"), 2000)
    second = state.apply_rule(sample.rule("r2"), 2100)
    assert first["activated"] is True
    assert second["activated"] is False
    assert second["idempotent"] is True
    connection = state.connect()
    try:
        activations = connection.execute(
            "SELECT COUNT(*) FROM rule_activations WHERE version = 'r2'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert activations == 1


def test_version_rollback_is_rejected(state, sample):
    state.apply_rule(sample.rule("r2"), 2000)
    with pytest.raises(BizError) as raised:
        state.apply_rule(sample.rule("r1"), 2200)
    assert raised.value.code == "E_RULE_ROLLBACK"
    assert state.read_snapshot().version == "r2"


def test_rollback_package_is_rejected(state, sample):
    with pytest.raises(BizError) as raised:
        state.apply_rule(sample.rule("r3-rollback"), 3000)
    assert raised.value.code == "E_RULE_ROLLBACK"
    assert state.read_snapshot().version == "r1"


def test_clock_before_effective_is_rejected(state, sample):
    with pytest.raises(BizError) as raised:
        state.apply_rule(sample.rule("r2"), 1999)
    assert raised.value.code == "E_CLOCK_BEFORE_EFFECTIVE"
    assert state.read_snapshot().version == "r1"


def test_same_version_different_digest_is_conflict(state, sample):
    original = sample.rule("r2")
    tampered = RulePackage(
        version=original.version,
        digest="sha256:" + "a" * 64,
        effective_clock_ms=original.effective_clock_ms,
        rollback_target=original.rollback_target,
        snapshot=original.snapshot,
    )
    with pytest.raises(BizError) as raised:
        state.apply_rule(tampered, 2000)
    assert raised.value.code == "E_RULE_CONFLICT"
    assert state.read_snapshot().version == "r1"


def test_concurrent_apply_activates_once(tmp_path: Path):
    state_path = tmp_path / "state.db"
    cli_json(
        "load",
        "--sample",
        "samples/sample.json",
        "--clock",
        "1000",
        state_path=state_path,
    )
    def apply_once():
        return run_cli(
            "apply",
            "--sample",
            "samples/sample.json",
            "--version",
            "r2",
            "--clock",
            "2000",
            state_path=state_path,
            check=False,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(lambda _: apply_once(), range(2)))
    assert first.returncode == 0
    assert second.returncode == 0
    outputs = sorted(
        [
            json.loads(first.stdout)["activated"],
            json.loads(second.stdout)["activated"],
        ]
    )
    assert outputs == [False, True]
    connection = sqlite3.connect(state_path)
    try:
        activation_count = connection.execute(
            "SELECT COUNT(*) FROM rule_activations WHERE version = 'r2'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert activation_count == 1

