from __future__ import annotations

import json
from pathlib import Path

import pytest

from prefworkbench.canonical import canonical_bytes, read_json
from prefworkbench.diagnostics import build_diagnostics, write_dashboard
from prefworkbench.engine import run_pipeline
from prefworkbench.errors import WorkbenchError
from prefworkbench.selfcheck import run_selfcheck
from prefworkbench.store import Stage, StageLock


def _run(generated: dict, stage: Path, **kwargs):
    return run_pipeline(
        samples_path=generated["samples_path"],
        config_path=generated["config_path"],
        stage_dir=stage,
        mode=kwargs.pop("mode", "full"),
        **kwargs,
    )


def test_stage_busy_is_immediate(tmp_path: Path, generated: dict) -> None:
    stage = Stage(tmp_path / "stage")
    with StageLock(stage.root):
        with pytest.raises(WorkbenchError) as raised:
            _run(generated, stage.root)
    assert raised.value.code == "E_STAGE_BUSY"


def test_stale_base_version_cannot_replace_current(
    tmp_path: Path,
    generated: dict,
) -> None:
    stage = tmp_path / "stage"
    first = _run(generated, stage)
    second = run_pipeline(
        samples_path=generated["samples_path"],
        config_path=generated["config_path"],
        stage_dir=stage,
        mode="incremental",
    )
    with pytest.raises(WorkbenchError) as raised:
        run_pipeline(
            samples_path=generated["samples_path"],
            config_path=generated["config_path"],
            stage_dir=stage,
            mode="incremental",
            expected_base_version=first["version"],
        )
    assert raised.value.code == "E_STALE_VERSION"
    current = Stage(stage).current_manifest()
    assert current["version"] == second["version"]


def test_cache_payload_corruption_returns_E_CACHE_CORRUPT(
    tmp_path: Path,
    generated: dict,
) -> None:
    stage = tmp_path / "stage"
    manifest = _run(generated, stage)
    diag = manifest["nodes"]["quality_clean"]
    path = stage / diag["cache_object_path"]
    entry = read_json(path)
    entry["payload"]["pair_metrics"][0]["quality_score"] = 0.999999
    path.write_bytes(canonical_bytes(entry))
    with pytest.raises(WorkbenchError) as raised:
        run_pipeline(
            samples_path=generated["samples_path"],
            config_path=generated["config_path"],
            stage_dir=stage,
            mode="incremental",
        )
    error = raised.value
    assert error.code == "E_CACHE_CORRUPT"
    assert error.node == "quality_clean"
    assert error.version == diag["entry_version"]
    assert error.details["category"] == "CACHE_PAYLOAD_HASH_MISMATCH"


def test_manifest_corruption_returns_E_CACHE_CORRUPT(
    tmp_path: Path,
    generated: dict,
) -> None:
    stage = tmp_path / "stage"
    _run(generated, stage)
    current = stage / "current.json"
    current.write_bytes(current.read_bytes().replace(b'"version":1', b'"version":7'))
    with pytest.raises(WorkbenchError) as raised:
        Stage(stage).current_manifest()
    assert raised.value.code == "E_CACHE_CORRUPT"
    assert raised.value.node == "manifest"
    assert raised.value.version == 7


def test_selfcheck_detects_dataset_file_tampering(
    tmp_path: Path,
    generated: dict,
) -> None:
    stage = tmp_path / "stage"
    manifest = _run(generated, stage)
    assert run_selfcheck(stage, persist=False)["ok"]
    content_path = stage / manifest["dataset_records_path"]
    content_path.write_bytes(content_path.read_bytes() + b'{"tampered":true}\n')
    with pytest.raises(WorkbenchError) as raised:
        run_selfcheck(stage, persist=False)
    assert raised.value.code == "E_INCONSISTENT"
    assert "DATASET_RECORDS_FILE" in raised.value.details["categories"]


def test_topology_cycle_is_detected() -> None:
    from prefworkbench.graph import topology_is_acyclic

    ok, errors = topology_is_acyclic(["a", "b"], [("a", "b"), ("b", "a")])
    assert not ok
    assert errors


def test_diagnostics_payload_and_dashboard(tmp_path: Path, generated: dict) -> None:
    stage = tmp_path / "stage"
    manifest = _run(generated, stage)
    data = build_diagnostics(stage)
    assert data["ok"]
    assert data["manifest"]["version"] == manifest["version"]
    assert data["topology"]["edges"] == manifest["topology"]["edges"]
    for node in manifest["nodes"]:
        assert node in data["nodes"]
        assert data["nodes"][node]["key_components"]
    dashboard = write_dashboard(stage, tmp_path / "dashboard.html")
    html = dashboard.read_text(encoding="utf-8")
    assert "偏好数据增量诊断工作台" in html
    assert "quality_clean" in html
    assert "near_duplicate" in html
    assert "pair_consistency" in html
    assert "allocation_sampling" in html


def test_http_api_serves_diagnostics(tmp_path: Path, generated: dict) -> None:
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    from prefworkbench.server import make_handler

    stage = tmp_path / "stage"
    manifest = _run(generated, stage)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(stage))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        with urllib.request.urlopen(f"http://{host}:{port}/api/diagnostics") as response:
            data = json.loads(response.read().decode("utf-8"))
        assert data["manifest"]["version"] == manifest["version"]
        with urllib.request.urlopen(f"http://{host}:{port}/api/selfcheck") as response:
            selfcheck = json.loads(response.read().decode("utf-8"))
        assert selfcheck["ok"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
