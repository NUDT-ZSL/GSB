from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import BusinessError, fail
from .util import read_json, sha256_json


STAGE_ORDER = ["features", "noise_floor", "attribution", "profile"]


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        fail("E_CONFIG_INVALID", detail="configuration file is missing", path=str(path))
    try:
        config = read_json(path)
    except (OSError, ValueError) as exc:
        fail("E_CONFIG_INVALID", detail=f"cannot read configuration: {exc}", path=str(path))
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    if not isinstance(config, dict):
        fail("E_CONFIG_INVALID", detail="configuration must be an object")
    if config.get("sample_rate") != 16000:
        fail(
            "E_CONFIG_INVALID",
            detail="sample_rate must be 16000",
            sample_rate=config.get("sample_rate"),
        )
    if config.get("frame_ms") != 20:
        fail(
            "E_CONFIG_INVALID",
            detail="frame_ms must be 20",
            frame_ms=config.get("frame_ms"),
        )
    for section in ("features", "noise_floor", "attribution", "profile"):
        if not isinstance(config.get(section), dict):
            fail("E_CONFIG_INVALID", detail=f"{section} must be an object")

    features = config["features"]
    if int(features.get("frame_shift_ms", 0)) <= 0:
        fail("E_CONFIG_INVALID", detail="frame_shift_ms must be positive")
    if features.get("frame_shift_ms") != config["frame_ms"]:
        fail(
            "E_CONFIG_INVALID",
            detail="overlapping or gapped feature frames are not supported",
            frame_shift_ms=features.get("frame_shift_ms"),
        )

    noise = config["noise_floor"]
    if not 0.0 <= float(noise.get("quantile", -1)) <= 1.0:
        fail("E_CONFIG_INVALID", detail="noise_floor.quantile must be between 0 and 1")
    if int(noise.get("min_step_frames", 0)) <= 0:
        fail("E_CONFIG_INVALID", detail="noise_floor.min_step_frames must be positive")
    if int(noise.get("min_segment_frames", 0)) <= 0:
        fail("E_CONFIG_INVALID", detail="noise_floor.min_segment_frames must be positive")

    attribution = config["attribution"]
    labels = attribution.get("controlled_labels")
    if not isinstance(labels, list) or not labels or len(labels) != len(set(labels)):
        fail("E_CONFIG_INVALID", detail="controlled_labels must be a unique non-empty list")
    rules = attribution.get("rules")
    if not isinstance(rules, list) or not rules:
        fail("E_CONFIG_INVALID", detail="attribution.rules must be a non-empty list")
    validate_rule_labels(rules, set(labels), call_id=None)
    default_label = attribution.get("default_label")
    if default_label is not None and default_label not in labels:
        fail("E_LABEL_INVALID", label=default_label)
    if int(config["profile"].get("min_gap_frames", -1)) < 0:
        fail("E_CONFIG_INVALID", detail="profile.min_gap_frames must be non-negative")


def validate_rule_labels(
    rules: list[dict[str, Any]], controlled: set[str], call_id: str | None
) -> None:
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            fail("E_CONFIG_INVALID", detail=f"attribution rule {index} must be an object")
        label = rule.get("label")
        if label not in controlled:
            context: dict[str, Any] = {"label": label, "rule_index": index}
            if call_id is not None:
                context["call_id"] = call_id
            raise BusinessError(code="E_LABEL_INVALID", context=context)


def section_revision(config: dict[str, Any], section: str) -> str:
    if section == "features":
        payload = {
            "sample_rate": config["sample_rate"],
            "frame_ms": config["frame_ms"],
            "section": config["features"],
        }
    elif section == "noise_floor":
        payload = {
            "sample_rate": config["sample_rate"],
            "frame_ms": config["frame_ms"],
            "section": config["noise_floor"],
        }
    elif section == "attribution":
        payload = {
            "sample_rate": config["sample_rate"],
            "frame_ms": config["frame_ms"],
            "section": config["attribution"],
        }
    elif section == "profile":
        payload = {
            "sample_rate": config["sample_rate"],
            "frame_ms": config["frame_ms"],
            "section": config["profile"],
        }
    else:
        fail("E_CONFIG_INVALID", detail=f"unknown stage section: {section}")
    return sha256_json(payload)


def all_revisions(config: dict[str, Any]) -> dict[str, str]:
    return {section: section_revision(config, section) for section in STAGE_ORDER}

