from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .canon import canonical_hash, sha256_file
from .errors import Code, WorkbenchError
from .loader import _normalize_v2_entry, _read_json
from .rules import require_nonnegative_int, require_positive_int


SOURCE_PRIORITY = {"v1": 10, "v2": 20, "v2-package": 30}


def convert_v1_effective_time(entry: Dict[str, Any], injection_clock_ms: int) -> Dict[str, Any]:
    relative_seconds = entry["effective_rel_sec"]
    scale = 1000
    numerator = injection_clock_ms * scale + relative_seconds * scale
    absolute_ms = numerator // scale
    remainder = numerator % scale
    return {
        "effective_at_ms": absolute_ms,
        "original_time": {
            "format": "v1_relative_seconds",
            "effective_rel_sec": relative_seconds,
            "snapshot_declared_clock_ms": entry.get("snapshot_declared_clock_ms"),
        },
        "time_conversion": {
            "base_clock_ms": injection_clock_ms,
            "relative_seconds": relative_seconds,
            "scale_ms_per_second": scale,
            "formula": "base_clock_ms + effective_rel_sec * scale_ms_per_second",
            "rounding_mode": "ceil",
            "rounding_direction": "toward_positive_infinity",
            "numerator": numerator,
            "denominator": scale,
            "remainder": remainder,
            "rounded": remainder != 0,
            "rounding_delta_ms": 0 if remainder == 0 else scale - remainder,
        },
    }


def _candidate_from_v1(entry: Dict[str, Any], injection_clock_ms: int, snapshot_clock: int) -> Dict[str, Any]:
    timing = convert_v1_effective_time({**entry, "snapshot_declared_clock_ms": snapshot_clock}, injection_clock_ms)
    return {
        "source": "v1",
        "declared_clock_ms": entry["declared_clock_ms"],
        "semantic_generation": entry["semantic_generation"],
        "revision": entry["revision"],
        "tombstone": entry["tombstone"],
        "tombstone_reason": entry["tombstone_reason"],
        "dependencies": entry["dependencies"],
        **timing,
    }


def _candidate_from_v2(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source": "v2",
        "declared_clock_ms": entry["declared_clock_ms"],
        "semantic_generation": entry["semantic_generation"],
        "revision": entry["revision"],
        "tombstone": entry["tombstone"],
        "tombstone_reason": entry["tombstone_reason"],
        "dependencies": entry["dependencies"],
        "effective_at_ms": entry["effective_at_ms"],
        "original_time": {"format": "v2_absolute_milliseconds", "effective_at_ms": entry["effective_at_ms"]},
        "time_conversion": None,
    }


def _entry_content(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "message_key": entry["message_key"],
        "effective_at_ms": entry["effective_at_ms"],
        "semantic_generation": entry["semantic_generation"],
        "revision": entry["revision"],
        "tombstone": entry["tombstone"],
        "tombstone_reason": entry["tombstone_reason"],
        "dependencies": sorted(
            entry["dependencies"],
            key=lambda dep: (dep["topic"], dep["partition"], dep["offset"], dep["required_watermark"]),
        ),
    }


