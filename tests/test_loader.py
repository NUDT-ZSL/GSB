import json

import pytest

from hotcfg.errors import BizError, E_CONFIG
from hotcfg.loader import (
    load_config_items,
    load_sample_series,
    load_task_specs,
)


def _write(tmp_path, name, doc):
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


GOOD_CONFIG = {
    "items": [
        {"key": "a", "type": "int", "value": 1, "weight": 1.0},
        {"key": "b.c", "type": "float", "value": 0.5, "weight": 2},
    ]
}
GOOD_TASKS = {"tasks": [{"id": "t1", "keys": ["a", "b.c"], "aggregation": "sum"}]}


def test_load_ok(tmp_path):
    cfg = _write(tmp_path, "c.json", GOOD_CONFIG)
    items = load_config_items(cfg)
    assert set(items) == {"a", "b.c"}
    tasks = load_task_specs(_write(tmp_path, "t.json", GOOD_TASKS), items)
    assert tasks[0].keys == ("a", "b.c")


@pytest.mark.parametrize(
    "mutate, position",
    [
        (lambda d: d["items"].append(d["items"][0].copy()), "items[2].key"),
        (lambda d: d["items"][0].update(key="0bad"), "items[0].key"),
        (lambda d: d["items"][0].update(type="str"), "items[0].type"),
        (lambda d: d["items"][0].update(value="x"), "items[0].value"),
        (lambda d: d["items"][0].update(weight="heavy"), "items[0].weight"),
        (lambda d: d["items"][0].update(weight=True), "items[0].weight"),
    ],
)
def test_config_rejected_with_position(tmp_path, mutate, position):
    doc = json.loads(json.dumps(GOOD_CONFIG))
    mutate(doc)
    p = _write(tmp_path, "bad.json", doc)
    with pytest.raises(BizError) as ei:
        load_config_items(p)
    assert ei.value.code == E_CONFIG
    assert ei.value.details["position"] == position


def test_corrupt_json_rejected(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text('{"items": [', encoding="utf-8")
    with pytest.raises(BizError) as ei:
        load_config_items(p)
    assert ei.value.code == E_CONFIG
    assert ei.value.details["position"] == "$"


def test_task_reference_and_aggregation(tmp_path):
    items = load_config_items(_write(tmp_path, "c.json", GOOD_CONFIG))
    bad_ref = {"tasks": [{"id": "t", "keys": ["a", "ghost"], "aggregation": "sum"}]}
    with pytest.raises(BizError) as ei:
        load_task_specs(_write(tmp_path, "t1.json", bad_ref), items)
    assert ei.value.code == E_CONFIG
    assert ei.value.details["position"] == "tasks[0].keys"
    bad_agg = {"tasks": [{"id": "t", "keys": ["a"], "aggregation": "median"}]}
    with pytest.raises(BizError) as ei:
        load_task_specs(_write(tmp_path, "t2.json", bad_agg), items)
    assert ei.value.details["position"] == "tasks[0].aggregation"


def test_series_validation(tmp_path):
    items = load_config_items(_write(tmp_path, "c.json", GOOD_CONFIG))
    bad = {"series": {"a": [1, 2], "ghost": [1]}}
    with pytest.raises(BizError) as ei:
        load_sample_series(_write(tmp_path, "d.json", bad), items)
    assert ei.value.code == E_CONFIG
