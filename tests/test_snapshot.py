"""Requirement 7: versioned snapshots, stale detection, graceful stop and
hard-kill recovery."""

import json
import os

from conftest import fresh_workbench, make_workspace, semantic

from workbench.errors import E_SNAPSHOT_STALE


def test_snapshot_hit_and_reuse_after_restart(tmp_path):
    root, config = make_workspace(tmp_path)
    wb = fresh_workbench(root, config)
    first = wb.run()
    assert first["snapshot"]["status"] == "fresh"

    # New process (fresh in-memory state) on the same workdir: manifest hit,
    # completed requests recovered, traces reused without re-decoding.
    wb2 = fresh_workbench(root, config)
    second = wb2.run()
    assert second["snapshot"]["status"] == "hit"
    assert second["snapshot"]["recovered"] is True
    assert second["snapshot"]["resume_point"] == "req-005"
    ok = [e for e in second["requests"]]
    assert all(e["cache"]["status"] == "reused" for e in ok)
    assert semantic(first) == semantic(second)


def test_snapshot_stale_on_version_mismatch(tmp_path):
    root, config = make_workspace(tmp_path)
    wb = fresh_workbench(root, config)
    wb.run()

    config["snapshot_version"] = 99
    with open(os.path.join(root, "samples", "config.json"), "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    wb2 = fresh_workbench(root, config)
    result = wb2.run()
    assert result["snapshot"]["status"] == "stale"
    stale = [f for f in result["failures"] if f["code"] == E_SNAPSHOT_STALE]
    assert stale, "E_SNAPSHOT_STALE must be reported"
    # recover_from_snapshot=True: completed list recovered from the last
    # fully replaced manifest.
    assert result["snapshot"]["recovered"] is True
    assert result["snapshot"]["completed_requests"]


def test_snapshot_stale_without_recovery(tmp_path):
    root, config = make_workspace(tmp_path, recover_from_snapshot=False)
    wb = fresh_workbench(root, config)
    wb.run()

    config["snapshot_version"] = 99
    wb2 = fresh_workbench(root, config)
    result = wb2.run()
    assert result["snapshot"]["status"] == "stale"
    assert result["snapshot"]["recovered"] is False
    assert result["snapshot"]["completed_requests"] == []


def test_graceful_stop_persists_progress_and_exit_state(tmp_path):
    root, config = make_workspace(tmp_path, max_workers=1)
    wb = fresh_workbench(root, config)

    original = wb._decode_one
    calls = {"n": 0}

    def stop_after_first(request):
        trace = original(request)
        calls["n"] += 1
        if calls["n"] == 1:
            wb.request_stop()  # simulate SIGTERM after the first request
        return trace

    wb._decode_one = stop_after_first
    result = wb.run()
    assert result["stopped"] is True
    # Exactly one request trajectory landed before the stop.
    assert len(result["requests"]) == 1

    manifest = json.load(open(os.path.join(root, "out", "snapshot", "manifest.json")))
    assert manifest["completed_requests"] == ["req-001"]

    # Restart (as after a hard kill): resume from fully replaced artifacts.
    wb2 = fresh_workbench(root, config)
    resumed = wb2.run()
    assert resumed["stopped"] is False
    assert resumed["snapshot"]["recovered"] is True
    assert resumed["snapshot"]["resume_point"] == "req-001"
    reused = [e for e in resumed["requests"] if e["request_id"] == "req-001"][0]
    assert reused["cache"]["status"] == "reused"
    # The full final result equals an uninterrupted full decode.
    root_full, config_full = make_workspace(tmp_path / "full", max_workers=1)
    wb_full = fresh_workbench(root_full, config_full)
    full = wb_full.run()
    assert semantic(resumed) == semantic(full)


def test_artifacts_written_atomically(tmp_path):
    root, config = make_workspace(tmp_path)
    wb = fresh_workbench(root, config)
    wb.run()
    # No temp files left behind after atomic replacement.
    for dirpath, _, files in os.walk(os.path.join(root, "out")):
        for name in files:
            assert not name.endswith(".tmp"), name
    # Manifest records versions and fingerprints.
    manifest = json.load(open(os.path.join(root, "out", "snapshot", "manifest.json")))
    assert manifest["global_version"] == 1
    assert manifest["policy_source_version"] == "v2"
    assert manifest["policy_fingerprint"]
    assert manifest["score_fingerprint"]
    assert manifest["artifacts"] == {"policy": 1, "traces": 1, "bypass": 1}
