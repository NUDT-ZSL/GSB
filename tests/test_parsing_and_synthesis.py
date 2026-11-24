from __future__ import annotations

from sensitivity_engine.loader import load_dataset
from sensitivity_engine.synthesis import flatten_basis_source_keys


def test_invalid_sample_emits_stable_codes(sample_root):
    dataset = load_dataset(sample_root / "data" / "invalid")
    codes = {issue.code for issue in dataset.issues}
    assert {
        "E_DUPLICATE_OBJECT_KEY",
        "E_PARENT_NOT_FOUND",
        "E_SELF_PARENT",
        "E_INVALID_INHERITANCE_MARK",
        "E_INVALID_LEVEL",
        "E_SEQ_NOT_MONOTONIC",
    } <= codes
    seq_issue = next(issue for issue in dataset.issues if issue.code == "E_SEQ_NOT_MONOTONIC")
    assert seq_issue.batch_id == "batch:invalid-seq"
    assert seq_issue.op_seq == 1


def test_effective_levels_and_all_sources(imported_store):
    query = imported_store.query_ledger()
    levels = {item["object_key"]: item["effective_level"] for item in query["objects"]}
    assert levels["obj:finance"] == "RESTRICTED"
    assert levels["obj:finance-audit"] == "RESTRICTED"
    assert levels["obj:hr"] == "CONFIDENTIAL"
    assert levels["obj:hr-secret"] == "RESTRICTED"
    assert levels["obj:empty"] is None
    assert levels["obj:cycle-target"] is None

    finance = imported_store.query_ledger("obj:finance")
    alice = next(row for row in finance["trustees"] if row["trustee_id"] == "trustee:alice")
    sources = alice["basis"]["sources"]
    source_keys = {source["source_key"] for source in sources}
    assert "src:finance-compliance-restricted" in source_keys
    assert "src:finance-alice-confidential" in source_keys
    assert alice["effective_level"] == "RESTRICTED"


def test_stable_object_order_and_empty_basis_code(imported_store):
    objects = imported_store.query_ledger()["objects"]
    keys = [item["object_key"] for item in objects]
    assert keys == sorted(keys)
    empty = next(item for item in objects if item["object_key"] == "obj:empty")
    assert empty["decision_status"] == "NO_BASIS"
    assert "E_NO_BASIS" in empty["codes"]


def test_group_cycle_is_undecidable_and_listed(imported_store):
    result = imported_store.query_as_of(20, "obj:cycle-target")
    assert result["decision_status"] == "UNDECIDABLE"
    assert ["group:cycle-a", "group:cycle-b"] in result["group_cycles"]
    assert ["group:self-loop"] in result["group_cycles"]
    assert "E_GROUP_CYCLE" in result["codes"]


def test_basis_flatten_uses_persisted_json(imported_store):
    entry = imported_store.get_object_ledger_entry("obj:revoke-retain")
    keys = {source["source_key"] for source in flatten_basis_source_keys(entry)}
    assert "src:retain-alt-bob-restricted" in keys
