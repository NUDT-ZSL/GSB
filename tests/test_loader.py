from __future__ import annotations

import json
from pathlib import Path

import pytest

from coldstart_ledger.cli import main
from coldstart_ledger.errors import BusinessError, ErrorCode
from coldstart_ledger.loader import load_inputs

from conftest import write_bundle


def test_sample_bundle_loads_and_sorts(sample_bundle) -> None:
    assert [item.instance_id for item in sample_bundle.instances] == [
        "api-batch",
        "api-fast",
        "edge-one",
        "edge-two",
        "worker-late",
        "worker-low",
    ]
    assert sample_bundle.instances_by_id["api-fast"].required_layers == (
        "sha256:layer-api",
        "sha256:layer-base",
    )
    assert sample_bundle.config.fixed_scale == 1000


def test_duplicate_instance_returns_business_code(tmp_path: Path) -> None:
    instances = [
        {
            "instance_id": "same",
            "service": "svc",
            "priority": 1,
            "requested_ephemeral_bytes": 10,
            "required_layers": ["sha256:l1"],
            "deadline_ms": 10,
        },
        {
            "instance_id": "same",
            "service": "svc",
            "priority": 2,
            "requested_ephemeral_bytes": 10,
            "required_layers": ["sha256:l1"],
            "deadline_ms": 10,
        },
    ]
    with pytest.raises(BusinessError) as caught:
        write_bundle(tmp_path, instances=instances)
    assert caught.value.code == ErrorCode.DUPLICATE_INSTANCE


def test_duplicate_layer_returns_business_code(tmp_path: Path) -> None:
    layers = [
        {"digest": "same", "bytes": 1, "node": "node-a", "ref_count": 1},
        {"digest": "same", "bytes": 2, "node": "node-a", "ref_count": 1},
    ]
    with pytest.raises(BusinessError) as caught:
        write_bundle(tmp_path, layers=layers)
    assert caught.value.code == ErrorCode.DUPLICATE_LAYER


def test_missing_layer_returns_business_code(tmp_path: Path) -> None:
    layers = [{"digest": "sha256:l1", "bytes": 1, "node": "node-a", "ref_count": 1}]
    instances = [
        {
            "instance_id": "i1",
            "service": "svc",
            "priority": 1,
            "requested_ephemeral_bytes": 1,
            "required_layers": ["sha256:missing"],
            "deadline_ms": 1,
        }
    ]
    with pytest.raises(BusinessError) as caught:
        write_bundle(tmp_path, instances=instances, layers=layers)
    assert caught.value.code == ErrorCode.LAYER_MISSING


def test_positive_quantity_validation(tmp_path: Path) -> None:
    layers = [{"digest": "sha256:l1", "bytes": 0, "node": "node-a", "ref_count": 1}]
    with pytest.raises(BusinessError) as caught:
        write_bundle(tmp_path, layers=layers)
    assert caught.value.code == ErrorCode.INVALID_QUANTITY
    assert caught.value.details["field"] == "bytes"


def test_node_and_config_positive_validation(tmp_path: Path) -> None:
    nodes = [
        {
            "node": "node-a",
            "available_ephemeral_bytes": 0,
            "image_layer_bytes": 1000,
            "max_concurrent_cold_starts": 1,
        }
    ]
    with pytest.raises(BusinessError) as node_error:
        write_bundle(tmp_path / "nodes", nodes=nodes)
    assert node_error.value.code == ErrorCode.INVALID_QUANTITY

    with pytest.raises(BusinessError) as config_error:
        write_bundle(tmp_path / "config", config={"fixed_scale": 0})
    assert config_error.value.code == ErrorCode.INVALID_QUANTITY


def test_integer_validation_rejects_bool(tmp_path: Path) -> None:
    layers = [{"digest": "sha256:l1", "bytes": True, "node": "node-a", "ref_count": 1}]
    with pytest.raises(BusinessError) as caught:
        write_bundle(tmp_path, layers=layers)
    assert caught.value.code == ErrorCode.INVALID_INTEGER


def test_multi_node_required_layers_are_rejected(tmp_path: Path) -> None:
    layers = [
        {"digest": "sha256:l1", "bytes": 1, "node": "node-a", "ref_count": 1},
        {"digest": "sha256:l2", "bytes": 1, "node": "node-b", "ref_count": 1},
    ]
    nodes = [
        {
            "node": "node-a",
            "available_ephemeral_bytes": 10,
            "image_layer_bytes": 10,
            "max_concurrent_cold_starts": 1,
        },
        {
            "node": "node-b",
            "available_ephemeral_bytes": 10,
            "image_layer_bytes": 10,
            "max_concurrent_cold_starts": 1,
        },
    ]
    instances = [
        {
            "instance_id": "i1",
            "service": "svc",
            "priority": 1,
            "requested_ephemeral_bytes": 1,
            "required_layers": ["sha256:l1", "sha256:l2"],
            "deadline_ms": 1,
        }
    ]
    with pytest.raises(BusinessError) as caught:
        write_bundle(tmp_path, instances=instances, layers=layers, nodes=nodes)
    assert caught.value.code == ErrorCode.INSTANCE_NODE_AMBIGUOUS


def test_invalid_json_cli_does_not_create_database(tmp_path: Path, capsys) -> None:
    root = tmp_path / "inputs"
    write_bundle(root)
    duplicated = [
        {
            "instance_id": "i1",
            "service": "svc",
            "priority": 1,
            "requested_ephemeral_bytes": 1,
            "required_layers": ["sha256:l1"],
            "deadline_ms": 10,
        },
        {
            "instance_id": "i1",
            "service": "svc",
            "priority": 1,
            "requested_ephemeral_bytes": 1,
            "required_layers": ["sha256:l1"],
            "deadline_ms": 10,
        },
    ]
    (root / "instances.json").write_text(json.dumps({"instances": duplicated}), encoding="utf-8")
    database = tmp_path / "must-not-exist.db"
    assert main(["--input-dir", str(root), "--db", str(database), "audit"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["error"]["code"] == ErrorCode.DUPLICATE_INSTANCE
    assert not database.exists()


def test_corrupt_json_returns_parse_code(tmp_path: Path) -> None:
    root = tmp_path / "inputs"
    write_bundle(root)
    (root / "layers.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(BusinessError) as caught:
        load_inputs(root)
    assert caught.value.code == ErrorCode.INVALID_JSON

