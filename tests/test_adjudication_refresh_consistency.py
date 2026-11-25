from __future__ import annotations

from transcript_governance.adjudicate import adjudicate, choose_winner
from transcript_governance.codes import Code
from transcript_governance.consistency import TimelineIndex, check_consistency
from transcript_governance.models import Utterance, Word
from transcript_governance.refresh import (
    AdjudicationRefreshService,
    RefreshEngine,
    plan_v2_revision,
    versioned_nodes,
)


def u(id_: str, start: int, end: int, text: str, confidence: float, revision: int = 1) -> Utterance:
    return Utterance(id_, start, end, text, confidence=confidence, revision=revision)


def test_priority_confidence_then_revision_then_lexical_id() -> None:
    high_conf_v1 = u("A", 0, 100, "one", 0.95, 1)
    low_conf_v2 = u("A", 0, 100, "two", 0.80, 9)
    winner, applied, _ = choose_winner(high_conf_v1, low_conf_v2)
    assert winner is high_conf_v1 and applied == "confidence"

    low_rev_v1 = u("A", 0, 100, "one", 0.9, 1)
    high_rev_v2 = u("A", 0, 100, "two", 0.9, 4)
    winner, applied, _ = choose_winner(low_rev_v1, high_rev_v2)
    assert winner is high_rev_v2 and applied == "revision"

    lexical_v1 = u("A-lex", 0, 100, "one", 0.9, 2)
    lexical_v2 = u("B-lex", 0, 100, "two", 0.9, 2)
    winner, applied, _ = choose_winner(lexical_v1, lexical_v2)
    assert winner is lexical_v1 and applied == "utterance_id"


def test_adjudication_outputs_three_overlap_conclusions() -> None:
    v1 = [
        u("same", 0, 100, "same text", 0.9),
        u("conf", 100, 200, "old words", 0.9),
        u("partial", 300, 500, "left only phrase", 0.9),
    ]
    v2 = [
        u("same", 0, 100, "same text", 0.8),
        u("conf", 100, 200, "new words", 0.95),
        u("partial", 450, 650, "right only phrase", 0.9),
    ]
    report = adjudicate(v1, v2)
    by_id = {(p.v1_id, p.v2_id): p.decision.conclusion for p in report.pairs}
    assert by_id[("same", "same")] == "covered"
    assert by_id[("conf", "conf")] == "conflict"
    assert by_id[("partial", "partial")] == "supplement"


def test_consistency_detects_overlap_and_gap_and_point_query() -> None:
    items = [u("A", 0, 100, "a", 1.0), u("B", 100, 200, "b", 1.0)]
    ok_report = check_consistency(items, declared_regions=[(0, 200)])
    assert ok_report.ok and ok_report.code == Code.OK

    overlap = check_consistency(items + [u("C", 90, 120, "c", 1.0)], declared_regions=[(0, 200)])
    assert overlap.code == Code.E_OVERLAP

    gap = check_consistency([items[0]], declared_regions=[(0, 200)])
    assert gap.code == Code.E_GAP

    index = TimelineIndex(items, declared_regions=[(0, 200)], cache_keys={"A": "key-a"})
    assert index.at(99).utterance.utterance_id == "A"
    assert index.at(100).utterance.utterance_id == "B"
    assert index.at(200).code == Code.E_GAP
    assert index.at(50).cache_key == "key-a"


def test_refresh_only_invalidates_overlap_word_trigger_and_dependents() -> None:
    old = u("ROOT", 0, 100, "old", 0.9)
    new = Utterance(
        "ROOT",
        0,
        100,
        "new",
        0.9,
        words=(Word("move", 40, 160),),
    )
    overlap = u("OV", 80, 180, "overlap", 0.9)
    word_target = u("WORD", 150, 220, "word target", 0.9)
    unaffected = u("FAR", 300, 400, "far", 0.9)
    nodes = versioned_nodes([overlap, word_target, unaffected], [old])
    dependencies = {"artifact:downstream": {"v2:ROOT"}}

    plan = plan_v2_revision(old, new, nodes, dependencies=dependencies)
    assert "v2:ROOT" in plan.invalidated_nodes
    assert "v1:OV" in plan.invalidated_nodes
    assert "v1:WORD" in plan.invalidated_nodes
    assert "v1:FAR" in plan.reused_nodes
    assert "artifact:downstream" in plan.invalidated_nodes
    assert any(trigger.kind == "word_time" for trigger in plan.triggers)

    engine = RefreshEngine({"v1:FAR": "committed"})
    result = engine.run(plan, lambda key: f"computed:{key}")
    assert result.results["v1:FAR"] == "committed"
    assert all(key in result.results for key in plan.invalidated_nodes)


def test_incremental_adjudication_matches_full_recompute_ms_by_ms() -> None:
    v1 = [u("A", 0, 100, "old alpha", 0.95), u("B", 200, 300, "keep", 0.8)]
    v2 = [
        Utterance("A", 0, 100, "alpha", 0.8, revision=1, words=(Word("alpha", 10, 90),)),
        u("B", 200, 300, "keep", 0.9),
    ]
    service = AdjudicationRefreshService(v1, v2, region=(0, 300))
    plan, incremental, consistent = service.revise_v2(
        "A",
        text="completely revised alpha",
        words=(Word("completely", 5, 40), Word("revised", 41, 95)),
        revision=2,
    )

    assert consistent
    assert plan.invalidated_nodes == ["v1:A", "v2:A"]
    assert service.last_recomputed_pair_keys == ["A__x__A"]
    assert service.last_reused_pair_keys == ["B__x__B"]

    incremental_state, full_state = service.millisecond_states()
    assert incremental_state == full_state
    assert len(incremental_state) == 300


def test_timeline_fingerprint_is_stable() -> None:
    items = [u("A", 0, 10, "a", 1.0), u("B", 10, 20, "b", 1.0)]
    first = TimelineIndex(items).millisecond_fingerprint((0, 20))
    second = TimelineIndex(list(reversed(items))).millisecond_fingerprint((0, 20))
    assert first == second
