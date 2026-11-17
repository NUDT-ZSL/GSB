import copy

from sensitrace.lineage import LineageEngine, node_id
from sensitrace.models import Snapshot
from sensitrace.parser import parse_snapshot


def _snapshot(raw):
    snapshot, diagnostics = parse_snapshot(raw)
    assert diagnostics == []
    return snapshot


def _engine(documents):
    snapshots = [_snapshot(raw) for raw in documents]
    engine = LineageEngine()
    engine.ingest_all(snapshots)
    return engine


def test_v1_direct_derivation_does_not_propagate_transitively():
    documents = [
        {
            "doc_id": "d",
            "version": "1.0.0",
            "data": {"src": 1, "mid": 2, "sink": 3},
            "fields": [
                {"path": "src", "sensitivity": "sensitive"},
                {"path": "mid", "sensitivity": "masked"},
                {"path": "sink", "sensitivity": "plain"},
            ],
            "derivations": [
                {"source": "src", "target": "mid", "decl_version": "v1"},
                {"source": "mid", "target": "sink", "decl_version": "v1"},
            ],
        }
    ]
    engine = _engine(documents)
    # Direct edge still raises the effective level of mid.
    assert engine.nodes[node_id("1.0.0", "mid")].effective == "sensitive"
    # v1 chain stops: sink only sees mid's *declared* masked level, never the
    # sensitive level of src.
    assert engine.nodes[node_id("1.0.0", "sink")].effective == "masked"


def test_v2_transitive_chain_carries_sensitivity_to_closure():
    documents = [
        {
            "doc_id": "d",
            "version": "1.0.0",
            "data": {"src": 1, "mid": 2, "sink": 3},
            "fields": [
                {"path": "src", "sensitivity": "sensitive"},
                {"path": "mid", "sensitivity": "masked"},
                {"path": "sink", "sensitivity": "plain"},
            ],
            "derivations": [
                {"source": "src", "target": "mid",
                 "decl_version": "v2", "transitive": True},
                {"source": "mid", "target": "sink",
                 "decl_version": "v2", "transitive": True},
            ],
        }
    ]
    engine = _engine(documents)
    assert engine.nodes[node_id("1.0.0", "sink")].effective == "sensitive"
    closure = engine.transitive_closure()
    assert node_id("1.0.0", "sink") in closure[node_id("1.0.0", "src")]


def test_mixed_v1_v2_in_one_document_are_dispatched_per_declaration():
    documents = [
        {
            "doc_id": "d",
            "version": "1.0.0",
            "data": {"s": 1, "a": 2, "b": 3, "c": 4},
            "fields": [
                {"path": "s", "sensitivity": "sensitive"},
                {"path": "a", "sensitivity": "masked"},
                {"path": "b", "sensitivity": "masked"},
                {"path": "c", "sensitivity": "plain"},
            ],
            "derivations": [
                {"source": "s", "target": "a", "decl_version": "v1"},
                {"source": "a", "target": "c", "decl_version": "v1"},
                {"source": "s", "target": "b",
                 "decl_version": "v2", "transitive": True},
                {"source": "b", "target": "c",
                 "decl_version": "v2", "transitive": True},
            ],
        }
    ]
    engine = _engine(documents)
    # c receives sensitive via the v2 transitive branch s->b->c only.
    assert engine.nodes[node_id("1.0.0", "c")].effective == "sensitive"
    v1_edges = [e for e in engine.edges if e.decl_version == "v1"]
    assert all(e.transitive is False for e in v1_edges)


def test_addressing_format_change_keeps_one_field_identity_across_versions():
    documents = [
        {
            "doc_id": "d",
            "version": "1.0.0",
            "data": {"payments": [{"card_no": "x"}]},
            "fields": [
                {"path": "payments[0].card_no", "sensitivity": "sensitive"},
            ],
            "derivations": [],
        },
        {
            "doc_id": "d",
            "version": "2.0.0",
            # Same logical field; canonical key must remain identical.
            "data": {"payments": [{"card_no": "y"}]},
            "fields": [
                {"path": "payments[0].card_no", "sensitivity": "masked"},
            ],
            "derivations": [
                {"source": "payments[0].card_no", "target": "payments[0].card_no",
                 "decl_version": "v1"},
            ],
        },
    ]
    engine = _engine(documents)
    stat_v2 = next(s for s in engine.version_stats if s.version == "2.0.0")
    assert stat_v2.added == []  # not treated as a new field
    continuity = [e for e in engine.edges if e.kind == "continuity"]
    assert len(continuity) == 1
    # Sensitivity flows across versions even though the snapshot marking changed.
    assert engine.nodes[node_id("2.0.0", "payments[0].card_no")].effective == "sensitive"


def test_results_are_independent_of_declaration_order():
    base = {
        "doc_id": "d",
        "version": "1.0.0",
        "data": {"s": 1, "a": 2, "b": 3},
        "fields": [
            {"path": "s", "sensitivity": "sensitive"},
            {"path": "a", "sensitivity": "masked"},
            {"path": "b", "sensitivity": "plain"},
        ],
        "derivations": [
            {"source": "s", "target": "a", "decl_version": "v1"},
            {"source": "s", "target": "b",
             "decl_version": "v2", "transitive": True},
            {"source": "a", "target": "b", "decl_version": "v1"},
        ],
    }
    shuffled = copy.deepcopy(base)
    shuffled["derivations"] = list(reversed(shuffled["derivations"]))
    first = _engine([base]).to_dict()
    second = _engine([shuffled]).to_dict()
    assert first["nodes"] == second["nodes"]
    assert first["edges"] == second["edges"]
    assert first["diagnostics"] == second["diagnostics"]
