from __future__ import annotations

import json

import pytest

from driftmon.alerts import AlertStore
from driftmon.canonical import sha256_obj
from driftmon.cache import CacheStore
from driftmon.config import default_config
from driftmon.engine import MonitorEngine
from driftmon.errors import E_LINEAGE_BROKEN, AppError
from driftmon.lineage import replay_alert


def test_alert_versions_replace_whole_artifact_and_old_replays(runtime) -> None:
    config, cache, alerts = runtime
    engine = MonitorEngine(config, cache, alerts)
    first = engine.run(3)
    assert first.alert is not None
    old = alerts.load_current()
    assert old is not None
    assert old["checksum"] == sha256_obj(old["content"])

    changed = config.updated(window_size=2, slide_step=1)
    second = MonitorEngine(changed, cache, alerts).run(
        4, previous_config=config, previous_cursor=3
    )
    assert second.alert is not None
    current = alerts.load_current()
    assert current is not None
    assert current["version"] > old["version"]
    assert current["checksum"] != old["checksum"]
    assert alerts.get_alert(old["alert_id"])["checksum"] == old["checksum"]
    replay = replay_alert(old, cache)
    assert replay["matches"] is True
    assert replay["replayed_checksum"] == old["checksum"]


def test_missing_cache_item_breaks_lineage_with_precise_code_and_path(runtime) -> None:
    config, cache, alerts = runtime
    result = MonitorEngine(config, cache, alerts).run(3)
    assert result.alert is not None
    alert = alerts.load_current()
    assert alert is not None
    feature_record = alert["content"]["features"][0]
    stats_key = feature_record["stats_key"]
    cache._path(stats_key).unlink()
    with pytest.raises(AppError) as captured:
        replay_alert(alert, cache)
    assert captured.value.code == E_LINEAGE_BROKEN
    assert any(
        item == f"cache:stats:{stats_key}"
        for item in captured.value.details["missing"]
    )


def test_alert_checksum_tamper_is_detected(runtime) -> None:
    config, cache, alerts = runtime
    MonitorEngine(config, cache, alerts).run(3)
    payload = json.loads(alerts.current_path.read_text(encoding="utf-8"))
    payload["content"]["summary"]["detected_count"] = 999
    alerts.current_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AppError) as captured:
        alerts.load_current()
    assert captured.value.code == "E_CHECKSUM_MISMATCH"


def test_checksum_change_is_replaced_as_a_whole_new_version(runtime) -> None:
    config, cache, alerts = runtime
    MonitorEngine(config, cache, alerts).run(3)
    original = alerts.load_current()
    assert original is not None
    corrupted = json.loads(alerts.current_path.read_text(encoding="utf-8"))
    corrupted["content"]["summary"]["detected_count"] = 999
    alerts.current_path.write_text(json.dumps(corrupted), encoding="utf-8")
    replacement = alerts.emit(
        original["content"]["config_version"],
        original["content"]["window"],
        original["content"]["features"],
    )
    assert replacement is not None
    assert replacement["version"] == original["version"] + 1
    repaired = alerts.load_current()
    assert repaired is not None
    assert repaired["version"] == replacement["version"]
    assert repaired["checksum"] == sha256_obj(repaired["content"])
    assert alerts.get_alert(original["alert_id"])["checksum"] == original["checksum"]

