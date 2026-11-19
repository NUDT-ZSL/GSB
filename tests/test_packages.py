"""Requirement 7: package replacement, version recognition, rollback ban."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from conftest import build_plan
from recovery_pipeline.errors import BizError, Err
from recovery_pipeline.packages import (classify_package,
                                        collect_old_versions,
                                        register_package_meta)


def _install(store, samples, version):
    plan, _, _ = build_plan(samples)
    view = register_package_meta(plan.view(), samples["v2"])
    store.install_plan(version, view, plan.migration_map)
    return plan


def _add_shard(samples, name="shards/shard-2.json"):
    v2 = Path(samples["v2"])
    payload = {"services": [{
        "name": "svc-epsilon", "effective_ms": 902000, "generation": 2,
        "priority": "P2", "upstream": ["svc-beta"], "tombstone": False}]}
    blob = json.dumps(payload, indent=2, sort_keys=True).encode()
    (v2 / name).write_bytes(blob)
    manifest = json.loads((v2 / "manifest.json").read_text())
    manifest["shards"].append(
        {"path": name, "sha256": hashlib.sha256(blob).hexdigest()})
    (v2 / "manifest.json").write_text(json.dumps(manifest, indent=2))


def _bump_manifest(samples, version):
    v2 = Path(samples["v2"])
    manifest = json.loads((v2 / "manifest.json").read_text())
    manifest["manifest_version"] = version
    (v2 / "manifest.json").write_text(json.dumps(manifest, indent=2))


def test_classify_full_replace_then_noop(store, samples):
    assert classify_package(store, samples["v2"]) == "FULL_REPLACE"
    _install(store, samples, version=2)
    assert classify_package(store, samples["v2"]) == "NOOP"


def test_classify_incremental_on_added_shard(store, samples):
    _install(store, samples, version=2)
    _add_shard(samples)
    assert classify_package(store, samples["v2"]) == "INCREMENTAL"


def test_classify_full_replace_on_changed_shard(store, samples):
    _install(store, samples, version=2)
    shard = Path(samples["v2"]) / "shards" / "shard-0.json"
    payload = json.loads(shard.read_text())
    payload["services"][0]["effective_ms"] = 910000
    blob = json.dumps(payload, indent=2, sort_keys=True).encode()
    shard.write_bytes(blob)
    manifest = json.loads((Path(samples["v2"]) / "manifest.json").read_text())
    manifest["shards"][0]["sha256"] = hashlib.sha256(blob).hexdigest()
    (Path(samples["v2"]) / "manifest.json").write_text(json.dumps(manifest))
    assert classify_package(store, samples["v2"]) == "FULL_REPLACE"


def test_version_regression_rejected_and_audited(store, samples):
    _install(store, samples, version=2)
    _bump_manifest(samples, 1)
    with pytest.raises(BizError) as ei:
        classify_package(store, samples["v2"])
    assert ei.value.code == Err.PACKAGE_VERSION_REGRESSION
    events = store.audit_events()
    assert any(e == "package_rejected" for e, _ in events)
    assert store.current_plan_version() == 2  # pointer untouched


def test_full_replace_flips_pointer_atomically(store, samples):
    _install(store, samples, version=2)
    _add_shard(samples)
    _bump_manifest(samples, 3)
    assert classify_package(store, samples["v2"]) == "INCREMENTAL"
    _install(store, samples, version=3)
    assert store.current_plan_version() == 3
    view = store.load_plan_view(3)
    assert "svc-alpha" in view["services"]


def test_old_version_collected_only_without_refs(store, samples, target):
    from conftest import make_clock
    from recovery_pipeline.executor import Orchestrator
    plan = _install(store, samples, version=2)
    orch = Orchestrator(store, make_clock(), target.url)
    orch.run_failover(plan, 2)  # commits references to version 2
    _install(store, samples, version=3)
    assert collect_old_versions(store) == []  # version 2 still referenced
    _install(store, samples, version=4)
    assert collect_old_versions(store) == [3]  # version 3 has no refs
    assert store.current_plan_version() == 4
