from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from .canonical import digest_json
from .canonical import canonical_json
from .errors import VERIFY_001, error
from .samples import generate_samples
from .validation import normalize_scenario, validate_patch_document


def verify_samples(root: str | Path) -> dict[str, Any]:
    sample_root = Path(root)
    manifest_path = sample_root / "manifest.json"
    if not manifest_path.exists():
        raise error(
            VERIFY_001,
            "sample manifest is missing",
            details={"path": str(manifest_path)},
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checks: list[dict[str, Any]] = []
    for entry in manifest.get("files", []):
        path = sample_root / entry["path"]
        data = path.read_bytes()
        observed = "sha256:" + hashlib.sha256(data).hexdigest()
        checks.append(
            {
                "path": entry["path"],
                "size_ok": len(data) == entry["size"],
                "sha256_ok": observed == entry["sha256"],
                "size": len(data),
                "sha256": observed,
            }
        )
    scenario = normalize_scenario(
        json.loads((sample_root / "scenario.json").read_text(encoding="utf-8"))
    )
    end_scenario = normalize_scenario(
        json.loads((sample_root / "end_stream_scenario.json").read_text(encoding="utf-8"))
    )
    main_patch = validate_patch_document(
        json.loads(
            (sample_root / "patches" / "settings-weight-v2.json").read_text(
                encoding="utf-8"
            )
        )
    )
    end_patch = validate_patch_document(
        json.loads(
            (sample_root / "patches" / "end-stream-v2.json").read_text(encoding="utf-8")
        )
    )
    main_frames = [
        json.loads(line)
        for line in (sample_root / "frames" / "main.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    end_frames = [
        json.loads(line)
        for line in (sample_root / "frames" / "end_stream.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    expected_manifest_digest = digest_json(
        {key: value for key, value in manifest.items() if key != "manifest_digest"}
    )
    with tempfile.TemporaryDirectory(prefix="h2diag-samples-") as temporary:
        regenerated = generate_samples(Path(temporary) / "samples")
    regeneration_ok = regenerated["manifest_digest"] == manifest.get("manifest_digest")
    ok = (
        all(item["size_ok"] and item["sha256_ok"] for item in checks)
        and expected_manifest_digest == manifest.get("manifest_digest")
        and regeneration_ok
        and manifest.get("scenario_digest") == digest_json(scenario)
        and manifest.get("end_stream_scenario_digest") == digest_json(end_scenario)
        and manifest.get("patch_settings_weight_digest") == main_patch["_computed_digest"]
        and manifest.get("patch_end_stream_digest") == end_patch["_computed_digest"]
        and [canonical_json(item) for item in main_frames]
        == [canonical_json(item) for item in scenario["events"]]
        and [canonical_json(item) for item in end_frames]
        == [canonical_json(item) for item in end_scenario["events"]]
    )
    return {
        "ok": ok,
        "root": str(sample_root),
        "file_count": len(checks),
        "files": checks,
        "scenario_digest": digest_json(scenario),
        "end_stream_scenario_digest": digest_json(end_scenario),
        "patch_settings_weight_digest": main_patch["_computed_digest"],
        "patch_end_stream_digest": end_patch["_computed_digest"],
        "manifest_digest": manifest.get("manifest_digest"),
        "deterministic_regeneration_ok": regeneration_ok,
        "deterministic_regeneration": digest_json(regenerated),
    }
