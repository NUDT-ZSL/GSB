import json
import numpy as np
import pytest

from piano_workbench.controller import Controller
from piano_workbench.errors import WorkbenchError
from piano_workbench.pcm import hash_pcm
from piano_workbench.workspace import MANIFEST_NAME, Workspace


@pytest.fixture()
def populated(controller, piano_wav):
    _, pcm = piano_wav
    n = pcm.size
    controller.apply_operation("denoise", 0, n // 4)
    controller.apply_operation("fade", n // 2, 3 * n // 4, {"mode": "out"})
    controller.undo()
    return controller


def test_manifest_and_cache_layout(populated):
    ws = populated.workspace
    assert (ws.root / MANIFEST_NAME).exists()
    cache_files = list(ws.cache_dir.glob("*.wav"))
    assert len(cache_files) == len(ws.nodes)
    manifest = json.loads((ws.root / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["committed"] is True
    assert manifest["format_version"] == 1
    assert manifest["current_id"] == ws.current_id


def test_reopen_restores_identical_state_and_pcm(populated):
    ws = populated.workspace
    current_id = ws.current_id
    expected_pcm = ws.node_pcm(current_id)
    snapshot_before = ws.snapshot()

    reopened = Workspace.open(ws.root)
    assert reopened.current_id == current_id
    assert reopened.undo_stack == ws.undo_stack
    assert reopened.redo_stack == ws.redo_stack
    assert np.array_equal(reopened.node_pcm(current_id), expected_pcm)
    assert reopened.snapshot()["node_count"] == snapshot_before["node_count"]


def test_cache_hash_mismatch_is_detected_and_state_untouched(populated):
    ws = populated.workspace
    current_id = ws.current_id
    good_pcm = ws.node_pcm(current_id)
    node = ws.nodes[current_id]
    target = ws.cache_path(node["cache"]["sha256"])
    backup = target.read_bytes()
    try:
        target.write_bytes(backup[:-44] + b"\x00" * 44)
        with pytest.raises(WorkbenchError) as info:
            Workspace.open(ws.root)
        assert info.value.code == "E_CACHE_HASH"
        # The manifest on disk was never modified.
        assert json.loads((ws.root / MANIFEST_NAME).read_text())["current_id"] == current_id
    finally:
        target.write_bytes(backup)
    assert np.array_equal(Workspace.open(ws.root).node_pcm(current_id), good_pcm)


def test_missing_cache_reported_as_cache_hash_error(populated):
    ws = populated.workspace
    node = ws.nodes[ws.current_id]
    target = ws.cache_path(node["cache"]["sha256"])
    backup = target.read_bytes()
    try:
        target.unlink()
        with pytest.raises(WorkbenchError) as info:
            Workspace.open(ws.root)
        assert info.value.code == "E_CACHE_HASH"
    finally:
        target.write_bytes(backup)


def test_version_conflict_leaves_workspace_untouched(populated):
    ws = populated.workspace
    manifest_path = ws.root / MANIFEST_NAME
    original = manifest_path.read_text(encoding="utf-8")
    with pytest.raises(WorkbenchError) as info:
        Workspace.open(ws.root, expected_version=999)
    assert info.value.code == "E_VERSION_CONFLICT"
    assert info.value.to_json_dict()["ok"] is False
    assert manifest_path.read_text(encoding="utf-8") == original


def test_forced_termination_reclaims_orphan_and_keeps_stable_state(populated):
    ws = populated.workspace
    from piano_workbench.synth import synthesize_piano

    stable_id = ws.current_id
    stable_pcm = ws.node_pcm(stable_id)

    # Simulate an interrupted commit: cache file landed, manifest did not.
    orphan_pcm = synthesize_piano(duration_seconds=0.5, midi_note=48)
    orphan_hash = ws._write_cache(orphan_pcm)
    ws.write_exit_marker("forced", {"reason": "test_sigkill"})

    assert ws.cache_path(orphan_hash).exists()
    reopened = Workspace.open(ws.root)
    assert not reopened.cache_path(orphan_hash).exists()
    assert (reopened.orphan_dir / f"{orphan_hash}.wav").exists()
    assert reopened.last_recovery["orphan_count"] == 1
    assert reopened.last_recovery["previous_exit"]["kind"] == "forced"
    assert np.array_equal(reopened.node_pcm(stable_id), stable_pcm)


def test_controlled_exit_marker_is_honest(populated):
    ws = populated.workspace
    ws.close()
    marker = json.loads(ws.exit_path.read_text(encoding="utf-8"))
    assert marker["kind"] == "controlled"
    reopened = Workspace.open(ws.root)
    assert reopened.last_recovery["previous_exit"]["kind"] == "controlled"
    # Marker is consumed once the new session starts.
    assert not reopened.exit_path.exists()


def test_node_missing_error_chain(populated):
    with pytest.raises(WorkbenchError) as info:
        populated.render_node("n_ghost")
    assert info.value.code == "E_NODE_MISSING"
    assert info.value.to_json_dict()["detail"]["node_id"] == "n_ghost"


def test_repeated_rebuild_byte_identical_across_reopens(populated):
    ws = populated.workspace
    node_ids = list(ws.nodes)
    first = {nid: populated.render_node(nid) for nid in node_ids}
    reopened = Controller(Workspace.open(ws.root))
    for nid in node_ids:
        second = reopened.render_node(nid)
        assert np.array_equal(first[nid], second)
        assert hash_pcm(second) == ws.nodes[nid]["cache"]["sha256"]


def test_no_partial_write_on_bad_commit(populated):
    ws = populated.workspace
    manifest_before = (ws.root / MANIFEST_NAME).read_bytes()
    node_count_before = len(ws.nodes)
    with pytest.raises(WorkbenchError):
        # Pointer references a node that does not exist -> commit refused.
        ws.update_pointers("n_missing", [], [])
    assert (ws.root / MANIFEST_NAME).read_bytes() == manifest_before
    assert len(ws.nodes) == node_count_before
