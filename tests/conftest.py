from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

import pytest

from colagg.canonical import canonical_dumps, sha256_json


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def cli(
    root: Path,
    *args: str,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "colagg.cli", *args, "--root", str(root)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def json_stdout(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.stdout.strip(), f"empty stdout; stderr={result.stderr}"
    return json.loads(result.stdout)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_dumps(payload) + "\n", encoding="utf-8")


def build_batch(
    batch_seq: int,
    columns: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    row_count = len(columns[0]["values"])
    for column in columns:
        assert len(column["values"]) == row_count
    payload = {
        "batch_seq": batch_seq,
        "row_count": row_count,
        "columns": list(columns),
    }
    return {**payload, "batch_digest": sha256_json(payload)}


def default_query(window_length: int = 4, window_stride: int = 2) -> dict[str, Any]:
    return {
        "query_id": "test-query",
        "group_by": ["site"],
        "aggregates": [
            {"name": "value_sum", "op": "sum", "column": "value"},
            {"name": "value_mean", "op": "mean", "column": "value"},
            {"name": "value_variance", "op": "variance", "column": "value"},
            {"name": "row_count", "op": "count", "column": "value"},
        ],
        "window": {
            "length": window_length,
            "stride": window_stride,
            "column": "value",
            "outputs": ["count", "sum", "mean", "variance"],
        },
    }


def default_config(
    *,
    mode: str = "fixed",
    budget_mib: int = 1,
    max_batch_rows: int = 1000,
    fixed_scale: int = 1000,
) -> dict[str, Any]:
    return {
        "memory_budget_mib": budget_mib,
        "max_batch_rows": max_batch_rows,
        "spill_dir": "spill",
        "fixed_scale": fixed_scale,
        "fixed_int_bits": 64,
        "float_tolerance": 1e-12,
        "logical_clock_step": 1,
        "numeric_mode": mode,
    }


def make_dataset(
    root: Path,
    batches: Sequence[dict[str, Any]],
    *,
    query: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    injections: Sequence[dict[str, Any]] = (),
) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    query_path = root / "query.json"
    config_path = root / "config.json"
    manifest_path = root / "injections.json"
    batches_dir = root / "batches"
    write_json(query_path, query or default_query())
    write_json(config_path, config or default_config())
    write_json(manifest_path, {"injections": list(injections)})
    for index, batch in enumerate(batches, start=1):
        write_json(batches_dir / f"batch-{index:04d}.json", batch)
    return {
        "root": root,
        "query": query_path,
        "config": config_path,
        "manifest": manifest_path,
        "batches": batches_dir,
    }


def load_dataset(
    dataset: dict[str, Path],
    *,
    mode: str | None = None,
    workspace: str = "workspace/colagg.sqlite3",
) -> subprocess.CompletedProcess[str]:
    args = [
        "load",
        "--workspace",
        workspace,
        "--query",
        str(dataset["query"]),
        "--config",
        str(dataset["config"]),
        "--manifest",
        str(dataset["manifest"]),
        "--batches-dir",
        str(dataset["batches"]),
    ]
    if mode is not None:
        args.extend(["--mode", mode])
    return cli(dataset["root"], *args)


def load_and_execute_samples(
    root: Path,
    *,
    manifest: str = "injections.json",
    mode: str | None = None,
) -> dict[str, Any]:
    load_args = [
        "load",
        "--query",
        str(SAMPLES / "query.json"),
        "--config",
        str(SAMPLES / "config.json"),
        "--manifest",
        str(SAMPLES / manifest),
        "--batches-dir",
        str(SAMPLES / "batches"),
    ]
    if mode is not None:
        load_args.extend(["--mode", mode])
    loaded = cli(root, *load_args)
    executed = cli(root, "execute", timeout=240)
    status = cli(root, "status")
    verified = cli(root, "verify")
    return {
        "root": root,
        "load": loaded,
        "execute": executed,
        "status": status,
        "verify": verified,
    }


@pytest.fixture(scope="session")
def clean_fixed_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("clean-fixed")
    return load_and_execute_samples(root)


@pytest.fixture(scope="session")
def clean_fixed_run_repeat(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("clean-fixed-repeat")
    return load_and_execute_samples(root)


@pytest.fixture(scope="session")
def clean_float_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("clean-float")
    return load_and_execute_samples(root, mode="float")


@pytest.fixture(scope="session")
def clean_float_run_repeat(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("clean-float-repeat")
    return load_and_execute_samples(root, mode="float")


@pytest.fixture(scope="session")
def anomaly_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("anomaly")
    return load_and_execute_samples(root, manifest="injections-anomalies.json")
