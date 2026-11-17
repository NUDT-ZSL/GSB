"""Batch loading, concurrency, deterministic assembly, and diagnostics."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .engine import EngineOptions, analyze_dataset
from .model import BUSINESS_CODE_ORDER, sort_codes


class BatchFormatError(ValueError):
    pass


@dataclass(frozen=True)
class BatchOptions:
    concurrency: int = 4
    timeout_ms: int = 2000
    max_fields: int = 10000

    def __post_init__(self) -> None:
        if not isinstance(self.concurrency, int) or self.concurrency <= 0:
            raise ValueError("concurrency must be a positive integer")
        EngineOptions(timeout_ms=self.timeout_ms, max_fields=self.max_fields)


@dataclass(frozen=True)
class DatasetSource:
    dataset: dict[str, Any]
    source_path: str


def _dataset_id(dataset: dict[str, Any]) -> str:
    value = dataset.get("dataset_id", dataset.get("id", "dataset"))
    return value if isinstance(value, str) and value else str(value)


def _load_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_batch(input_path: str | Path) -> list[DatasetSource]:
    """Load one dataset, a JSON array, or a manifest with dataset entries."""

    path = Path(input_path).resolve()
    if not path.is_file():
        raise BatchFormatError(f"input file does not exist: {path}")
    try:
        document = _load_json_file(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchFormatError(f"cannot parse input JSON: {exc}") from exc

    entries: list[Any]
    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict):
        if isinstance(document.get("datasets"), list):
            entries = document["datasets"]
        elif isinstance(document.get("dataset_files"), list):
            entries = document["dataset_files"]
        elif "records" in document:
            entries = [document]
        else:
            raise BatchFormatError(
                "input object must contain records, datasets, or dataset_files"
            )
    else:
        raise BatchFormatError("input JSON must be an object or array")

    sources: list[DatasetSource] = []
    for entry in entries:
        if isinstance(entry, str):
            dataset_path = (path.parent / entry).resolve()
            try:
                dataset = _load_json_file(dataset_path)
            except (OSError, json.JSONDecodeError) as exc:
                raise BatchFormatError(
                    f"cannot load dataset file {dataset_path}: {exc}"
                ) from exc
            if not isinstance(dataset, dict):
                raise BatchFormatError(
                    f"dataset file must contain an object: {dataset_path}"
                )
            sources.append(DatasetSource(dataset, str(dataset_path)))
        elif isinstance(entry, dict):
            sources.append(DatasetSource(entry, str(path)))
        else:
            raise BatchFormatError("dataset entries must be objects or file paths")
    return sources


def _quote_command_part(value: str) -> str:
    return subprocess.list2cmdline([value])


def _slug(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in value
    )
    return cleaned.strip("._") or "dataset"


def _diagnostic_commands(
    dataset_id: str,
    result: dict[str, Any],
    *,
    input_path: str,
    report_path: str,
    options: BatchOptions,
) -> list[str]:
    report_dir = Path(report_path).resolve().parent
    dataset_report = report_dir / f"dataset-{_slug(dataset_id)}.json"
    base_arguments = [
        "python -m sensitive_guard validate",
        "--input",
        _quote_command_part(str(Path(input_path).resolve())),
        "--dataset-id",
        _quote_command_part(dataset_id),
        "--report",
        _quote_command_part(str(dataset_report)),
        "--concurrency 1",
        f"--timeout-ms {options.timeout_ms}",
        f"--max-fields {options.max_fields}",
    ]
    rerun = " ".join(base_arguments)
    commands = [rerun]
    codes = set(result.get("codes", []))

    if "E_TIMEOUT" in codes:
        larger_timeout = max(options.timeout_ms * 2, options.timeout_ms + 1000)
        relaxed = base_arguments.copy()
        relaxed[-2] = f"--timeout-ms {larger_timeout}"
        commands.append(" ".join(relaxed))
    if "E_FIELDS" in codes:
        larger_limit = max(
            int(result.get("field_count", 0)),
            options.max_fields * 2,
        )
        relaxed = base_arguments.copy()
        relaxed[-1] = f"--max-fields {larger_limit}"
        commands.append(" ".join(relaxed))

    focus_tests: list[str] = []
    if "E_CYCLE" in codes:
        focus_tests.append("cycles")
    if "E_VERSION_CONFLICT" in codes:
        focus_tests.append("version_conflict")
    if codes.intersection({"E_DOWNGRADE", "E_LEAK"}):
        focus_tests.append("downgrade_and_leak")
    if "E_RECORD" in codes:
        focus_tests.append("invalid_records")
    for focus in focus_tests:
        commands.append(
            "python -m pytest -q tests/test_engine.py "
            f"-k {_quote_command_part(focus)}"
        )
    return commands


def _fatal_result(dataset: dict[str, Any], exc: Exception) -> dict[str, Any]:
    dataset_id = _dataset_id(dataset)
    records = dataset.get("records")
    return {
        "dataset_id": dataset_id,
        "status": "failed",
        "sync_marker": dataset.get("sync_marker"),
        "computed_sync_marker": None,
        "sync_marker_ok": False,
        "field_count": len(records) if isinstance(records, list) else 0,
        "processed_field_count": 0,
        "codes": ["E_INTERNAL"],
        "rejected_records": [],
        "duplicate_edges": [],
        "cycles": [],
        "violations": [],
        "fields": [],
        "injection": dataset.get("injection", {}),
        "observations": {
            "checked": 0,
            "consistent": 0,
            "inconsistent": 0,
            "checks": [],
        },
        "internal_error": {"type": type(exc).__name__, "message": str(exc)},
    }


def _analyze_worker(
    source: DatasetSource,
    options: EngineOptions,
    *,
    input_path: str,
    report_path: str,
    batch_options: BatchOptions,
) -> dict[str, Any]:
    dataset_id = _dataset_id(source.dataset)
    try:
        result = analyze_dataset(source.dataset, options)
    except Exception as exc:  # defensive batch isolation
        result = _fatal_result(source.dataset, exc)
    result["source_path"] = source.source_path
    result["diagnostics"] = {
        "commands": _diagnostic_commands(
            dataset_id,
            result,
            input_path=input_path,
            report_path=report_path,
            options=batch_options,
        )
    }
    return result


def _code_counts(datasets: Iterable[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for dataset in datasets:
        counter.update(dataset.get("codes", []))
    counts = {code: counter[code] for code in sort_codes(counter)}
    return counts


def run_batch(
    sources: list[DatasetSource],
    *,
    input_path: str | Path,
    report_path: str | Path,
    options: BatchOptions | None = None,
) -> dict[str, Any]:
    """Analyze sources concurrently while preserving source order."""

    if options is None:
        options = BatchOptions()
    engine_options = EngineOptions(
        timeout_ms=options.timeout_ms,
        max_fields=options.max_fields,
    )
    input_absolute = str(Path(input_path).resolve())
    report_absolute = str(Path(report_path).resolve())

    def worker(source: DatasetSource) -> dict[str, Any]:
        return _analyze_worker(
            source,
            engine_options,
            input_path=input_absolute,
            report_path=report_absolute,
            batch_options=options,
        )

    with ThreadPoolExecutor(max_workers=options.concurrency) as executor:
        datasets = list(executor.map(worker, sources))

    success_count = sum(1 for dataset in datasets if dataset["status"] == "ok")
    observation_consistent = sum(
        dataset["observations"]["consistent"] for dataset in datasets
    )
    observation_inconsistent = sum(
        dataset["observations"]["inconsistent"] for dataset in datasets
    )
    all_success = success_count == len(datasets)
    summary = {
        "dataset_count": len(datasets),
        "success_count": success_count,
        "failure_count": len(datasets) - success_count,
        "all_success": all_success,
        "observation_consistent": observation_consistent,
        "observation_inconsistent": observation_inconsistent,
        "code_counts": _code_counts(datasets),
        "failed_dataset_ids": [
            dataset["dataset_id"]
            for dataset in datasets
            if dataset["status"] != "ok"
        ],
    }

    return {
        "schema_version": 1,
        "engine": {
            "name": "sensitive_guard",
            "version": __version__,
            "business_codes": list(BUSINESS_CODE_ORDER),
        },
        "run": {
            "input": input_absolute,
            "report": report_absolute,
            "concurrency": options.concurrency,
            "timeout_ms": options.timeout_ms,
            "max_fields": options.max_fields,
        },
        "summary": summary,
        "diagnostics": {
            "commands": [
                "python -m pytest -q",
                (
                    "python -m sensitive_guard validate --input "
                    f"{_quote_command_part(input_absolute)} --report "
                    f"{_quote_command_part(report_absolute)} "
                    f"--concurrency 1 --timeout-ms {options.timeout_ms} "
                    f"--max-fields {options.max_fields}"
                ),
            ]
        },
        "datasets": datasets,
    }


def filter_sources(
    sources: list[DatasetSource], dataset_ids: list[str] | None
) -> list[DatasetSource]:
    if not dataset_ids:
        return sources
    wanted = set(dataset_ids)
    return [
        source
        for source in sources
        if _dataset_id(source.dataset) in wanted
    ]