def merge_snapshots(bundle: Dict[str, Any], injection_clock_ms: int) -> Dict[str, Any]:
    require_nonnegative_int(injection_clock_ms, Code.EVENT_FIELD, "injection_clock_ms")
    v1_by_key = {entry["message_key"]: entry for entry in bundle["v1"]["entries"]}
    v2_by_key = {entry["message_key"]: entry for entry in bundle["v2"]["entries"]}
    entries: List[Dict[str, Any]] = []
    missing_keys: List[str] = []
    for event in sorted(bundle["events"], key=lambda item: (item["topic"], item["partition"], item["offset"])):
        key = event["message_key"]
        candidates: List[Dict[str, Any]] = []
        if key in v1_by_key:
            candidates.append(_candidate_from_v1(v1_by_key[key], injection_clock_ms, bundle["v1"]["declared_clock_ms"]))
        if key in v2_by_key:
            candidates.append(_candidate_from_v2(v2_by_key[key]))
        if not candidates:
            missing_keys.append(key)
            entries.append(
                {
                    "message_key": key,
                    "topic": event["topic"],
                    "partition": event["partition"],
                    "offset": event["offset"],
                    "present": False,
                    "content_hash": None,
                }
            )
            continue
        if len(candidates) == 1:
            winner = candidates[0]
            adjudication = {
                "rule": "single_source",
                "winner_source": winner["source"],
                "reason": "only one format contains this message key",
                "candidates": [_adjudication_summary(candidate) for candidate in candidates],
            }
        else:
            v1_candidate = next(c for c in candidates if c["source"] == "v1")
            v2_candidate = next(c for c in candidates if c["source"] == "v2")
            if v1_candidate["declared_clock_ms"] == v2_candidate["declared_clock_ms"]:
                winner = max(candidates, key=lambda item: SOURCE_PRIORITY[item["source"]])
                reason = "equal_declared_clock_source_format_priority"
                rule = "clock_then_source_priority"
            elif v1_candidate["declared_clock_ms"] > v2_candidate["declared_clock_ms"]:
                winner = v1_candidate
                reason = "newer_declared_logical_clock"
                rule = "clock_recency"
            else:
                winner = v2_candidate
                reason = "newer_declared_logical_clock"
                rule = "clock_recency"
            loser = v2_candidate if winner is v1_candidate else v1_candidate
            generation_rollback = winner["semantic_generation"] < loser["semantic_generation"]
            adjudication = {
                "rule": rule,
                "winner_source": winner["source"],
                "reason": reason,
                "generation_rollback_against_other_format": generation_rollback,
                "generation_delta": winner["semantic_generation"] - loser["semantic_generation"],
                "source_priority": SOURCE_PRIORITY,
                "candidates": [_adjudication_summary(candidate) for candidate in sorted(candidates, key=lambda c: c["source"])],
            }
        merged = {
            "message_key": key,
            "topic": event["topic"],
            "partition": event["partition"],
            "offset": event["offset"],
            "present": True,
            "source": winner["source"],
            "declared_clock_ms": winner["declared_clock_ms"],
            "effective_at_ms": winner["effective_at_ms"],
            "semantic_generation": winner["semantic_generation"],
            "revision": winner["revision"],
            "tombstone": winner["tombstone"],
            "tombstone_reason": winner["tombstone_reason"],
            "dependencies": winner["dependencies"],
            "original_time": winner["original_time"],
            "time_conversion": winner["time_conversion"],
            "adjudication": adjudication,
        }
        merged["content_hash"] = canonical_hash(_entry_content(merged))
        entries.append(merged)
    view = {
        "view_schema": "unified-delivery-semantics/v1",
        "injection_clock_ms": injection_clock_ms,
        "source_bindings": {
            "v1_snapshot_id": bundle["v1"]["snapshot_id"],
            "v1_snapshot_version": bundle["v1"]["snapshot_version"],
            "v2_snapshot_id": bundle["v2"]["snapshot_id"],
            "v2_snapshot_version": bundle["v2"]["snapshot_version"],
            "v2_generation_min": bundle["v2"]["generation_min"],
            "v2_manifest_hash": bundle["v2"]["manifest_hash"],
        },
        "missing_keys": missing_keys,
        "entries": sorted(entries, key=lambda item: item["message_key"]),
    }
    view["content_hash"] = canonical_hash({key: value for key, value in view.items() if key != "content_hash"})
    view["view_id"] = f"unified-{view['content_hash'][:16]}"
    return view


def _adjudication_summary(candidate: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source": candidate["source"],
        "declared_clock_ms": candidate["declared_clock_ms"],
        "semantic_generation": candidate["semantic_generation"],
        "effective_at_ms": candidate["effective_at_ms"],
        "source_priority": SOURCE_PRIORITY[candidate["source"]],
    }


