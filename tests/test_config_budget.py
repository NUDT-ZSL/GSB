from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_pipeline.budget import RouteBudgetInput, allocate_frame_budget
from video_pipeline.config import (
    load_detection_config,
    load_matching_config,
    semantic_config_hash,
    validate_config_combination,
)
from video_pipeline.errors import ConfigError

from conftest import MODEL_CONFIG


def test_detection_config_validation_reports_all_required_fields(tmp_path: Path) -> None:
    path = tmp_path / "bad_model.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError) as raised:
        load_detection_config(path)
    message = str(raised.value)
    assert "windows" in message
    assert "target_colors" in message
    assert "detection_threshold" in message


def test_route_level_config_error_is_isolated_from_other_routes(tmp_path: Path) -> None:
    path = tmp_path / "matching.json"
    payload = {
        "max_workers": 2,
        "total_frame_budget": 12,
        "iou_match_threshold": 0.25,
        "candidate_pool_capacity": {"vehicle": 2, "pedestrian": 2},
        "routes": [
            {"name": "good.avi", "priority": 4, "frame_budget": 4},
            {"name": "bad.avi", "priority": "high", "frame_budget": 3},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    config = load_matching_config(path)
    assert config.rule_for("good.avi").priority == 4
    assert config.rule_for("bad.avi").frame_budget == 12
    assert len(config.route_issues) == 1
    assert config.route_issues[0].route == "bad.avi"


def test_budget_allocation_priority_route_cap_and_truncation() -> None:
    routes = [
        RouteBudgetInput("high.avi", 40, 8, 8),
        RouteBudgetInput("mid.avi", 30, 14, 18),
        RouteBudgetInput("low.avi", 20, 12, 16),
        RouteBudgetInput("last.avi", 10, 10, 20),
    ]
    allocation = allocate_frame_budget(routes, 36)
    assert [allocation[name].allocated_frames for name in ("high.avi", "mid.avi", "low.avi", "last.avi")] == [8, 14, 12, 2]
    assert allocation["high.avi"].truncated_frame is None
    assert allocation["mid.avi"].truncated_frame == 14
    assert allocation["low.avi"].truncated_frame == 12
    assert allocation["last.avi"].truncated_frame == 2
    assert sum(item.allocated_frames for item in allocation.values()) == 36


def test_budget_equal_priority_uses_least_consumed_and_name_tiebreak() -> None:
    routes = [
        RouteBudgetInput("b.avi", 5, 10, 10),
        RouteBudgetInput("a.avi", 5, 10, 10),
        RouteBudgetInput("c.avi", 5, 10, 10),
    ]
    allocation = allocate_frame_budget(routes, 7)
    assert allocation["a.avi"].allocated_frames == 3
    assert allocation["b.avi"].allocated_frames == 2
    assert allocation["c.avi"].allocated_frames == 2


def test_semantic_hash_ignores_concurrency_and_changes_on_threshold(tmp_path: Path) -> None:
    detection = load_detection_config(MODEL_CONFIG)
    matching_path = tmp_path / "matching.json"
    payload = {
        "max_workers": 1,
        "total_frame_budget": 20,
        "iou_match_threshold": 0.25,
        "candidate_pool_capacity": {"vehicle": 2, "pedestrian": 2},
        "routes": [{"name": "a.avi", "priority": 1, "frame_budget": 4}],
    }
    matching_path.write_text(json.dumps(payload), encoding="utf-8")
    first = load_matching_config(matching_path)
    validate_config_combination(detection, first)
    payload["max_workers"] = 8
    matching_path.write_text(json.dumps(payload), encoding="utf-8")
    second = load_matching_config(matching_path)
    assert semantic_config_hash(detection, first) == semantic_config_hash(detection, second)
    payload["iou_match_threshold"] = 0.2501
    matching_path.write_text(json.dumps(payload), encoding="utf-8")
    third = load_matching_config(matching_path)
    assert semantic_config_hash(detection, first) != semantic_config_hash(detection, third)
