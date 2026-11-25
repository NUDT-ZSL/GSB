from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import load_json, sha256_file
from .errors import ValidationError


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_fields(obj: dict[str, Any], fields: list[str], context: str, errors: list[str]) -> None:
    for field in fields:
        if field not in obj:
            errors.append(f"{context}: missing field {field}")


def validate_manifest(
    path: str | Path | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if data is None:
        if path is None:
            raise ValidationError("manifest path or data required")
        path = Path(path)
        data = load_json(path)
    source_dir = Path(path).parent if path is not None else Path.cwd()
    errors: list[str] = []
    if not isinstance(data, dict):
        raise ValidationError("manifest must be a JSON object")
    objects = data.get("objects")
    if not isinstance(objects, list) or not objects:
        raise ValidationError("manifest.objects must be a non-empty list")

    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, obj in enumerate(objects):
        context = f"objects[{index}]"
        if not isinstance(obj, dict):
            errors.append(f"{context}: must be an object")
            continue
        _require_fields(
            obj,
            [
                "key",
                "size_bytes",
                "last_access_clock_ms",
                "access_count",
                "heat_weight",
                "declared_tier",
                "content_digest",
            ],
            context,
            errors,
        )
        key = obj.get("key")
        if not isinstance(key, str) or not key:
            errors.append(f"{context}: key must be a non-empty string")
            key = f"<invalid-{index}>"
        elif key in seen:
            errors.append(f"{context}: duplicate object key {key!r}")
        seen.add(key)

        size = obj.get("size_bytes")
        if not is_int(size) or size < 0:
            errors.append(f"{context} ({key}): size_bytes must be a non-negative integer")

        clock = obj.get("last_access_clock_ms")
        if not is_int(clock) or clock < 0:
            errors.append(f"{context} ({key}): last_access_clock_ms must be a non-negative integer")

        count = obj.get("access_count")
        if not is_int(count) or count < 0:
            errors.append(f"{context} ({key}): access_count must be a non-negative integer")

        weight = obj.get("heat_weight")
        if not is_int(weight) or weight < 0:
            errors.append(f"{context} ({key}): heat_weight must be a non-negative integer")
        elif weight > 100:
            errors.append(f"{context} ({key}): heat_weight must be in [0, 100] percentage points")

        if obj.get("declared_tier") not in {"hot", "cold"}:
            errors.append(f"{context} ({key}): declared_tier must be hot or cold")

        digest = obj.get("content_digest")
        digest_hex = digest
        if isinstance(digest, str) and digest.startswith("sha256:"):
            digest_hex = digest.split(":", 1)[1]
        if not isinstance(digest_hex, str) or len(digest_hex) != 64:
            errors.append(f"{context} ({key}): content_digest must be sha256:<64 hex chars>")

        blob_path = obj.get("blob_path")
        if isinstance(blob_path, str) and is_int(size) and size >= 0:
            resolved = source_dir / blob_path
            if not resolved.is_file():
                errors.append(f"{context} ({key}): blob does not exist: {resolved}")
            else:
                obj["blob_path"] = str(resolved.resolve())
                actual_size = resolved.stat().st_size
                if actual_size != size:
                    errors.append(
                        f"{context} ({key}): byte count mismatch manifest={size} file={actual_size}"
                    )
                if isinstance(digest_hex, str) and len(digest_hex) == 64:
                    actual_digest = sha256_file(resolved)
                    if actual_digest != digest_hex.lower():
                        errors.append(f"{context} ({key}): content digest mismatch")
        elif obj.get("verify_blob", True):
            errors.append(f"{context} ({key}): blob_path is required for digest/byte verification")

        normalized.append(dict(obj))

    if errors:
        raise ValidationError("manifest validation failed", {"errors": errors[:50], "error_count": len(errors)})

    result = dict(data)
    result["objects"] = sorted(normalized, key=lambda item: item["key"])
    result["object_count"] = len(normalized)
    return result


def validate_config(
    path: str | Path | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if data is None:
        if path is None:
            raise ValidationError("config path or data required")
        data = load_json(path)
    if not isinstance(data, dict):
        raise ValidationError("config must be an object")
    errors: list[str] = []

    positive_fields = [
        "hot_capacity_bytes",
        "cold_capacity_bytes",
        "migration_byte_budget",
        "max_concurrency",
    ]
    for field in positive_fields:
        value = data.get(field)
        if not is_int(value) or value <= 0:
            errors.append(f"{field} must be a positive integer")

    scale = data.get("score_scale_bp", 10_000)
    if not is_int(scale) or scale <= 0:
        errors.append("score_scale_bp must be a positive integer")
    hot = data.get("hot_threshold_bp")
    cold = data.get("cold_threshold_bp")
    if not is_int(hot) or not (0 <= hot <= scale):
        errors.append("hot_threshold_bp must be an integer in [0, score_scale_bp]")
    if not is_int(cold) or not (0 <= cold <= scale):
        errors.append("cold_threshold_bp must be an integer in [0, score_scale_bp]")
    if is_int(hot) and is_int(cold) and not (0 <= cold <= hot <= scale):
        errors.append("thresholds must satisfy 0 <= cold_threshold_bp <= hot_threshold_bp <= scale")

    tolerance = data.get("clock_rollback_tolerance_ms", 0)
    if not is_int(tolerance) or tolerance < 0:
        errors.append("clock_rollback_tolerance_ms must be a non-negative integer")

    backoff = data.get("backoff")
    if not isinstance(backoff, dict):
        errors.append("backoff must be an object")
    else:
        for field in ["initial_delay_ms", "max_delay_ms"]:
            value = backoff.get(field)
            if not is_int(value) or value <= 0:
                errors.append(f"backoff.{field} must be a positive integer")
        multiplier = backoff.get("multiplier")
        if not isinstance(multiplier, (int, float)) or isinstance(multiplier, bool) or multiplier < 1:
            errors.append("backoff.multiplier must be a number >= 1")

    cost = data.get("cost_model")
    if not isinstance(cost, dict):
        errors.append("cost_model must be an object")
    else:
        if not is_int(cost.get("fixed_point_scale")) or cost["fixed_point_scale"] <= 0:
            errors.append("cost_model.fixed_point_scale must be a positive integer")
        if not is_int(cost.get("bandwidth_bytes_per_ms")) or cost["bandwidth_bytes_per_ms"] <= 0:
            errors.append("cost_model.bandwidth_bytes_per_ms must be a positive integer")
        if not is_int(cost.get("difference_tolerance_scaled")) or cost["difference_tolerance_scaled"] < 0:
            errors.append("cost_model.difference_tolerance_scaled must be a non-negative integer")

    if errors:
        raise ValidationError("configuration validation failed", {"errors": errors})
    return data


def validate_events(
    manifest: dict[str, Any],
    path: str | Path | None = None,
    data: dict[str, Any] | list[Any] | None = None,
) -> dict[str, Any]:
    if data is None:
        if path is None:
            raise ValidationError("events path or data required")
        data = load_json(path)
    if isinstance(data, dict):
        events = data.get("events")
    else:
        events = data
    if not isinstance(events, list):
        raise ValidationError("events must be a list or an object with events list")

    known_keys = {obj["key"] for obj in manifest["objects"]}
    errors: list[str] = []
    previous_seq: int | None = None
    normalized: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        context = f"events[{index}]"
        if not isinstance(event, dict):
            errors.append(f"{context}: must be an object")
            continue
        seq = event.get("seq")
        if not is_int(seq) or seq < 0:
            errors.append(f"{context}: seq must be a non-negative integer")
        elif previous_seq is not None and seq <= previous_seq:
            errors.append(f"{context}: seq {seq} is not strictly greater than {previous_seq}")
        if is_int(seq):
            previous_seq = seq

        object_key = event.get("object_key")
        if object_key not in known_keys:
            errors.append(f"{context}: references unknown object {object_key!r}")

        clock = event.get("clock_ms", event.get("clock"))
        if not is_int(clock) or clock < 0:
            errors.append(f"{context}: clock_ms must be a non-negative integer")

        event_type = event.get("event_type")
        if event_type not in {"requested", "claimed", "started", "succeeded", "failed", "abandoned", "recovered"}:
            errors.append(f"{context}: unsupported event_type {event_type!r}")

        normalized.append(dict(event))

    if errors:
        raise ValidationError("event validation failed", {"errors": errors[:50], "error_count": len(errors)})
    return {"events": normalized, "event_count": len(normalized)}
