import numpy as np
import pytest

from piano_workbench.errors import WorkbenchError
from piano_workbench.nodes import build_node, node_from_dict, node_to_dict
from piano_workbench.pcm import hash_pcm


def test_root_node_is_immutable_baseline(controller, piano_wav):
    _, pcm = piano_wav
    root = controller.current_node()
    assert root["op"] == "root"
    assert root["parent_id"] is None
    assert np.array_equal(controller.render_current(), pcm)
    assert root["cache"]["sha256"] == hash_pcm(pcm)


def test_node_records_op_selection_params_and_parent(controller, piano_wav):
    _, pcm = piano_wav
    node = controller.apply_operation(
        "envelope", 100, 200, {"gain_start": 0.5, "gain_end": 0.25}
    )
    assert node["op"] == "envelope"
    assert node["selection"] == [100, 200]
    assert node["params"]["gain_start"] == 0.5
    assert node["parent_id"] == controller.workspace.root_id
    # Round-trip through the persisted dict representation keeps the id.
    restored = node_from_dict(node_to_dict(node))
    assert restored["id"] == node["id"]


def test_content_addressing_same_operation_same_id():
    a = build_node("n_parent", "fade", (0, 100), {"mode": "in"})
    b = build_node("n_parent", "fade", (0, 100), {"mode": "in"})
    c = build_node("n_parent", "fade", (0, 100), {"mode": "out"})
    assert a["id"] == b["id"]
    assert a["id"] != c["id"]


def test_identical_operation_is_idempotent(controller):
    first = controller.apply_operation("fade", 0, 2048, {"mode": "in"})
    node_count = len(controller.workspace.nodes)
    # Back at the same parent with identical semantics: the content-addressed
    # node already exists and is returned without growing tree or undo stack.
    controller.undo()
    assert len(controller.workspace.undo_stack) == 0
    second = controller.apply_operation("fade", 0, 2048, {"mode": "in"})
    assert first["id"] == second["id"]
    assert len(controller.workspace.nodes) == node_count
    assert len(controller.workspace.undo_stack) == 1


def test_same_selection_allows_stacked_operations(controller):
    controller.apply_operation("denoise", 0, 4096)
    controller.apply_operation("fade", 0, 4096, {"mode": "in"})
    controller.apply_operation("envelope", 0, 4096, {"gain_start": 0.9})
    assert len(controller.workspace.undo_stack) == 3


def test_partial_overlap_rejected_but_disjoint_ranges_allowed(controller, piano_wav):
    _, pcm = piano_wav
    controller.apply_operation("fade", 1000, 3000, {"mode": "in"})
    with pytest.raises(WorkbenchError) as info:
        controller.apply_operation("fade", 2000, 4000, {"mode": "out"})
    assert info.value.code == "E_SEL_RANGE"

    # Disjoint ranges are fine, and the workspace stayed unchanged.
    controller.apply_operation("fade", 5000, 7000, {"mode": "out"})
    assert controller.workspace.current_id != controller.workspace.root_id


def test_invalid_selection_error_codes(controller, piano_wav):
    _, pcm = piano_wav
    bad_calls = [
        (-1, 100),
        (100, 100),
        (200, 100),
        (0, pcm.size + 1),
    ]
    for start, end in bad_calls:
        with pytest.raises(WorkbenchError) as info:
            controller.apply_operation("fade", start, end)
        assert info.value.code == "E_SEL_RANGE"


def test_every_node_rebuilds_full_waveform_and_matches_cache(controller, piano_wav):
    _, pcm = piano_wav
    n = pcm.size
    controller.apply_operation("denoise", 0, n // 4)
    controller.apply_operation("envelope", n // 4, n // 2,
                               {"gain_start": 1.0, "gain_end": 0.5})
    controller.apply_operation("fade", n // 2, 3 * n // 4, {"mode": "out"})
    controller.apply_operation("patch", 3 * n // 4, n - 1024)
    for node_id, node in controller.workspace.nodes.items():
        rebuilt = controller.render_node(node_id)
        cached = controller.workspace.node_pcm(node_id)
        assert np.array_equal(rebuilt, cached)
        assert rebuilt.shape == pcm.shape


def test_persisted_node_id_tampering_is_rejected(piano_wav):
    from piano_workbench.errors import E_CACHE_HASH
    from piano_workbench.nodes import build_root_node
    from piano_workbench.pcm import hash_pcm

    path, pcm = piano_wav
    root = build_root_node(path.name, pcm.size, 48000, hash_pcm(pcm))
    tampered = dict(root)
    tampered["id"] = "n_forged_identity"
    with pytest.raises(WorkbenchError) as info:
        node_from_dict(tampered)
    assert info.value.code == E_CACHE_HASH