def load_package(package_dir: str | Path) -> Dict[str, Any]:
    root = Path(package_dir)
    manifest_path = root / "package.json"
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("format") != "v2-semantic-package":
        raise WorkbenchError(Code.PACKAGE_INVALID, "package.json format must be v2-semantic-package", {"path": str(manifest_path)})
    snapshot_version = require_positive_int(manifest.get("snapshot_version"), Code.PACKAGE_INVALID, "snapshot_version")
    generation_min = require_nonnegative_int(manifest.get("generation_min"), Code.PACKAGE_INVALID, "generation_min")
    shard_refs = manifest.get("shards")
    if not isinstance(shard_refs, list) or not shard_refs:
        raise WorkbenchError(Code.PACKAGE_INVALID, "package must contain at least one shard")
    entries: List[Dict[str, Any]] = []
    shards: List[Dict[str, Any]] = []
    seen = set()
    previous_hash = manifest.get("prev_manifest_hash", "GENESIS")
    for index, ref in enumerate(shard_refs):
        if not isinstance(ref, dict) or ref.get("ordinal") != index:
            raise WorkbenchError(Code.PACKAGE_INVALID, "package shard ordinals must be contiguous", {"index": index})
        path = root / ref["path"]
        actual = sha256_file(str(path)) if path.is_file() else None
        if actual is None or actual != ref.get("sha256"):
            raise WorkbenchError(Code.V2_HASH, "package shard hash mismatch", {"path": str(path), "expected": ref.get("sha256"), "actual": actual})
        shard = _read_json(path)
        if not isinstance(shard, dict) or not isinstance(shard.get("entries"), list):
            raise WorkbenchError(Code.PACKAGE_INVALID, "package shard is invalid", {"path": str(path)})
        if ref.get("prev_sha256", "GENESIS") != previous_hash and index > 0:
            raise WorkbenchError(Code.V2_HASH, "package shard chain is broken", {"ordinal": index})
        for raw_entry in shard["entries"]:
            entry = _normalize_v2_entry(raw_entry, str(path))
            if entry["message_key"] in seen:
                raise WorkbenchError(Code.PACKAGE_INVALID, "duplicate package message_key", {"message_key": entry["message_key"]})
            seen.add(entry["message_key"])
            entries.append(entry)
        shards.append({"ordinal": index, "path": ref["path"], "sha256": actual, "semantic_generation": ref.get("semantic_generation")})
        previous_hash = actual
    return {
        "package_dir": str(root),
        "package_id": manifest.get("package_id", f"pkg-{snapshot_version}"),
        "snapshot_version": snapshot_version,
        "generation_min": generation_min,
        "base_snapshot_version": manifest.get("base_snapshot_version"),
        "mode": manifest.get("mode", "auto"),
        "declared_clock_ms": require_nonnegative_int(manifest.get("declared_clock_ms"), Code.PACKAGE_INVALID, "declared_clock_ms"),
        "manifest_hash": canonical_hash({k: v for k, v in manifest.items() if k != "manifest_sha256"}),
        "shards": shards,
        "entries": entries,
    }


def decide_package(current_binding: Optional[Dict[str, Any]], package: Dict[str, Any]) -> Dict[str, Any]:
    if current_binding is None:
        return {"decision": "initial_apply", "mode": "full", "reasons": ["no current semantic pointer"]}
    current_version = int(current_binding["snapshot_version"])
    incoming_version = int(package["snapshot_version"])
    if incoming_version < current_version:
        raise WorkbenchError(
            Code.VERSION_ROLLBACK,
            "semantic snapshot version rollback is rejected",
            {"current_version": current_version, "incoming_version": incoming_version},
        )
    current_manifest_hash = current_binding.get("manifest_hash")
    if incoming_version == current_version:
        if package["manifest_hash"] == current_manifest_hash:
            return {"decision": "idempotent", "mode": "none", "reasons": ["same version and manifest hash"]}
        raise WorkbenchError(
            Code.VERSION_CONFLICT,
            "same semantic version has a different shard/manifest hash",
            {"version": incoming_version, "current_manifest_hash": current_manifest_hash, "incoming_manifest_hash": package["manifest_hash"]},
        )
    if package["generation_min"] < int(current_binding.get("generation_min", 0)):
        raise WorkbenchError(
            Code.GENERATION_FLOOR,
            "incoming semantic generation lower bound regresses",
            {"current_generation_min": current_binding.get("generation_min"), "incoming_generation_min": package["generation_min"]},
        )
    current_hashes = set((current_binding.get("shard_hashes") or {}).values())
    incoming_hashes = {shard["sha256"] for shard in package["shards"]}
    unchanged_hashes = sorted(current_hashes & incoming_hashes)
    requested_mode = package["mode"]
    if requested_mode == "full":
        mode = "full"
        reasons = ["manifest requests full replacement"]
    elif requested_mode == "incremental":
        if package.get("base_snapshot_version") not in (None, current_version):
            raise WorkbenchError(
                Code.PACKAGE_INVALID,
                "incremental package base_snapshot_version does not match current pointer",
                {"current_version": current_version, "base_snapshot_version": package.get("base_snapshot_version")},
            )
        mode = "incremental"
        reasons = ["manifest requests incremental append"]
    elif package.get("base_snapshot_version") == current_version and unchanged_hashes:
        mode = "incremental"
        reasons = ["base version matches and unchanged shard hashes prove append/patch"]
    else:
        mode = "full"
        reasons = ["version advanced without a compatible incremental base/hash intersection"]
    return {
        "decision": "replace" if mode == "full" else "append",
        "mode": mode,
        "reasons": reasons,
        "unchanged_shard_hashes": unchanged_hashes,
        "changed_shard_hashes": sorted(incoming_hashes - current_hashes),
    }


