import numpy as np
import pytest

from piano_workbench.controller import UNDO_LIMIT
from piano_workbench.errors import WorkbenchError


def _apply_few(controller, pcm):
    n = pcm.size
    controller.apply_operation("denoise", 0, n // 4)
    controller.apply_operation("envelope", n // 4, n // 2,
                               {"gain_start": 1.0, "gain_end": 0.4})
    controller.apply_operation("fade", n // 2, 3 * n // 4, {"mode": "out"})


def test_undo_redo_returns_identical_pcm(controller, piano_wav):
    _, pcm = piano_wav
    _apply_few(controller, pcm)
    head = controller.current_node()["id"]
    head_pcm = controller.render_current()

    controller.undo()
    middle_id = controller.current_node()["id"]
    assert middle_id != head
    middle_pcm = controller.render_current()
    assert np.array_equal(middle_pcm, controller.workspace.node_pcm(middle_id))

    controller.undo()
    controller.undo()
    assert controller.current_node()["id"] == controller.workspace.root_id
    assert np.array_equal(controller.render_current(), pcm)

    for _ in range(3):
        controller.redo()
    assert controller.current_node()["id"] == head
    assert np.array_equal(controller.render_current(), head_pcm)


def test_redo_preserves_branch_node_parameters(controller, piano_wav):
    _, pcm = piano_wav
    node = controller.apply_operation(
        "envelope", 0, 4096, {"gain_start": 0.7, "gain_end": 0.1}
    )
    controller.undo()
    redone = controller.redo()
    assert redone["id"] == node["id"]
    assert redone["params"] == node["params"]
    assert redone["parent_id"] == node["parent_id"]


def test_new_operation_truncates_redo_branch(controller, piano_wav):
    _, pcm = piano_wav
    controller.apply_operation("fade", 0, 4096, {"mode": "in"})
    controller.apply_operation("fade", 8192, 12288, {"mode": "out"})
    controller.undo()
    assert len(controller.workspace.redo_stack) == 1
    truncated_node = controller.workspace.nodes[controller.workspace.redo_stack[0]]

    controller.apply_operation("denoise", 16384, 20480)
    assert controller.workspace.redo_stack == []
    # Truncated nodes stay in the tree and are still reconstructable.
    assert truncated_node["id"] in controller.workspace.nodes
    pcm_truncated = controller.render_node(truncated_node["id"])
    assert pcm_truncated.dtype == "<i2"


def test_redo_with_wrong_parent_is_rejected(controller, piano_wav):
    _, pcm = piano_wav
    controller.apply_operation("fade", 0, 4096, {"mode": "in"})
    controller.undo()
    # Corrupt the redo pointer target's parent linkage to prove the guard.
    target = controller.workspace.redo_stack[0]
    original_parent = controller.workspace.nodes[target]["parent_id"]
    controller.workspace.nodes[target]["parent_id"] = "n_fabricated"
    with pytest.raises(WorkbenchError) as info:
        controller.redo()
    assert info.value.code == "E_STATE"
    controller.workspace.nodes[target]["parent_id"] = original_parent


def test_undo_redo_empty_stacks_report_state_error(controller):
    with pytest.raises(WorkbenchError) as info:
        controller.undo()
    assert info.value.code == "E_STATE"
    controller.apply_operation("fade", 0, 2048, {"mode": "in"})
    with pytest.raises(WorkbenchError) as info:
        controller.redo()
    assert info.value.code == "E_STATE"


def test_undo_limit_is_200(controller, piano_wav):
    _, pcm = piano_wav
    # 240 disjoint 256-sample selections fit in the 96000-sample clip.
    count = 240
    for index in range(count):
        start = index * 384
        end = start + 256
        if end >= pcm.size:
            break
        controller.apply_operation("fade", start, end, {"mode": "in"})
    assert len(controller.workspace.undo_stack) <= UNDO_LIMIT
    assert len(controller.workspace.undo_stack) == UNDO_LIMIT
    # The oldest reachable history is bounded, but all nodes remain stored.
    for _ in range(UNDO_LIMIT):
        controller.undo()
    assert controller.can_redo()


def test_branch_switch_keeps_all_nodes_reachable(controller, piano_wav):
    branch_a = controller.apply_operation("fade", 0, 4096, {"mode": "in"})
    controller.undo()
    branch_b = controller.apply_operation("fade", 8192, 12288, {"mode": "out"})

    controller.checkout_node(branch_a["id"])
    assert controller.current_node()["id"] == branch_a["id"]
    assert branch_b["id"] in controller.workspace.nodes
    # Switching back reconstructs exactly the branch-B waveform.
    controller.checkout_node(branch_b["id"])
    assert controller.current_node()["id"] == branch_b["id"]
    assert controller.workspace.redo_stack == []
