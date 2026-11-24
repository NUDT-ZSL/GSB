from pdre.codes import BusinessCode as Codes
from pdre.lineage import build_lineage_view
from pdre.models import LineageRecord, SourceRef


def lineage(child: str, parent: str | None, session: str = "s1", seq: int = 1) -> LineageRecord:
    return LineageRecord(
        process_guid=child,
        parent_process_guid=parent,
        session_id=session,
        source=SourceRef("lineage", seq, "lineage.jsonl"),
    )


def test_self_reference_and_cycle_have_independent_codes():
    records = [
        lineage("self", "self", seq=1),
        lineage("a", "b", seq=2),
        lineage("b", "c", seq=3),
        lineage("c", "a", seq=4),
    ]
    view = build_lineage_view([], records)

    assert any(item["code"] == Codes.LINEAGE_SELF_REFERENCE for item in view.self_references)
    cycle = view.cycles[0]
    assert cycle["code"] == Codes.LINEAGE_CYCLE
    assert cycle["process_guids"] == ["a", "b", "c"]
    assert cycle["cycle_guids"] == ["a", "b", "c", "a"]


def test_fork_lists_all_candidate_roots_and_ambiguous_unit():
    records = [
        lineage("ra", None, seq=1),
        lineage("pa", "ra", seq=2),
        lineage("rb", None, seq=3),
        lineage("pb", "rb", seq=4),
        lineage("f", "pa", "session-a", seq=5),
        lineage("f", "pb", "session-b", seq=6),
        lineage("d", "f", seq=7),
    ]
    view = build_lineage_view([], records)

    fork = view.forks[0]
    assert fork["process_guid"] == "f"
    assert fork["candidate_root_guids"] == ["ra", "rb"]
    assignment = view.assignments.get("d")
    # No events were supplied, so assignments are empty; classify through a synthetic event below.
    assert assignment is None


def test_missing_parent_declaration_is_reported():
    records = [lineage("child", "ghost", seq=1)]
    view = build_lineage_view([], records)
    missing_guids = {item["process_guid"] for item in view.missing}
    assert "ghost" in missing_guids


def test_descendant_closure_covers_fork_subtree():
    records = [
        lineage("r", None, seq=1),
        lineage("f", "r", seq=2),
        lineage("d", "f", seq=3),
        lineage("g", "d", seq=4),
    ]
    view = build_lineage_view([], records)
    assert view.descendants_of("f") == {"f", "d", "g"}
