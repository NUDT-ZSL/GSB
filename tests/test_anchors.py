"""Snapshot management: schema/policy validation, replacement detection,
and order-independent effective-version resolution."""

import os

from certtrust import errors
from certtrust.anchors import SnapshotStore, is_replacement, parse_snapshot_files

from tests.conftest import load_basic


def test_unknown_policy_version_rejected(samples):
    path = os.path.join(samples["dirs"]["unknown_policy"],
                        "snap_unknown_policy.json")
    snaps, rejects = parse_snapshot_files([path])
    assert snaps == []
    assert rejects[0]["code"] == errors.E_POLICY_UNKNOWN


def test_bad_policy_format_rejected(samples):
    path = os.path.join(samples["dirs"]["unknown_policy"],
                        "snap_bad_policy_format.json")
    snaps, rejects = parse_snapshot_files([path])
    assert snaps == []
    assert rejects[0]["code"] == errors.E_SNAPSHOT_POLICY


def test_bad_structure_version_rejected(samples):
    path = os.path.join(samples["dirs"]["unknown_policy"],
                        "snap_bad_structure.json")
    snaps, rejects = parse_snapshot_files([path])
    assert snaps == []
    assert rejects[0]["code"] == errors.E_SNAPSHOT_STRUCTURE_VERSION


def test_missing_observation_time_is_structurally_valid(samples):
    path = os.path.join(samples["dirs"]["unknown_policy"],
                        "snap_no_obs_time.json")
    snaps, rejects = parse_snapshot_files([path])
    assert rejects == []
    assert snaps[0].observed_at_ms is None


def test_effective_version_is_arrival_order_independent(samples):
    _, _, snaps, _ = load_basic(samples)
    ordered = [snaps[v] for v in (1, 2, 3, 4)]
    shuffled = [snaps[v] for v in (3, 1, 4, 2)]
    store_a = SnapshotStore(ordered)
    store_b = SnapshotStore(shuffled)
    assert store_a.effective.stable_key == store_b.effective.stable_key
    assert store_a.effective.snapshot_version == 4


def test_same_version_resolved_by_stable_key(samples):
    _, _, snaps, _ = load_basic(samples)
    base = snaps[2]
    variant_data = base.content_dict()
    variant_data["trusted_roots"] = ["root-a"]  # same version, other content
    from certtrust.models import Snapshot
    variant = Snapshot.from_dict(variant_data)
    store = SnapshotStore([base, variant])
    expected = max(base, variant, key=lambda s: s.sort_key)
    assert store.effective.stable_key == expected.stable_key


def test_replacement_detection(samples):
    _, _, snaps, _ = load_basic(samples)
    v1, v2, v4 = snaps[1], snaps[2], snaps[4]
    assert is_replacement(v1, v2)  # trusted set grew
    assert is_replacement(v2, v4)  # policy version changed
    assert not is_replacement(v1, v1)


def test_store_add_reports_effective_change(samples):
    _, _, snaps, _ = load_basic(samples)
    store = SnapshotStore()
    assert store.add(snaps[2]) is True
    assert store.add(snaps[1]) is False  # older version never wins
    assert store.effective.snapshot_version == 2
