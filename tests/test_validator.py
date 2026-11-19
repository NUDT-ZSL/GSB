from __future__ import annotations

import copy

from qrw.validator import verify


def _first_rule(compiled):
    return next(rule for rule in compiled["rules"] if rule["emit"])


def _first_text_part(rule):
    return next(part for part in rule["replacement"]["parts"] if part["kind"] == "text")


def test_verifier_accepts_core(compiled_core, queries, config):
    report = verify(compiled_core, queries, config)
    assert report["ok"] is True
    assert report["checks"]["candidate_set_equal"] is True
    assert report["checks"]["fill_traceability"] is True


def test_verifier_reports_first_candidate_difference(compiled_core, queries, config):
    tampered = copy.deepcopy(compiled_core)
    rule = _first_rule(tampered["v1"])
    part = _first_text_part(rule)
    part["text"] = part["text"] + "偏"
    report = verify(tampered, queries, config)
    assert report["ok"] is False
    assert report["code"] == "E_INCONSISTENT"
    assert report["error"]["details"]["inconsistent_category"] == "candidate_set"
    position = report["error"]["details"]["first_difference_position"]
    assert position["candidate_index"] >= 0
    assert position["field"] == "candidate"
    assert position["codepoint_offset"] >= 0


def test_verifier_reports_fill_traceability(compiled_core, queries, config):
    tampered = copy.deepcopy(compiled_core)
    tampered["v1"]["fills"] = []
    report = verify(tampered, queries, config)
    assert report["code"] == "E_INCONSISTENT"
    assert report["error"]["details"]["inconsistent_category"] == "fill_traceability"


def test_verifier_reports_score_detail_tamper(
    compiled_core, queries, config, monkeypatch
):
    import qrw.validator as validator_module

    original = validator_module.expand_query

    def tampered_expand(compiled, query, compiler_config):
        candidates = copy.deepcopy(original(compiled, query, compiler_config))
        if compiled["dialect"] == "v2" and candidates:
            candidates[0]["score_detail"]["score"] = 999.0
        return candidates

    monkeypatch.setattr(validator_module, "expand_query", tampered_expand)
    report = verify(compiled_core, queries, config)
    assert report["code"] == "E_INCONSISTENT"
    assert report["error"]["details"]["inconsistent_category"] == "score_detail"

