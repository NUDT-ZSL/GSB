"""Deterministic local sample generation without network or external datasets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Sequence

from .canonical import (
    atomic_write_json,
    parse_timestamp_ms,
    require_field,
    require_int,
    require_mapping,
    require_string,
    sha256_file,
)
from .config_io import write_v1_config, write_v2_config
from .errors import corrupt
from .migration import downgrade_v2_to_v1
from .models import InferenceRequest, ThresholdConfig, ThresholdEntry


DEFAULT_SEED = 20261127
PROMPT_WORDS = (
    "coastal",
    "signal",
    "ledger",
    "lantern",
    "matrix",
    "harbor",
    "cipher",
    "orchard",
    "vector",
    "bridge",
    "quartz",
    "meadow",
    "circuit",
    "archive",
    "compass",
    "prism",
)


def _u32_stream(seed: int, lane: str, count: int) -> list[int]:
    values: list[int] = []
    block = 0
    while len(values) < count:
        digest = hashlib.sha256(
            f"{seed}:{lane}:{block}".encode("utf-8")
        ).digest()
        values.extend(
            int.from_bytes(digest[offset : offset + 4], "big")
            for offset in range(0, len(digest), 4)
        )
        block += 1
    return values[:count]


def generate_requests(seed: int, count: int) -> tuple[InferenceRequest, ...]:
    if count <= 0:
        raise corrupt("request count must be positive")
    words = _u32_stream(seed, "prompt", count * 6)
    image_seeds = _u32_stream(seed, "image", count)
    text_revisions = _u32_stream(seed, "text-revision", count)
    image_revisions = _u32_stream(seed, "image-revision", count)
    requests: list[InferenceRequest] = []
    for index in range(count):
        prompt = " ".join(
            PROMPT_WORDS[words[index * 6 + offset] % len(PROMPT_WORDS)]
            for offset in range(6)
        )
        prompt = f"{prompt} request {index:04d}"
        requests.append(
            InferenceRequest(
                request_id=f"req-{index:04d}",
                prompt_text=prompt,
                image_seed=image_seeds[index] & 0x7FFFFFFF,
                text_revision=text_revisions[index] % 4,
                image_revision=image_revisions[index] % 4,
            )
        )
    return tuple(requests)


def evolve_requests(
    requests: Sequence[InferenceRequest],
    seed: int,
) -> tuple[InferenceRequest, ...]:
    evolved = list(requests)
    if evolved:
        index = min(4, len(evolved) - 1)
        evolved[index] = replace(
            evolved[index],
            text_revision=evolved[index].text_revision + 1,
        )
    if len(evolved) > 9:
        evolved[9] = replace(
            evolved[9],
            image_revision=evolved[9].image_revision + 1,
        )
    extras = generate_requests(seed + 1, 2)
    next_index = len(evolved)
    for offset, extra in enumerate(extras):
        evolved.append(
            replace(extra, request_id=f"req-{next_index + offset:04d}")
        )
    return tuple(evolved)


def write_requests(path: str | Path, requests: Iterable[InferenceRequest]) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            request.input_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for request in sorted(requests, key=lambda item: item.request_id)
    ]
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination


def read_requests(path: str | Path) -> tuple[InferenceRequest, ...]:
    source = Path(path)
    requests: list[InferenceRequest] = []
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise corrupt("request stream cannot be read", path=str(source)) from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise corrupt(
                "request JSONL line cannot be parsed",
                path=str(source),
                line=line_number,
            ) from exc
        record = require_mapping(value, f"{source}:{line_number}")
        requests.append(
            InferenceRequest(
                request_id=require_string(
                    require_field(record, "request_id", f"{source}:{line_number}"),
                    f"{source}:{line_number}.request_id",
                ),
                prompt_text=require_string(
                    require_field(record, "prompt_text", f"{source}:{line_number}"),
                    f"{source}:{line_number}.prompt_text",
                    nonempty=False,
                ),
                image_seed=require_int(
                    require_field(record, "image_seed", f"{source}:{line_number}"),
                    f"{source}:{line_number}.image_seed",
                    minimum=0,
                ),
                text_revision=require_int(
                    require_field(record, "text_revision", f"{source}:{line_number}"),
                    f"{source}:{line_number}.text_revision",
                    minimum=0,
                ),
                image_revision=require_int(
                    require_field(record, "image_revision", f"{source}:{line_number}"),
                    f"{source}:{line_number}.image_revision",
                    minimum=0,
                ),
            )
        )
    if len({request.request_id for request in requests}) != len(requests):
        raise corrupt("request stream contains duplicate request_id", path=str(source))
    return tuple(requests)


def build_v1_document() -> dict[str, Any]:
    return {
        "schema": "mm-threshold-config",
        "format_version": 1,
        "config": {
            "thresholds": {
                "text": 0.52,
                "image": 0.56,
                "joint": 0.58,
            },
            "route_budget": 250000,
            "candidate_plan_count": 4,
            "scheduler_workers": 3,
            "precision": "fp32",
            "updated_at": "2026-11-20T08:00:00.123Z",
        },
    }


def build_v2_config(previous_updated_at_ms: int) -> ThresholdConfig:
    created_at_ms = parse_timestamp_ms("2026-11-21T07:00:00.000Z", "sample.created_at")
    updated_at_ms = parse_timestamp_ms("2026-11-21T08:00:00.456Z", "sample.updated_at")
    if updated_at_ms < previous_updated_at_ms:
        raise corrupt("generated v2 timestamp rolls backwards")
    return ThresholdConfig(
        schema_version=2,
        generation=2,
        revision=7,
        created_at_ms=created_at_ms,
        updated_at_ms=updated_at_ms,
        entries=(
            ThresholdEntry(
                key="image",
                threshold=0.60,
                revision=6,
                updated_at_ms=updated_at_ms - 2000,
            ),
            ThresholdEntry(
                key="joint",
                threshold=0.63,
                revision=5,
                updated_at_ms=updated_at_ms - 1000,
            ),
            ThresholdEntry(
                key="text",
                threshold=0.57,
                revision=7,
                updated_at_ms=updated_at_ms,
            ),
        ),
        tombstones=(),
        route_budget=250000,
        candidate_plan_count=5,
        scheduler_workers=3,
        precision="fp32",
    )


def generate_sample(
    root: str | Path,
    *,
    seed: int = DEFAULT_SEED,
    request_count: int = 16,
) -> dict[str, Any]:
    from .config_io import read_config
    from .migration import migrate_v1_to_v2

    destination = Path(root)
    destination.mkdir(parents=True, exist_ok=True)
    requests = generate_requests(seed, request_count)
    evolved = evolve_requests(requests, seed)
    requests_path = write_requests(destination / "requests.jsonl", requests)
    evolved_path = write_requests(destination / "requests_evolved.jsonl", evolved)
    v1_document = build_v1_document()
    v1_path = write_v1_config(v1_document, destination / "config_v1.json")
    v1_load = read_config(v1_path)
    upgrade_record = migrate_v1_to_v2(v1_document)
    upgraded_v2_dir = write_v2_config(
        upgrade_record.config,
        destination / "config_v1_upgraded_v2",
    )
    v2_config = build_v2_config(v1_load.config.updated_at_ms)
    v2_dir = write_v2_config(
        v2_config,
        destination / "config_v2",
        previous_updated_at_ms=v1_load.config.updated_at_ms,
    )
    downgrade = downgrade_v2_to_v1(v2_config)
    assert downgrade.document is not None
    downgraded_v1_path = write_v1_config(
        downgrade.document,
        destination / "config_v2_downgraded_v1.json",
    )
    migration_report = {
        "seed": seed,
        "request_count": len(requests),
        "evolved_request_count": len(evolved),
        "v1_upgrade": upgrade_record.to_dict(),
        "v2_downgrade": downgrade.to_dict(),
    }
    migration_report_path = destination / "migration_report.json"
    atomic_write_json(migration_report_path, migration_report)
    manifest = {
        "schema": "mm-router-sample",
        "seed": seed,
        "request_count": len(requests),
        "evolved_request_count": len(evolved),
        "files": {
            "requests": {
                "path": requests_path.name,
                "sha256": sha256_file(requests_path),
            },
            "requests_evolved": {
                "path": evolved_path.name,
                "sha256": sha256_file(evolved_path),
            },
            "config_v1": {
                "path": v1_path.name,
                "sha256": sha256_file(v1_path),
            },
            "config_v2_manifest": {
                "path": str((v2_dir / "manifest.json").relative_to(destination)),
                "sha256": sha256_file(v2_dir / "manifest.json"),
            },
            "upgraded_v2_manifest": {
                "path": str(
                    (upgraded_v2_dir / "manifest.json").relative_to(destination)
                ),
                "sha256": sha256_file(upgraded_v2_dir / "manifest.json"),
            },
            "downgraded_v1": {
                "path": downgraded_v1_path.name,
                "sha256": sha256_file(downgraded_v1_path),
            },
            "migration_report": {
                "path": migration_report_path.name,
                "sha256": sha256_file(migration_report_path),
            },
        },
    }
    manifest_path = destination / "sample_manifest.json"
    atomic_write_json(manifest_path, manifest)
    return {
        "root": str(destination),
        "manifest": str(manifest_path),
        "seed": seed,
        "requests": str(requests_path),
        "evolved_requests": str(evolved_path),
        "config_v1": str(v1_path),
        "config_v2": str(v2_dir),
        "upgraded_v2": str(upgraded_v2_dir),
        "downgraded_v1": str(downgraded_v1_path),
        "migration_report": str(migration_report_path),
    }

