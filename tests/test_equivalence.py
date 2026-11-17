"""Requirement 6: incremental decoding after an edit sequence must equal a
blank-workspace full decode under the same final policy and request set,
field by field, for greedy/sampling/beam and v1/v2 source policies."""

import pytest
from conftest import fresh_workbench, make_workspace, semantic

EDIT_SEQUENCE = [
    {"kind": "global_set", "fields": {"temperature": 0.9}},
    {"kind": "override", "request_id": "req-002", "fields": {"top_k": 3}},
    {"kind": "replay", "request_ids": ["req-001", "req-003"]},
    {"kind": "switch_version", "target": "v1"},
    {"kind": "global_set", "fields": {"top_p": 0.5}},
    {"kind": "switch_version", "target": "v2"},
    {"kind": "switch_version", "target": "v1"},
    {"kind": "delete_request", "request_id": "req-005"},
]

DECODER_SETUP = {
    "greedy": {"decoder_type": "greedy"},
    "sampling": {"decoder_type": "sampling"},
    "beam": {"decoder_type": "beam", "beam_width": 3, "length_alpha": 0.7},
}


def _sequence(decoder):
    return [{"kind": "global_set", "fields": DECODER_SETUP[decoder]}] + EDIT_SEQUENCE


@pytest.mark.parametrize("decoder", ["greedy", "sampling", "beam"])
@pytest.mark.parametrize("source_version", ["v1", "v2"])
def test_incremental_equals_full(tmp_path, decoder, source_version):
    edits = _sequence(decoder)

    # Incremental: run after every edit.
    inc_root, inc_config = make_workspace(
        tmp_path / "inc", active_policy=source_version
    )
    wb = fresh_workbench(inc_root, inc_config)
    result = None
    for edit in edits:
        wb.apply_edit(edit)
        result = wb.run()

    # Full: blank workspace, same final policy and request set, one run.
    full_root, full_config = make_workspace(
        tmp_path / "full", active_policy=source_version
    )
    wb_full = fresh_workbench(full_root, full_config)
    for edit in edits:
        wb_full.apply_edit(edit)
    full_result = wb_full.run()

    assert semantic(result) == semantic(full_result)


@pytest.mark.parametrize("decoder", ["greedy", "sampling", "beam"])
def test_batch_replay_equals_individual(tmp_path, decoder):
    root, config = make_workspace(tmp_path)
    wb = fresh_workbench(root, config)
    wb.apply_edit({"kind": "global_set", "fields": DECODER_SETUP[decoder]})
    wb.run()

    ids = ["req-001", "req-003", "req-004"]
    batch = wb.replay(ids)
    # Stable request-ID order regardless of input order.
    assert [t["request_id"] for t in batch] == sorted(ids)
    individual = [wb.replay([rid])[0] for rid in sorted(ids)]
    assert batch == individual
    # Batch replay matches the traces produced by the full run.
    result = wb.run()
    by_id = {e["request_id"]: e for e in result["requests"]}
    for trace in batch:
        entry = by_id[trace["request_id"]]
        assert trace["tokens"] == entry["tokens"]
        assert trace["steps"] == entry["steps"]


def test_override_isolation_and_global_invalidation(tmp_path):
    root, config = make_workspace(tmp_path)
    wb = fresh_workbench(root, config)
    wb.apply_edit({"kind": "override", "request_id": "req-002", "fields": {"top_k": 3}})
    first = wb.run()
    # Global modification: all non-overridden traces invalidated, the
    # explicitly overridden request keeps its cache hit.
    wb.apply_edit({"kind": "global_set", "fields": {"temperature": 0.33}})
    second = wb.run()
    by_id = {e["request_id"]: e for e in second["requests"]}
    assert by_id["req-002"]["cache"] == {"status": "reused", "invalidation": None}
    for rid, entry in by_id.items():
        if rid != "req-002":
            assert entry["cache"]["status"] == "recomputed"
            assert entry["cache"]["invalidation"] == "policy"
    # The overridden request's trace is unchanged.
    first_tokens = {e["request_id"]: e["tokens"] for e in first["requests"]}
    assert by_id["req-002"]["tokens"] == first_tokens["req-002"]


def test_version_switch_marks_loss_fields(tmp_path):
    root, config = make_workspace(tmp_path, active_policy="v2")
    wb = fresh_workbench(root, config)
    wb.run()
    wb.apply_edit({"kind": "override", "request_id": "req-001", "fields": {"top_k": 2}})
    wb.apply_edit({"kind": "switch_version", "target": "v1"})
    result = wb.run()
    assert result["downgrade_losses"], "lossy downgrade must record losses"
    marked = {
        e["request_id"]: e["loss_marks"]
        for e in result["requests"]
        if e["request_id"] != "req-001"
    }
    for marks in marked.values():
        assert marks == result["downgrade_losses"]
    # Overridden request is not affected by the global conversion losses.
    overridden = [e for e in result["requests"] if e["request_id"] == "req-001"][0]
    assert overridden["loss_marks"] == []
