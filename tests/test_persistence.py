from __future__ import annotations

import json
import hashlib
import sqlite3
import subprocess
import sys
import shutil
from pathlib import Path

import pytest

from deploy_convergence.errors import EngineError
from deploy_convergence.replacement import classify_change
from deploy_convergence.store import Store


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def test_logical_clock_rollback_keeps_view_unchanged(
    engine_factory,
    clock_ms: int,
) -> None:
    engine = engine_factory(state_name="clock.sqlite")
    baseline = engine.run(clock_ms=clock_ms)
    view_id = baseline.view["persisted"]["view_id"]
    watermark = Store(engine.state_path).status()["clock_watermark_ms"]

    with pytest.raises(EngineError) as raised:
        engine.run(clock_ms=clock_ms - 5000)
    assert raised.value.code == "E_CLOCK_ROLLBACK"
    assert raised.value.details["watermark_ms"] == watermark
    assert raised.value.details["rollback_by_ms"] == 5000

    current = Store(engine.state_path).get_current_view()
    assert current["persisted"]["view_id"] == view_id
    assert Store(engine.state_path).status()["clock_watermark_ms"] == watermark

    within = engine.run(clock_ms=clock_ms - 500)
    assert within.idempotent is True
    assert within.view["persisted"]["view_id"] == view_id
    assert within.view["submission"]["change_kind"] == "noop"


