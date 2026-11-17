from __future__ import annotations

import json

import pytest

from ml_gateway.declarations import load_declarations, load_requests_ndjson
from ml_gateway.errors import InputError, RuleError, VectorError

from tests.conftest import DATA_DIR, write_variant_declarations


def test_sample_declarations_and_requests_load(sample_bundle):
    requests = load_requests_ndjson(DATA_DIR / "requests.ndjson", sample_bundle.feature_schema)
    assert len(requests) == 8
    assert [request.request_id for request in requests][:3] == [
        "req-001",
        "req-002",
        "req-003",
    ]


@pytest.mark.parametrize("bad_port", [0, 70000, True, "24311"])
def test_invalid_port_is_e_rule(tmp_path, bad_port):
    def mutate(backends):
        backends["backends"][0]["port"] = bad_port

    backend_path, rules_path = write_variant_declarations(tmp_path, mutate_backends=mutate)
    with pytest.raises(RuleError) as exc:
        load_declarations(backend_path, rules_path)
    assert exc.value.code == "E_RULE"
    assert exc.value.path == "$.backends[0].port"


def test_duplicate_model_id_is_e_rule(tmp_path):
    def mutate(backends):
        backends["backends"][1]["model_id"] = backends["backends"][0]["model_id"]

    backend_path, rules_path = write_variant_declarations(tmp_path, mutate_backends=mutate)
    with pytest.raises(RuleError) as exc:
        load_declarations(backend_path, rules_path)
    assert exc.value.path == "$.backends[1].model_id"


def test_duplicate_rule_id_is_e_rule(tmp_path):
    def mutate(rules):
        rules["rules"][1]["rule_id"] = rules["rules"][0]["rule_id"]

    backend_path, rules_path = write_variant_declarations(tmp_path, mutate_rules=mutate)
    with pytest.raises(RuleError) as exc:
        load_declarations(backend_path, rules_path)
    assert exc.value.path == "$.rules[1].rule_id"


def test_missing_target_backend_is_e_rule(tmp_path):
    def mutate(rules):
        rules["rules"][0]["target_backend"] = "does-not-exist"

    backend_path, rules_path = write_variant_declarations(tmp_path, mutate_rules=mutate)
    with pytest.raises(RuleError) as exc:
        load_declarations(backend_path, rules_path)
    assert exc.value.path == "$.rules[0].target_backend"


def test_unknown_match_field_is_e_rule(tmp_path):
    def mutate(rules):
        rules["rules"][0]["match"]["unknown_feature"] = "x"

    backend_path, rules_path = write_variant_declarations(tmp_path, mutate_rules=mutate)
    with pytest.raises(RuleError) as exc:
        load_declarations(backend_path, rules_path)
    assert exc.value.path == "$.rules[0].match.unknown_feature"


def test_batch_size_above_backend_concurrency_is_e_rule(tmp_path):
    def mutate(rules):
        rules["rules"][0]["batch_size"] = 5

    backend_path, rules_path = write_variant_declarations(tmp_path, mutate_rules=mutate)
    with pytest.raises(RuleError) as exc:
        load_declarations(backend_path, rules_path)
    assert exc.value.path == "$.rules[0].batch_size"


def test_parameter_vector_length_mismatch_is_e_vector(tmp_path):
    (tmp_path / "short.json").write_text("[1.0,2.0,3.0]\n", encoding="utf-8")

    def mutate(backends):
        backends["backends"] = [backends["backends"][0]]
        backends["backends"][0]["parameter_file"] = "short.json"

    backend_path, rules_path = write_variant_declarations(tmp_path, mutate_backends=mutate)
    with pytest.raises(VectorError) as exc:
        load_declarations(backend_path, rules_path)
    assert exc.value.code == "E_VECTOR"
    assert exc.value.path == "$.backends[0].parameter_file"


def test_nonfinite_parameter_is_e_vector(tmp_path):
    (tmp_path / "bad.json").write_text("[1.0, NaN, 3.0, 4.0]\n", encoding="utf-8")

    def mutate(backends):
        backends["backends"] = [backends["backends"][0]]
        backends["backends"][0]["parameter_file"] = "bad.json"

    backend_path, rules_path = write_variant_declarations(tmp_path, mutate_backends=mutate)
    with pytest.raises(VectorError) as exc:
        load_declarations(backend_path, rules_path)
    assert exc.value.path == "$.backends[0].parameter_file[1]"


def test_request_parser_preserves_input_length_for_engine_validation(tmp_path, sample_bundle):
    requests = load_requests_ndjson(DATA_DIR / "requests.ndjson", sample_bundle.feature_schema)
    broken = json.loads(json.dumps(requests[0].to_dict()))
    broken["input"] = [1.0, 2.0, 3.0]
    path = tmp_path / "request.ndjson"
    path.write_text(json.dumps(broken) + "\n", encoding="utf-8")
    parsed = load_requests_ndjson(path, sample_bundle.feature_schema)
    # Length is backend-dependent and therefore validated by the engine.
    assert len(parsed[0].inputs) == 3
