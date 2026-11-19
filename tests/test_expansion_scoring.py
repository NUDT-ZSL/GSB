from __future__ import annotations

import json

import pytest

from qrw.canonical import canonical_json
from qrw.compiler import compile_templates
from qrw.errors import BusinessError
from qrw.expansion import expand_query
from qrw.readers import load_slot_types, read_template_file, read_template_text
from qrw.scoring import rank_candidates, recompute_score


def test_expansion_is_deterministic_and_offsets_point_to_query(
    compiled_core, queries, config
):
    query = next(item for item in queries if item["query_id"] == "q001")
    first = expand_query(compiled_core["v2"], query, config)
    second = expand_query(compiled_core["v2"], query, config)
    assert canonical_json(first) == canonical_json(second)
    candidate = first[0]
    for span in candidate["offset_map"]:
        if span["kind"] == "slot":
            expected = query["query_text"][span["query_start"] : span["query_end"]]
            actual = candidate["candidate"][span["output_start"] : span["output_end"]]
            assert expected == actual
    assert candidate["score_detail"]["score"] == recompute_score(candidate["score_detail"])
    assert candidate["rule_chain"]


def test_depth_overflow_is_config_invalid(samples, queries, config):
    slot_types = load_slot_types(samples / "anomalies" / "slot_types.json")
    parsed = read_template_file(samples / "anomalies" / "over_depth.json", slot_types, config)
    compiled = compile_templates(parsed, config)
    with pytest.raises(BusinessError) as exc:
        expand_query(
            compiled,
            {
                "query_id": "depth",
                "query_text": "上海",
                "locale": "zh_CN",
                "template_set_version": "2",
            },
            config,
        )
    assert exc.value.code == "E_CONFIG_INVALID"
    assert exc.value.field == "rules.deep_root.max_depth"
    assert exc.value.details["reason"] == "expansion_depth_exceeded"


def test_rank_tie_breaks_by_source_line_then_candidate():
    def candidate(text: str, line: int, rule_id: str):
        detail = {
            "weight": 1.0,
            "slot_hits": 0,
            "slot_points": 0.0,
            "expansion_depth": 1,
            "depth_penalty": 0.0,
            "score": 1.0,
            "formula": "weight + slot_points - depth_penalty",
        }
        return {
            "candidate": text,
            "rule_id": rule_id,
            "candidate_id": rule_id,
            "source_line": line,
            "score_detail": detail,
        }

    ranked = rank_candidates(
        [
            candidate("zeta", 5, "z"),
            candidate("beta", 5, "b"),
            candidate("alpha", 7, "a"),
        ]
    )
    assert [item["candidate"] for item in ranked] == ["beta", "zeta", "alpha"]


def test_unicode_offsets_are_codepoint_based(slot_types, config):
    doc = {
        "version": "V3",
        "rules": [
            {
                "id": "R",
                "priority": 1,
                "weight": 1,
                "enabled": True,
                "max_depth": 1,
                "pattern": "{city} 酒店",
                "slots": [{"name": "city", "type": "GEO"}],
                "replacement": [{"text": "推荐 "}, {"slot": "city"}],
            }
        ],
    }
    parsed = read_template_text(json.dumps(doc), "<unicode>", slot_types, config)
    compiled = compile_templates(parsed, config)
    candidates = expand_query(
        compiled,
        {
            "query_id": "unicode",
            "query_text": "上海 酒店",
            "locale": "zh_CN",
            "template_set_version": "2",
        },
        config,
    )
    assert candidates[0]["candidate"] == "推荐 上海"
    slot_span = [item for item in candidates[0]["offset_map"] if item["kind"] == "slot"][0]
    assert (slot_span["query_start"], slot_span["query_end"]) == (0, 2)
    assert (slot_span["output_start"], slot_span["output_end"]) == (3, 5)

