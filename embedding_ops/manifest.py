"""JSONL manifest loading with deterministic duplicate detection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    E_DUP_JOB,
    E_MANIFEST_INVALID,
    business_error,
)
from .models import JobSpec
from .util import canonical_json_text, sha256_bytes


def _parse_lines(lines: Iterable[str], *, source: str) -> tuple[list[JobSpec], list[dict[str, Any]]]:
    specs: list[JobSpec] = []
    duplicate_errors: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for line_number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise business_error(
                E_MANIFEST_INVALID,
                "manifest",
                "unknown",
                f"invalid JSON at {source}:{line_number}",
                source=source,
                line=line_number,
                column=exc.colno,
            ) from exc
        spec = JobSpec.from_mapping(value)
        if spec.job_id in seen:
            duplicate_errors.append(
                {
                    "job_id": spec.job_id,
                    "code": E_DUP_JOB,
                    "stage": "manifest",
                    "message": (
                        f"duplicate job_id in manifest: {spec.job_id!r}"
                    ),
                    "detail": {
                        "first_line": seen[spec.job_id],
                        "duplicate_line": line_number,
                    },
                }
            )
            continue
        seen[spec.job_id] = line_number
        specs.append(spec)
    return specs, duplicate_errors


def load_manifest(
    path: str | Path,
    *,
    reject_duplicates: bool = True,
) -> list[JobSpec]:
    manifest_path = Path(path)
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise business_error(
            E_MANIFEST_INVALID,
            "manifest",
            "unknown",
            f"cannot read manifest: {manifest_path.name}",
            error=type(exc).__name__,
        ) from exc
    specs, duplicates = _parse_lines(text.splitlines(), source=manifest_path.name)
    if duplicates and reject_duplicates:
        first = duplicates[0]
        raise business_error(
            first["code"],
            first["stage"],
            first["job_id"],
            first["message"],
            **first["detail"],
        )
    return specs


def load_manifest_with_duplicates(
    path: str | Path,
) -> tuple[list[JobSpec], list[dict[str, Any]]]:
    manifest_path = Path(path)
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise business_error(
            E_MANIFEST_INVALID,
            "manifest",
            "unknown",
            f"cannot read manifest: {manifest_path.name}",
            error=type(exc).__name__,
        ) from exc
    return _parse_lines(text.splitlines(), source=manifest_path.name)


def write_manifest(path: str | Path, specs: Iterable[JobSpec]) -> str:
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        canonical_json_text(spec.to_dict()) for spec in specs
    ]
    text = "\n".join(lines) + ("\n" if lines else "")
    manifest_path.write_text(text, encoding="utf-8", newline="\n")
    return sha256_bytes(text.encode("utf-8"))
