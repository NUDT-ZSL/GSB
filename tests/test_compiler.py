from __future__ import annotations

from pathlib import Path

import pytest

from qrw.canonical import walk_nodes
from qrw.compiler import compile_bytes, compile_templates
from qrw.errors import BusinessError
from qrw.readers import read_template_file, read_template_text


def test_compile_is_byte_deterministic_and_traceable(compiled_core):
    for dialect, compiled in compiled_core.items():
        assert compile_bytes(compiled) == compile_bytes(compiled)
        nodes = list(walk_nodes(compiled))
        assert nodes
        for _path, node in nodes:
            assert node["source"]["line"] >= 1
            assert node["source"]["dialect"] == dialect
            assert node["source"]["source"]


def test_slot_type_out_of_bounds(samples: Path, slot_types, config):
    with pytest.raises(BusinessError) as exc:
        parsed = read_template_file(
            samples / "anomalies" / "bad_slot_type.json", slot_types, config
        )
        compile_templates(parsed, config)
    assert exc.value.code == "E_CONFIG_INVALID"
    assert exc.value.field == "rules.bad_geo.slots.city.type"
    assert exc.value.details["reason"] == "slot_type_out_of_bounds"


@pytest.mark.parametrize(
    "field,value,expected_field",
    [
        ("weight", "0", "rules.R.weight"),
        ("max_depth", "9", "rules.R.max_depth"),
    ],
)
def test_invalid_weight_and_depth(
    slot_types,
    config,
    field: str,
    value: str,
    expected_field: str,
):
    doc = {
        "version": "V3",
        "rules": [
            {
                "id": "R",
                "priority": 1,
                "weight": 1,
                "enabled": True,
                "max_depth": 1,
                "pattern": "{city}",
                "slots": [{"name": "city", "type": "GEO"}],
                "replacement": [{"text": "{city}"}],
            }
        ],
    }
    doc["rules"][0][field] = float(value) if field == "weight" else int(value)
    import json

    parsed = read_template_text(json.dumps(doc), "<config-test>", slot_types, config)
    with pytest.raises(BusinessError) as exc:
        compile_templates(parsed, config)
    assert exc.value.code == "E_CONFIG_INVALID"
    assert exc.value.field == expected_field


def test_reference_cycle_is_rejected(slot_types, config):
    import json

    doc = {
        "version": "V3",
        "rules": [
            {
                "id": "R",
                "priority": 1,
                "pattern": "{city}",
                "slots": [{"name": "city", "type": "GEO"}],
                "replacement": [{"ref": "F"}],
            },
            {
                "id": "F",
                "kind": "fragment",
                "priority": 0,
                "slots": [],
                "replacement": [{"ref": "G"}],
            },
            {
                "id": "G",
                "kind": "fragment",
                "priority": 0,
                "slots": [],
                "replacement": [{"ref": "F"}],
            },
        ],
    }
    parsed = read_template_text(json.dumps(doc), "<cycle>", slot_types, config)
    with pytest.raises(BusinessError) as exc:
        compile_templates(parsed, config)
    assert exc.value.code == "E_CONFIG_INVALID"
    assert "reference cycle" in exc.value.message