def test_crash_after_merge_replays_only_unfinished_steps(
    engine_factory,
    clock_ms: int,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "recover-merge.sqlite"
    command = [
        sys.executable,
        "-m",
        "deploy_convergence",
        "verify",
        "--state",
        str(state_path),
        "--crash-after",
        "merge",
    ]
    crashed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert crashed.returncode == 90
    status = Store(state_path).status()
    assert status["current_view"] is None
    assert status["clock_watermark_ms"] == 0
    assert [step["step_name"] for step in status["steps"]] == [
        "parse",
        "migrate",
        "merge",
    ]

    engine = engine_factory(state_name=state_path.name)
    result = engine.run(clock_ms=clock_ms, recover=True)
    assert result.recovered is True
    assert result.steps_replayed == ["parse", "migrate", "merge"]
    assert result.steps_executed == ["evaluate", "stage", "switch"]
    assert result.view["persisted"]["commit_seq"] == 1


def test_concurrent_submission_commits_once(
    tmp_path: Path,
    clock_ms: int,
) -> None:
    state_path = tmp_path / "concurrent.sqlite"
    outputs = [tmp_path / "concurrent-a.json", tmp_path / "concurrent-b.json"]
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "deploy_convergence",
                "verify",
                "--state",
                str(state_path),
                "--json-out",
                str(output),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for output in outputs
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, (stdout, stderr)
    results = [
        json.loads(output.read_text(encoding="utf-8")) for output in outputs
    ]
    assert sorted(result["idempotent"] for result in results) == [False, True]
    assert len({result["view"]["persisted"]["view_id"] for result in results}) == 1

    connection = sqlite3.connect(state_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM view_versions"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM operations WHERE status='complete'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT MAX(commit_seq) FROM view_versions"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_full_replacement_rollback_rejection_and_degrade(
    engine_factory,
    clock_ms: int,
) -> None:
    state_name = "replace.sqlite"
    base = engine_factory(state_name=state_name).run(clock_ms=clock_ms)
    assert base.view["change"]["change_kind"] == "initial"

    next_engine = engine_factory(
        state_name=state_name,
        v2_meta=SAMPLES / "v2_next" / "meta.sqlite",
        v2_shards=SAMPLES / "v2_next" / "shards",
    )
    next_result = next_engine.run(clock_ms=clock_ms + 1000)
    assert next_result.view["change"]["change_kind"] == "full_replace"
    assert next_result.view["persisted"]["commit_seq"] == 2
    assert next_result.view["consistency"]["consistent"] is True
    next_view_id = next_result.view["persisted"]["view_id"]

    with pytest.raises(EngineError) as rejected:
        engine_factory(state_name=state_name).run(clock_ms=clock_ms + 2000)
    assert rejected.value.code == "E_VERSION_ROLLBACK"
    assert rejected.value.details["decision"] == "reject"
    assert Store(engine_factory(state_name=state_name).state_path).get_current_view()[
        "persisted"
    ]["view_id"] == next_view_id

    degraded = engine_factory(
        state_name=state_name,
        rollback_policy="degrade",
    ).run(clock_ms=clock_ms + 3000)
    assert degraded.view["change"]["change_kind"] == "rollback_degraded"
    assert degraded.view["persisted"]["commit_seq"] == 3
    assert degraded.view["persisted"]["view_id"] != next_view_id
    connection = sqlite3.connect(
        engine_factory(state_name=state_name).state_path
    )
    try:
        for table in (
            "staging_versions",
            "staging_services",
            "staging_blobs",
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0] == 0
    finally:
        connection.close()


def test_incremental_package_appends_shards_without_version_bump(
    engine_factory,
    clock_ms: int,
    tmp_path: Path,
) -> None:
    incremental = tmp_path / "incremental-v2"
    shutil.copytree(SAMPLES / "v2", incremental)
    connection = sqlite3.connect(incremental / "meta.sqlite")
    try:
        for index, environment in enumerate(
            ("development", "staging", "production")
        ):
            generation = 17 + index
            shard_id = f"{environment}-search"
            relative_path = f"{shard_id}.json"
            payload = {
                "shard_id": shard_id,
                "manifest_version": "2.1.0",
                "generation": generation,
                "environment": environment,
                "services": [
                    {
                        "name": "search-api",
                        "effective_at_ms": clock_ms,
                        "generation": generation,
                        "params": {
                            "image_tag": "registry.local/search-api:1.0.0",
                            "replicas": 2,
                            "timeout_ms": 400,
                            "retry_factor": 1.4,
                            "resource_limit": {
                                "cpu_millicores": 300,
                                "memory_mb": 512,
                            },
                            "strategy": "rolling",
                            "max_burst": 2,
                        },
                        "upstreams": ["auth-service"],
                        "ttl_ms": 172800000,
                        "tombstone": False,
                    }
                ],
            }
            encoded = (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
            (incremental / "shards" / relative_path).write_bytes(encoded)
            connection.execute(
                """
                INSERT INTO shards(
                    shard_id, relative_path, digest,
                    generation, environment, priority
                ) VALUES (?, ?, ?, ?, ?, 10)
                """,
                (
                    shard_id,
                    relative_path,
                    "sha256:" + hashlib.sha256(encoded).hexdigest(),
                    generation,
                    environment,
                ),
            )
        connection.commit()
    finally:
        connection.close()

    state_name = "incremental.sqlite"
    base = engine_factory(state_name=state_name).run(clock_ms=clock_ms)
    assert base.view["change"]["change_kind"] == "initial"
    incremental_result = engine_factory(
        state_name=state_name,
        v2_meta=incremental / "meta.sqlite",
        v2_shards=incremental / "shards",
    ).run(clock_ms=clock_ms + 1000)
    assert incremental_result.view["change"]["change_kind"] == "incremental"
    assert incremental_result.view["persisted"]["commit_seq"] == 2
    assert (
        incremental_result.view["environments"]["development"]["search-api"][
            "params"
        ]["timeout_ms"]["value"]
        == 400
    )


def test_staged_replacement_keeps_pointer_until_atomic_switch(
    engine_factory,
    clock_ms: int,
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "staged-replace.sqlite"
    base = engine_factory(state_name=state_path.name).run(clock_ms=clock_ms)
    base_view_id = base.view["persisted"]["view_id"]

    crashed = subprocess.run(
        [
            sys.executable,
            "-m",
            "deploy_convergence",
            "verify",
            "--state",
            str(state_path),
            "--v2-meta",
            str(SAMPLES / "v2_next" / "meta.sqlite"),
            "--v2-shards",
            str(SAMPLES / "v2_next" / "shards"),
            "--crash-after",
            "stage",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert crashed.returncode == 90
    connection = sqlite3.connect(state_path)
    try:
        current_view_id = connection.execute(
            "SELECT value FROM state WHERE key='current_view_id'"
        ).fetchone()[0]
        assert current_view_id == base_view_id
        assert connection.execute(
            "SELECT COUNT(*) FROM view_versions"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM staging_versions"
        ).fetchone()[0] == 1
    finally:
        connection.close()

    next_engine = engine_factory(
        state_name=state_path.name,
        v2_meta=SAMPLES / "v2_next" / "meta.sqlite",
        v2_shards=SAMPLES / "v2_next" / "shards",
    )
    recovered = next_engine.run(clock_ms=clock_ms, recover=True)
    assert recovered.recovered is True
    assert recovered.steps_replayed[-1] == "stage"
    assert recovered.steps_executed == ["switch"]
    assert recovered.view["change"]["change_kind"] == "full_replace"
    assert recovered.view["persisted"]["commit_seq"] == 2


def test_replacement_classifier_covers_noop_incremental_and_full(
    clock_ms: int,
) -> None:
    current = {
        "manifest_version": "2.1.0",
        "generation_min": 11,
        "shard_hashes": {"a": "a1"},
        "parameter_digest": "p1",
        "input_digest": "i1",
        "committed_at_ms": clock_ms,
    }
    noop = classify_change(
        current,
        {
            "manifest_version": "2.1.0",
            "generation_min": 11,
            "shard_hashes": {"a": "a1"},
            "parameter_digest": "p1",
            "input_digest": "i1",
        },
        clock_ms=clock_ms,
        rollback_policy="reject",
        rollback_window_ms=300000,
    )
    assert noop["change_kind"] == "noop"

    changed_input_noop = classify_change(
        current,
        {
            "manifest_version": "2.1.0",
            "generation_min": 11,
            "shard_hashes": {"a": "a1"},
            "parameter_digest": "p1",
            "input_digest": "different",
        },
        clock_ms=clock_ms,
        rollback_policy="reject",
        rollback_window_ms=300000,
    )
    assert changed_input_noop["change_kind"] == "noop"

    incremental = classify_change(
        current,
        {
            "manifest_version": "2.1.0",
            "generation_min": 12,
            "shard_hashes": {"a": "a1", "b": "b1"},
            "parameter_digest": "p2",
            "input_digest": "i2",
        },
        clock_ms=clock_ms,
        rollback_policy="reject",
        rollback_window_ms=300000,
    )
    assert incremental["change_kind"] == "incremental"

    full = classify_change(
        current,
        {
            "manifest_version": "2.2.0",
            "generation_min": 21,
            "shard_hashes": {"a": "changed"},
            "parameter_digest": "p3",
            "input_digest": "i3",
        },
        clock_ms=clock_ms,
        rollback_policy="reject",
        rollback_window_ms=300000,
    )
    assert full["change_kind"] == "full_replace"

    with pytest.raises(EngineError) as generation:
        classify_change(
            current,
            {
                "manifest_version": "2.2.0",
                "generation_min": 10,
                "shard_hashes": {"a": "changed"},
                "parameter_digest": "p3",
                "input_digest": "i3",
            },
            clock_ms=clock_ms,
            rollback_policy="reject",
            rollback_window_ms=300000,
        )
    assert generation.value.code == "E_GENERATION_CONFLICT"
