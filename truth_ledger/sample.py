from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .generator import build_deterministic_events, generate_csr, generate_rhs, get_config
from .hashing import write_json_atomic
from .ingest import validate_csr
from .migrate import migrate_three_views
from .workspace import CaseWorkspace


FIXED_SEED = 20261127


def generate_sample_case(
    root: str | Path,
    *,
    case_id: str = "case-001",
    seed: int = FIXED_SEED,
    config: dict[str, Any] | None = None,
) -> CaseWorkspace:
    ws = CaseWorkspace(root)
    if ws.root.exists():
        shutil.rmtree(ws.root)
    ws.ensure_dirs()
    cfg = get_config(config)
    matrix = generate_csr(case_id, seed, cfg)
    validate_csr(matrix, stage="sample_generation")
    ws.save_initial_csr(matrix)

    dense_b, sparse_b = generate_rhs(seed, cfg, case_id=case_id)
    ws.save_rhs(dense_b, sparse_b)

    ledger_paths = migrate_three_views(matrix, ws.initial_ledgers_dir)
    events = build_deterministic_events(matrix, seed)
    event_descriptors: list[dict[str, Any]] = []
    for index, event in enumerate(events, start=1):
        event_path = ws.events_dir / f"write-{index:03d}.json"
        write_json_atomic(event_path, event)
        event_descriptors.append(
            {
                "write_version": event["write_version"],
                "path": str(event_path),
                "interval_count": len(event["writes"]),
            }
        )

    manifest = {
        "task": "multi-format sparse truth ledger",
        "case_id": case_id,
        "seed": seed,
        "config": cfg,
        "nrows": matrix.nrows,
        "ncols": matrix.ncols,
        "nnz": matrix.nnz,
        "content_hash": matrix.digest,
        "row_intervals": [
            {"interval_id": matrix.interval_id(start, end), "row_start": start, "row_end": end}
            for start, end in matrix.interval_bounds()
        ],
        "formats": {
            "v1": str(ledger_paths[1]),
            "v2": str(ledger_paths[2]),
            "v3": str(ledger_paths[3]),
        },
        "events": event_descriptors,
    }
    write_json_atomic(ws.manifest_path, manifest)
    return ws


def generate_task_workspace(
    root: str | Path,
    *,
    case_count: int = 2,
    seed: int = FIXED_SEED,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(root)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    cases: list[dict[str, Any]] = []
    for index in range(case_count):
        case_id = f"case-{index + 1:03d}"
        case_seed = seed + index * 10_000
        case_root = root / "cases" / case_id
        ws = generate_sample_case(
            case_root, case_id=case_id, seed=case_seed, config=config
        )
        cases.append(
            {
                "case_id": case_id,
                "seed": case_seed,
                "path": str(case_root),
                "manifest": str(ws.manifest_path),
            }
        )
    task_manifest = {
        "task": "CSR multi-format truth ledger deterministic task list",
        "fixed_seed": seed,
        "case_count": case_count,
        "cases": cases,
    }
    write_json_atomic(root / "task_manifest.json", task_manifest)
    return task_manifest


def copy_case_for_run(source: str | Path, destination: str | Path) -> Path:
    source = Path(source)
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    return destination