def apply_package_to_view(view: Dict[str, Any], package: Dict[str, Any], decision: Dict[str, Any]) -> Dict[str, Any]:
    old_by_key = {entry["message_key"]: entry for entry in view["entries"]}
    incoming_by_key: Dict[str, Dict[str, Any]] = {}
    for raw in package["entries"]:
        candidate = _candidate_from_v2(raw)
        entry = {
            **raw,
            **candidate,
            "present": True,
            "source": "v2-package",
            "original_time": {"format": "v2_package_absolute_milliseconds", "effective_at_ms": raw["effective_at_ms"]},
            "adjudication": {
                "rule": "versioned_package_supersession",
                "winner_source": "v2-package",
                "reason": decision["decision"],
                "package_id": package["package_id"],
            },
        }
        entry["content_hash"] = canonical_hash(_entry_content(entry))
        incoming_by_key[raw["message_key"]] = entry
    if decision["mode"] == "full":
        new_keys = set(incoming_by_key)
        entries: List[Dict[str, Any]] = []
        for key, entry in incoming_by_key.items():
            event_like = old_by_key.get(key)
            if event_like is None:
                raise WorkbenchError(Code.SEMANTIC_KEY_UNKNOWN, "full package contains unknown key", {"message_key": key})
            entries.append({**entry, "topic": event_like["topic"], "partition": event_like["partition"], "offset": event_like["offset"], "present": True})
        for key, old in old_by_key.items():
            if key not in new_keys:
                entries.append({k: old[k] for k in ("message_key", "topic", "partition", "offset", "present", "content_hash")} | {"present": False})
    else:
        entries = []
        for key, old in old_by_key.items():
            if key in incoming_by_key:
                incoming = incoming_by_key[key]
                entries.append({**old, **incoming})
            else:
                entries.append(old)
    new_view = {
        **view,
        "source_bindings": {
            **view["source_bindings"],
            "current_package_id": package["package_id"],
            "current_snapshot_version": package["snapshot_version"],
            "current_generation_min": package["generation_min"],
            "current_manifest_hash": package["manifest_hash"],
        },
        "entries": sorted(entries, key=lambda item: item["message_key"]),
    }
    new_view["content_hash"] = canonical_hash({key: value for key, value in new_view.items() if key not in {"content_hash", "view_id"}})
    new_view["view_id"] = f"unified-{new_view['content_hash'][:16]}"
    return new_view


def diff_views(old_view: Optional[Dict[str, Any]], new_view: Dict[str, Any]) -> List[Dict[str, Any]]:
    old_by_key = {} if old_view is None else {entry["message_key"]: entry for entry in old_view["entries"]}
    changes: List[Dict[str, Any]] = []
    for new in sorted(new_view["entries"], key=lambda item: item["message_key"]):
        key = new["message_key"]
        old = old_by_key.get(key)
        if not new.get("present"):
            continue
        if old is None or not old.get("present"):
            kind = "added"
            reasons = ["semantic entry appeared"]
        elif old.get("content_hash") == new.get("content_hash"):
            continue
        else:
            reasons = []
            if not old.get("tombstone") and new.get("tombstone"):
                reasons.append("new_tombstone")
            if new.get("semantic_generation", 0) < old.get("semantic_generation", 0):
                reasons.append("generation_rollback")
            if _semantic_core_changed(old, new) and not reasons:
                reasons.append("semantic_content_changed")
            kind = "invalidated" if any(reason in {"new_tombstone", "generation_rollback"} for reason in reasons) else "modified"
            if not reasons:
                reasons.append("semantic_content_changed")
        changes.append(
            {
                "message_key": key,
                "topic": new["topic"],
                "partition": new["partition"],
                "offset": new["offset"],
                "change_kind": kind,
                "trigger_reasons": reasons,
                "old_content_hash": None if old is None else old.get("content_hash"),
                "new_content_hash": new.get("content_hash"),
                "old_generation": None if old is None else old.get("semantic_generation"),
                "new_generation": new.get("semantic_generation"),
                "trigger_semantic_version": new_view["source_bindings"].get("current_package_id")
                or f"v1+v2#{new_view['source_bindings']['v2_snapshot_version']}",
            }
        )
    return changes


def _semantic_core_changed(old: Dict[str, Any], new: Dict[str, Any]) -> bool:
    fields = ("effective_at_ms", "semantic_generation", "revision", "tombstone", "tombstone_reason", "dependencies")
    return any(old.get(field) != new.get(field) for field in fields)
