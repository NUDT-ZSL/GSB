import json

import pytest

from recovery_engine.errors import BizError, ErrCode
from recovery_engine.v1_loader import load_v1
from tests.conftest import V1_PATH


def write(tmp_path, payload):
    path = tmp_path / "v1.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def base_payload():
    return {
        "format": "recovery-v1",
        "logical_clock_ms": 5000,
        "services": [
            {"name": "a", "failure_domain": "fd-1", "fallback_seconds": 3,
             "actions": [{"id": "a1", "kind": "stop", "at_tick": 1},
                         {"id": "a2", "kind": "start", "at_tick": 2}]},
        ],
        "service_actions": {"a": ["a1", "a2"]},
    }


def test_load_valid():
    cfg = load_v1(V1_PATH)
    assert cfg["config_clock_ms"] == 5000
    assert set(cfg["services"]) == {"web", "api", "legacy", "tie"}
    web = cfg["services"]["web"]
    assert web.fallback_seconds == 30
    assert [a.action_id for a in web.actions] == ["web-stop", "web-switch"]
    assert all(a.source_version == "v1" for a in web.actions)


def test_duplicate_service_name(tmp_path):
    payload = base_payload()
    payload["services"].append(dict(payload["services"][0]))
    payload["service_actions"] = {"a": ["a1", "a2"]}
    with pytest.raises(BizError) as exc:
        load_v1(write(tmp_path, payload))
    assert exc.value.code == ErrCode.E_BAD_CONFIG
    assert exc.value.details["service"] == "a"


def test_negative_fallback_seconds(tmp_path):
    payload = base_payload()
    payload["services"][0]["fallback_seconds"] = -1
    with pytest.raises(BizError) as exc:
        load_v1(write(tmp_path, payload))
    assert exc.value.code == ErrCode.E_BAD_CONFIG


def test_non_integer_fallback_seconds(tmp_path):
    payload = base_payload()
    payload["services"][0]["fallback_seconds"] = 1.5
    with pytest.raises(BizError) as exc:
        load_v1(write(tmp_path, payload))
    assert exc.value.code == ErrCode.E_BAD_CONFIG


def test_actions_must_be_non_decreasing(tmp_path):
    payload = base_payload()
    payload["services"][0]["actions"][1]["at_tick"] = 0
    with pytest.raises(BizError) as exc:
        load_v1(write(tmp_path, payload))
    assert exc.value.code == ErrCode.E_BAD_CONFIG
    assert exc.value.details["action"] == "a2"


def test_flat_mapping_must_match_declarations(tmp_path):
    payload = base_payload()
    payload["service_actions"] = {"a": ["a1"]}
    with pytest.raises(BizError) as exc:
        load_v1(write(tmp_path, payload))
    assert exc.value.code == ErrCode.E_BAD_CONFIG


def test_mapping_service_set_must_match(tmp_path):
    payload = base_payload()
    payload["service_actions"] = {"a": ["a1", "a2"], "ghost": []}
    with pytest.raises(BizError) as exc:
        load_v1(write(tmp_path, payload))
    assert exc.value.code == ErrCode.E_BAD_CONFIG
