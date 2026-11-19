from __future__ import annotations

import json

import pytest

from shardroute.errors import BizError
from shardroute.router import build_rewrite, merge_params

from .conftest import cli_json


def _rewrite(state, template_id: str, params: dict | None = None):
    snapshot = state.read_snapshot()
    template = state.query_template(template_id)
    return build_rewrite(
        snapshot.snapshot,
        template,
        merge_params(template, params),
        snapshot.version,
    )


def test_eq_route_and_physical_sql(state):
    rewrite = _rewrite(state, "orders_eq")
    assert rewrite.target_shards == (2,)
    assert rewrite.full_route is False
    assert rewrite.shards[0].table == "orders_02"
    assert rewrite.shards[0].database == "shards/orders_02.db"
    assert '"orders_02"' in rewrite.shards[0].sql
    assert "customer_id = 6" in rewrite.shards[0].sql
    assert "orders WHERE" not in rewrite.shards[0].sql


def test_mod_range_clips_each_shard_predicate(state):
    rewrite = _rewrite(state, "orders_range")
    assert rewrite.target_shards == (2, 3)
    assert "customer_id >= 2 AND customer_id <= 2 AND customer_id % 4 = 2" in rewrite.shards[0].sql
    assert "customer_id >= 3 AND customer_id <= 3 AND customer_id % 4 = 3" in rewrite.shards[1].sql


def test_in_route_groups_values_per_shard(state):
    rewrite = _rewrite(state, "orders_in")
    assert rewrite.target_shards == (1, 2)
    assert "customer_id IN (1)" in rewrite.shards[0].sql
    assert "customer_id IN (6, 10)" in rewrite.shards[1].sql


def test_all_route_is_explicit(state):
    rewrite = _rewrite(state, "orders_all")
    assert rewrite.full_route is True
    assert rewrite.target_shards == (0, 1, 2, 3)
    assert all("{{shard_predicate}}" not in shard.sql for shard in rewrite.shards)


def test_range_sharding_clips_to_boundaries(state):
    rewrite = _rewrite(state, "events_range")
    assert rewrite.target_shards == (0, 1, 2)
    assert "event_no >= 900 AND event_no <= 999" in rewrite.shards[0].sql
    assert "event_no >= 1000 AND event_no <= 1999" in rewrite.shards[1].sql
    assert "event_no >= 2000 AND event_no <= 2400" in rewrite.shards[2].sql


def test_route_repeat_is_byte_stable(state):
    first = cli_json("route", "--template", "events_range", "--clock", "1100", state_path=state.path)
    second = cli_json("route", "--template", "events_range", "--clock", "1100", state_path=state.path)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_out_of_range_values_are_business_errors(state):
    snapshot = state.read_snapshot()
    template = state.query_template("events_eq")
    with pytest.raises(BizError) as raised:
        build_rewrite(snapshot.snapshot, template, {"shard_key": 4000}, snapshot.version)
    error = raised.value
    assert error.code == "E_SHARD_KEY_OUT_OF_RANGE"
    assert error.stage == "route"
    assert error.query_template == "events_eq"
    assert error.rule_version == "r1"


def test_range_outside_domain_is_rejected(state):
    snapshot = state.read_snapshot()
    template = state.query_template("events_range")
    with pytest.raises(BizError) as raised:
        build_rewrite(snapshot.snapshot, template, {"start": 5000, "end": 6000}, snapshot.version)
    assert raised.value.code == "E_SHARD_KEY_OUT_OF_RANGE"


def test_negative_shard_key_is_out_of_range(state):
    snapshot = state.read_snapshot()
    template = state.query_template("orders_eq")
    with pytest.raises(BizError) as raised:
        build_rewrite(snapshot.snapshot, template, {"shard_key": -1}, snapshot.version)
    assert raised.value.code == "E_SHARD_KEY_OUT_OF_RANGE"
    assert raised.value.stage == "route"

