"""Import and normalize all offline inputs into one canonical bundle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

from .constants import (
    CREDENTIAL_VERSION,
    POLICY_ALGEBRA_VERSION,
    PROT_CALIBER_VERSION,
    UsageError,
)
from .parsers import (
    load_json,
    parse_event_stream,
    parse_observation_clock,
    parse_regions,
    parse_v1_file,
    parse_v2_manifest,
    rejection,
    CODE_JSON,
)
from .policy import build_policy_view


DEFAULT_FILES = {
    "manifest": "manifest.json",
    "events": "events.jsonl",
    "regions": "regions.json",
    "v1": "policy_v1.json",
    "v2_manifest": "policy_v2/manifest.json",
}


def resolve_input_paths(
    sample_dir: Optional[Path] = None,
    *,
    manifest: Optional[Path] = None,
    events: Optional[Path] = None,
    regions: Optional[Path] = None,
    v1: Optional[Path] = None,
    v2_manifest: Optional[Path] = None,
) -> Dict[str, Path]:
    base = Path(sample_dir) if sample_dir else Path("samples/seed-400")
    paths = {
        "manifest": Path(manifest) if manifest else base / DEFAULT_FILES["manifest"],
        "events": Path(events) if events else base / DEFAULT_FILES["events"],
        "regions": Path(regions) if regions else base / DEFAULT_FILES["regions"],
        "v1": Path(v1) if v1 else base / DEFAULT_FILES["v1"],
        "v2_manifest": (
            Path(v2_manifest)
            if v2_manifest
            else base / DEFAULT_FILES["v2_manifest"]
        ),
    }
    return paths


def fingerprint_bundle(bundle: Dict[str, Any]) -> str:
    material = {
        "prot_caliber": PROT_CALIBER_VERSION,
        "credential_version": CREDENTIAL_VERSION,
        "policy_algebra": POLICY_ALGEBRA_VERSION,
        "clock": bundle.get("clock"),
        "events": bundle.get("events"),
        "regions": bundle.get("regions"),
        "policy_candidates": bundle.get("policy_candidates"),
    }
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def import_inputs(
    sample_dir: Optional[Path] = None,
    *,
    manifest_path: Optional[Path] = None,
    events_path: Optional[Path] = None,
    regions_path: Optional[Path] = None,
    v1_path: Optional[Path] = None,
    v2_manifest_path: Optional[Path] = None,
) -> Dict[str, Any]:
    paths = resolve_input_paths(
        sample_dir,
        manifest=manifest_path,
        events=events_path,
        regions=regions_path,
        v1=v1_path,
        v2_manifest=v2_manifest_path,
    )
    rejections = []

    manifest = load_json(paths["manifest"], "sample_manifest", rejections)
    if manifest is None:
        raise UsageError(
            "a valid sample manifest with the declared observation clock is required",
            details={"manifest": str(paths["manifest"]), "rejections": rejections},
        )
    try:
        clock = parse_observation_clock(manifest, rejections)
    except ValueError as exc:
        raise UsageError(
            str(exc),
            details={"manifest": str(paths["manifest"]), "rejections": rejections},
        ) from exc

    events = parse_event_stream(paths["events"], clock, rejections)
    regions = parse_regions(paths["regions"], clock, rejections)

    policy_candidates = []
    if paths["v1"].exists():
        policy_candidates.extend(parse_v1_file(paths["v1"], clock, rejections))
    else:
        rejections.append(
            rejection(
                "policy_v1_snapshot",
                CODE_JSON,
                "v1 policy file not found",
                record_ref=str(paths["v1"]),
            )
        )

    if paths["v2_manifest"].exists():
        v2_candidates, _ = parse_v2_manifest(paths["v2_manifest"], rejections)
        policy_candidates.extend(v2_candidates)
    else:
        rejections.append(
            rejection(
                "policy_v2_manifest",
                CODE_JSON,
                "v2 policy manifest not found",
                record_ref=str(paths["v2_manifest"]),
            )
        )

    policy_view = build_policy_view(policy_candidates)
    bundle = {
        "schema": "memguard-import-bundle-v1",
        "versions": {
            "prot_caliber": PROT_CALIBER_VERSION,
            "credential": CREDENTIAL_VERSION,
            "policy_algebra": POLICY_ALGEBRA_VERSION,
        },
        "paths": {key: str(value) for key, value in paths.items()},
        "clock": clock,
        "events": events,
        "regions": regions,
        "policy_candidates": policy_candidates,
        "policy_view": policy_view,
        "rejections": rejections,
        "counts": {
            "events_valid": len(events),
            "regions_valid": len(regions),
            "policy_candidates": len(policy_candidates),
            "policy_keys": len(policy_view.get("keys", {})),
            "policy_conflicts": len(policy_view.get("conflicts", [])),
            "rejections": len(rejections),
        },
        "partial": bool(rejections),
    }
    bundle["fingerprint"] = fingerprint_bundle(bundle)
    return bundle


def save_bundle(bundle: Dict[str, Any], state_dir: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "bundle.json"
    temporary = state_dir / "bundle.json.tmp"
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(bundle, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    with temporary.open("r", encoding="utf-8") as handle:
        json.load(handle)
    temporary.replace(path)
    return path


def load_bundle(state_dir: Path) -> Optional[Dict[str, Any]]:
    path = state_dir / "bundle.json"
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
