"""Load deterministic sample files from a data directory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .codes import Code
from .domain import LAYOUT_VERSION, LEVEL_ENUM_VERSION
from .models import Dataset, Issue, issue
from .parsing import (
    normalize_group_edges,
    normalize_objects,
    normalize_sources,
    normalize_trustees,
    parse_batch_envelope,
)


def read_records_file(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        records: list[Any] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                records.append(
                    {
                        "__parse_error__": True,
                        "line_number": line_number,
                        "message": str(exc),
                    }
                )
        return records

    payload = json.loads(text)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        return payload["records"]
    return [payload]


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_dataset(data_dir: str | Path) -> Dataset:
    root = Path(data_dir)
    metadata_path = root / "metadata.json"
    metadata: dict[str, Any] = {}
    input_failures: list[Issue] = []
    if metadata_path.exists():
        try:
            loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                metadata = loaded
        except json.JSONDecodeError as exc:
            input_failures.append(
                issue(
                    Code.INVALID_JSON,
                    f"metadata.json is not valid JSON: {exc}",
                    source_name=str(metadata_path),
                )
            )

    layout_version = str(metadata.get("layout_version", LAYOUT_VERSION))
    level_enum_version = str(metadata.get("level_enum_version", LEVEL_ENUM_VERSION))
    evaluation_clock = int(metadata.get("evaluation_clock", 0) or 0)

    def records(name: str) -> tuple[list[Any], list[Issue]]:
        path = root / name
        if not path.exists():
            return [], []
        try:
            return read_records_file(path), []
        except (OSError, json.JSONDecodeError) as exc:
            return [], [
                issue(
                    Code.INVALID_JSON,
                    f"{name} could not be read: {exc}",
                    source_name=str(path),
                )
            ]

    raw_objects, object_read_issues = records("objects.json")
    raw_trustees, trustee_read_issues = records("trustees.json")
    raw_edges, edge_read_issues = records("group_edges.json")
    raw_sources, source_read_issues = records("sources.json")

    objects, object_issues = normalize_objects(raw_objects, "objects.json")
    trustees, trustee_issues = normalize_trustees(raw_trustees, "trustees.json")
    known_trustees = {item.trustee_id for item in trustees}
    edges, edge_issues = normalize_group_edges(raw_edges, known_trustees or None, "group_edges.json")
    known_objects = {item.object_key for item in objects}
    sources, source_issues = normalize_sources(raw_sources, known_objects, "sources.json")

    batches = []
    batch_issues: list[Issue] = []
    batch_dir = root / "batches"
    if batch_dir.exists():
        for path in sorted(batch_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                input_failures.append(
                    issue(
                        Code.INVALID_JSON,
                        f"batch file could not be read: {exc}",
                        batch_id=path.stem,
                        source_name=str(path),
                    )
                )
                continue
            batch, issues = parse_batch_envelope(raw, source_name=str(path))
            batch_issues.extend(issues)
            if batch is not None:
                batches.append(batch)

    issues: list[Issue] = []
    for group in (
        object_read_issues,
        trustee_read_issues,
        edge_read_issues,
        source_read_issues,
        object_issues,
        trustee_issues,
        edge_issues,
        source_issues,
        batch_issues,
    ):
        issues.extend(group)

    return Dataset(
        layout_version=layout_version,
        level_enum_version=level_enum_version,
        evaluation_clock=evaluation_clock,
        objects=objects,
        trustees=trustees,
        group_edges=edges,
        sources=sources,
        batches=batches,
        issues=issues,
        input_failures=input_failures,
    )


def load_truth(truth_path: str | Path) -> dict[str, Any]:
    path = Path(truth_path)
    return _read_json(path, {})
