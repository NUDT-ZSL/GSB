from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from serverless_engine.engine import Engine
from serverless_engine.errors import EngineError
from serverless_engine.util import stable_json

from conftest import SAMPLES, db_rows


def outcomes(result: dict) -> list[tuple[int, str, str | None]]:
    return [
        (call["call_seq"], call["outcome"], call["reason_code"])
        for call in result["calls"]
    ]


def test_reuse_budget_abandonment_and_cold_start(loaded_engine: Engine) -> None:
    result = loaded_engine.invoke_trace(str(SAMPLES / "traces" / "before.json"))
    assert outcomes(result) == [
        (1, "REUSED_WARM", None),
        (2, "REUSED_WARM", None),
        (3, "BUDGET_NOT_STARTED", "E_FUNCTION_CONCURRENCY"),
        (4, "REUSED_WARM", None),
        (5, "BUDGET_NOT_STARTED", "E_GLOBAL_BUDGET"),
        (6, "COLD_START_SUCCEEDED", None),
        (7, "REUSED_WARM", None),
        (8, "REUSED_WARM", None),
        (9, "REUSED_WARM", None),
        (10, "REUSED_WARM", None),
    ]
    first = result["calls"][0]
    assert first["decision"]["precision_sensitive"] is True
    assert first["decision"]["error_source_chain"] is not None
    assert first["error_bounds"] == {
        "init_duration_ms": 0.0,
        "concurrency_occupancy": 0.0,
    }
    notes = db_rows(
        loaded_engine.store.db_path,
        "SELECT * FROM precision_notes ORDER BY id",
    )
    assert notes
    assert any(row["precision_sensitive"] == 1 for row in notes)
    assert all(json.loads(row["source_chain_json"]) for row in notes)


def test_layer_closure_invalidation_and_unaffected_reuse(loaded_engine: Engine) -> None:
    loaded_engine.invoke_trace(str(SAMPLES / "traces" / "before.json"))
    before_status = loaded_engine.status()
    before_basis = {
        instance["instance_id"]: instance["decision_basis_sha256"]
        for instance in before_status["instances"]
    }
    result = loaded_engine.apply_patch(str(SAMPLES / "patches" / "layer-update.json"))
    assert result["affected_functions"] == ["legacy-handler", "resize-image"]
    assert result["first_affected_instance"] == "inst-legacy-handler-g000002-000002"
    assert result["unaffected_instances_reused_without_rewrite"] is True
    assert result["affected_init_cache"]

    after_status = loaded_engine.status()
    by_id = {instance["instance_id"]: instance for instance in after_status["instances"]}
    assert by_id["inst-resize-image-g000002-000003"]["stage"] == "INVALIDATED"
    assert by_id["inst-resize-image-g000002-000004"]["stage"] == "INVALIDATED"
    assert by_id["inst-legacy-handler-g000002-000002"]["stage"] == "INVALIDATED"
    unaffected = [
        "inst-classify-text-g000002-000001",
        "inst-report-csv-g000003-000005",
    ]
    for instance_id in unaffected:
        assert by_id[instance_id]["stage"] == "WARMED"
        assert by_id[instance_id]["decision_basis_sha256"] == before_basis[instance_id]

    post = loaded_engine.invoke_trace(str(SAMPLES / "traces" / "after.json"))
    post_outcomes = outcomes(post)
    assert post_outcomes[0] == (11, "REUSED_WARM", None)
    assert post_outcomes[1] == (12, "REUSED_WARM", None)
    assert post_outcomes[2] == (13, "COLD_START_SUCCEEDED", None)
    assert post_outcomes[3] == (14, "FAILED_PRE_START", "E_LAYER_TOMBSTONED")
    assert post_outcomes[4] == (15, "REUSED_WARM", None)
    resize_instance = next(
        instance
        for instance in loaded_engine.status()["instances"]
        if instance["instance_id"] == post["calls"][2]["instance_id"]
    )
    assert resize_instance["bound_layer_versions"] == {
        "base-runtime": 3,
        "image-libs": 3,
    }
    assert loaded_engine.verify()["ok"] is True


def test_init_deadline_and_explicit_active_cancel(loaded_engine: Engine) -> None:
    deadline = loaded_engine.invoke_trace(str(SAMPLES / "traces" / "init-deadline.json"))
    call = deadline["calls"][0]
    assert call["outcome"] == "FAILED_AFTER_START"
    assert call["reason_code"] == "E_INIT_DEADLINE"
    assert call["init_started_clock_ms"] == 2000
    assert call["completed_clock_ms"] == 2002
    report = next(
        instance
        for instance in loaded_engine.status()["instances"]
        if instance["function"] == "report-csv"
    )
    assert report["stage"] == "INVALIDATED"
    assert report["invalidated_clock_ms"] == 2002

    second = Engine(loaded_engine.state_dir.parent / "cancel-state")
    second.load(
        str(SAMPLES / "functions.json"),
        str(SAMPLES / "layers.json"),
        str(SAMPLES / "snapshots" / "v1.json"),
        str(SAMPLES / "snapshots" / "v2" / "index.json"),
    )
    second.invoke_trace(str(SAMPLES / "traces" / "before.json"))
    cancelled = second.invoke_trace(str(SAMPLES / "traces" / "active-cancel.json"))
    assert cancelled["calls"][0]["outcome"] == "ABANDONED_ACTIVE"
    assert cancelled["calls"][0]["reason_code"] == "E_ACTIVE_CANCEL"
    assert cancelled["calls"][0]["started_clock_ms"] == 850
    assert cancelled["calls"][0]["completed_clock_ms"] == 860


def test_idempotent_calls_and_patch(loaded_engine: Engine) -> None:
    first = loaded_engine.invoke_trace(str(SAMPLES / "traces" / "before.json"))
    second = loaded_engine.invoke_trace(str(SAMPLES / "traces" / "before.json"))
    assert stable_json(first) == stable_json(second)
    call_count = db_rows(
        loaded_engine.store.db_path, "SELECT COUNT(*) AS n FROM calls"
    )[0]["n"]
    assert call_count == 10

    patch1 = loaded_engine.apply_patch(str(SAMPLES / "patches" / "layer-update.json"))
    patch2 = loaded_engine.apply_patch(str(SAMPLES / "patches" / "layer-update.json"))
    assert stable_json(patch1) == stable_json(patch2)
    patch_count = db_rows(
        loaded_engine.store.db_path, "SELECT COUNT(*) AS n FROM patches"
    )[0]["n"]
    assert patch_count == 1
    status = loaded_engine.status()
    assert status["config_version"] == 2


def test_version_rollback_is_rejected(loaded_engine: Engine) -> None:
    loaded_engine.apply_patch(str(SAMPLES / "patches" / "layer-update.json"))
    stale = json.loads(
        (SAMPLES / "patches" / "runtime-update.json").read_text(encoding="utf-8")
    )
    stale["base_config_version"] = 1
    stale["patch_id"] = "stale-base"
    path = loaded_engine.state_dir / "stale.json"
    path.write_text(json.dumps(stale), encoding="utf-8")
    try:
        loaded_engine.apply_patch(str(path))
        raise AssertionError("stale base version should have failed")
    except EngineError as exc:
        assert exc.code == "E_VERSION_ROLLBACK"
    assert loaded_engine.status()["config_version"] == 2
