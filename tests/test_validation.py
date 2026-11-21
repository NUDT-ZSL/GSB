from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from batchdiag.errors import BusinessError
from batchdiag.jsonutil import read_json
from batchdiag.spec import validate_workspace

from conftest import SAMPLES, cli_json


def _values():
    return (
        read_json(SAMPLES / "model.json"),
        read_json(SAMPLES / "replicas.json"),
        read_json(SAMPLES / "requests.json"),
        read_json(SAMPLES / "precision.json"),
    )


def _expect(code: str, model=None, replicas=None, requests=None, config=None):
    m, r, q, c = _values()
    with pytest.raises(BusinessError) as caught:
        validate_workspace(
            m if model is None else model,
            r if replicas is None else replicas,
            q if requests is None else requests,
            c if config is None else config,
        )
    assert caught.value.code == code


def test_duplicate_replica_code():
    _, replicas, _, _ = _values()
    replicas = copy.deepcopy(replicas)
    replicas["replicas"][1]["replica_id"] = replicas["replicas"][0]["replica_id"]
    _expect("E_DUPLICATE_REPLICA", replicas=replicas)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("compute_units", 0, "E_REPLICA_COMPUTE"),
        ("compute_units", float("inf"), "E_REPLICA_COMPUTE"),
        ("memory_limit_mib", 0, "E_REPLICA_MEMORY"),
        ("max_batch_size", 1.5, "E_REPLICA_MAX_BATCH"),
        ("dispatch_quota", 0, "E_REPLICA_QUOTA"),
    ],
)
def test_replica_numeric_codes(field, value, code):
    _, replicas, _, _ = _values()
    replicas = copy.deepcopy(replicas)
    replicas["replicas"][0][field] = value
    _expect(code, replicas=replicas)


def test_sequence_limit_and_request_codes():
    _, replicas, requests, _ = _values()
    replicas = copy.deepcopy(replicas)
    replicas["replicas"][0]["max_sequence_length"] = 10
    _expect("E_SEQUENCE_LIMIT", replicas=replicas)

    requests = copy.deepcopy(requests)
    requests["requests"][1]["request_id"] = requests["requests"][0]["request_id"]
    _expect("E_DUPLICATE_REQUEST", requests=requests)

    _, _, requests, _ = _values()
    requests["requests"][0]["arrival_ms"] = 5
    requests["requests"][0]["deadline_ms"] = 4
    _expect("E_REQUEST_DEADLINE", requests=requests)

    _, _, requests, _ = _values()
    requests["requests"][0]["service_class"] = "platinum"
    _expect("E_SERVICE_CLASS", requests=requests)


@pytest.mark.parametrize("scale", [0, 3, 12, 10**13])
def test_scale_must_be_power_of_ten(scale):
    _, _, _, config = _values()
    config = copy.deepcopy(config)
    config["fixed_point_scales"]["compute"] = scale
    _expect("E_SCALE", config=config)


def test_zero_tolerance_and_zero_sample_ratio_are_valid():
    model, replicas, requests, config = _values()
    config["float_tolerance"] = 0
    config["diagnostic_sample_ratio"] = 0
    config["fixed_point_scales"]["compute"] = "1000000"
    validate_workspace(model, replicas, requests, config)


def test_invalid_tolerance_and_sample_ratio_codes():
    _, _, _, config = _values()
    bad_tolerance = copy.deepcopy(config)
    bad_tolerance["float_tolerance"] = -1
    _expect("E_TOLERANCE", config=bad_tolerance)

    bad_ratio = copy.deepcopy(config)
    bad_ratio["diagnostic_sample_ratio"] = 1.1
    _expect("E_SAMPLE_RATIO", config=bad_ratio)


def test_invalid_load_writes_no_database(tmp_path: Path):
    model, replicas, requests, config = _values()
    model_path = tmp_path / "model.json"
    replicas_path = tmp_path / "replicas.json"
    requests_path = tmp_path / "requests.json"
    config_path = tmp_path / "config.json"
    model_path.write_text(json.dumps(model), encoding="utf-8")
    replicas_path.write_text(json.dumps(replicas), encoding="utf-8")
    requests["requests"][0]["service_class"] = "invalid"
    requests_path.write_text(json.dumps(requests), encoding="utf-8")
    config_path.write_text(json.dumps(config), encoding="utf-8")
    db = tmp_path / "must-not-exist.sqlite"
    code, payload = cli_json(
        "load",
        "--model",
        str(model_path),
        "--replicas",
        str(replicas_path),
        "--requests",
        str(requests_path),
        "--config",
        str(config_path),
        "--db",
        str(db),
    )
    assert code == 2
    assert payload["code"] == "E_SERVICE_CLASS"
    assert not db.exists()
    assert not Path(str(db) + "-wal").exists()


def test_nonfinite_json_value_returns_business_code(tmp_path: Path):
    model, replicas, requests, config = _values()
    replicas["replicas"][0]["compute_units"] = float("nan")
    paths = {}
    for name, value in (
        ("model", model),
        ("replicas", replicas),
        ("requests", requests),
        ("config", config),
    ):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        paths[name] = path
    code, payload = cli_json(
        "load",
        "--model",
        str(paths["model"]),
        "--replicas",
        str(paths["replicas"]),
        "--requests",
        str(paths["requests"]),
        "--config",
        str(paths["config"]),
        "--db",
        str(tmp_path / "state.sqlite"),
    )
    assert code == 2
    assert payload["code"] == "E_REPLICA_COMPUTE"
    assert not (tmp_path / "state.sqlite").exists()


def test_invalid_force_load_preserves_existing_database(tmp_path: Path):
    model, replicas, requests, config = _values()
    paths = {}
    for name, value in (
        ("model", model),
        ("replicas", replicas),
        ("requests", requests),
        ("config", config),
    ):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        paths[name] = path
    db = tmp_path / "existing.sqlite"
    code, payload = cli_json(
        "load",
        "--model",
        str(paths["model"]),
        "--replicas",
        str(paths["replicas"]),
        "--requests",
        str(paths["requests"]),
        "--config",
        str(paths["config"]),
        "--db",
        str(db),
    )
    assert code == 0, payload
    original = db.read_bytes()
    bad_requests = copy.deepcopy(requests)
    bad_requests["requests"][0]["service_class"] = "invalid"
    paths["requests"].write_text(json.dumps(bad_requests), encoding="utf-8")
    code, payload = cli_json(
        "load",
        "--model",
        str(paths["model"]),
        "--replicas",
        str(paths["replicas"]),
        "--requests",
        str(paths["requests"]),
        "--config",
        str(paths["config"]),
        "--db",
        str(db),
        "--force",
    )
    assert code == 2
    assert payload["code"] == "E_SERVICE_CLASS"
    assert db.read_bytes() == original
