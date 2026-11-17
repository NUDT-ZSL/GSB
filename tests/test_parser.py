import pytest

from sensitrace.errors import E_FIELD, E_VERSION
from sensitrace.parser import SnapshotStructureError, parse_snapshot


def _base():
    return {
        "doc_id": "d",
        "version": "1.2.3",
        "data": {"a": {"b": 1}, "items": [{"v": 9}]},
        "fields": [
            {"path": "a.b", "sensitivity": "plain"},
            {"path": "items[0].v", "sensitivity": "sensitive"},
        ],
        "derivations": [],
    }


def test_valid_snapshot_parses_and_normalises():
    snapshot, diagnostics = parse_snapshot(_base())
    assert snapshot is not None
    assert diagnostics == []
    assert {f.key for f in snapshot.fields} == {"a.b", "items[0].v"}


def test_invalid_document_version_raises_structure_error():
    raw = _base()
    raw["version"] = "1.2"
    with pytest.raises(SnapshotStructureError):
        parse_snapshot(raw)


def test_out_of_range_and_malformed_fields_become_e_field():
    raw = _base()
    raw["fields"] = [
        {"path": "items[9].v", "sensitivity": "sensitive"},
        {"path": "a..b", "sensitivity": "plain"},
        {"path": "a.b", "sensitivity": "unknown-level"},
        {"path": "a.b", "sensitivity": "masked"},  # still accepted
    ]
    snapshot, diagnostics = parse_snapshot(raw)
    codes = {(d.code, d.entry_seq) for d in diagnostics}
    assert (E_FIELD, 1) in codes
    assert (E_FIELD, 2) in codes
    assert (E_FIELD, 3) in codes
    assert len(snapshot.fields) == 1
    assert all(d.version == "1.2.3" for d in diagnostics)


def test_unknown_declaration_version_is_e_version_not_silent_downgrade():
    raw = _base()
    raw["derivations"] = [
        {"source": "a.b", "target": "items[0].v", "decl_version": "v3"},
        {"source": "a.b", "target": "items[0].v", "decl_version": "v1"},
    ]
    snapshot, diagnostics = parse_snapshot(raw)
    assert len(diagnostics) == 1
    assert diagnostics[0].code == E_VERSION
    assert diagnostics[0].entry_seq == 1
    # The valid v1 declaration is retained and is non-transitive.
    assert len(snapshot.derivations) == 1
    assert snapshot.derivations[0].transitive is False


def test_v2_transitive_marker_is_honoured():
    raw = _base()
    raw["derivations"] = [
        {"source": "a.b", "target": "items[0].v",
         "decl_version": "v2", "transitive": True},
        {"source": "a.b", "target": "items[0].v",
         "decl_version": "v2", "transitive": False},
    ]
    snapshot, _ = parse_snapshot(raw)
    assert snapshot.derivations[0].transitive is True
    assert snapshot.derivations[1].transitive is False
