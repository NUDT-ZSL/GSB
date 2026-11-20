from __future__ import annotations

import json
import sqlite3
from pathlib import Path, PurePosixPath
from typing import Any

from .canonical import canonical_bytes, sha256_bytes, sha256_file, sha256_json
from .engine import PipelineEngine
from .errors import PipelineError, error
from .kernel import serial_aggregate
from .store import Store


def _check(
    checks: list[dict[str, Any]],
    name: str,
    ok: bool,
    *,
    task: str | None = None,
    code: str | None = None,
    detail: str | None = None,
) -> None:
    checks.append(
        {
            "check": name,
            "task": task,
            "ok": bool(ok),
            "code": code if not ok else None,
            "detail": detail if not ok else None,
        }
    )


def verify_pipeline(engine: PipelineEngine) -> dict[str, Any]:
    if not engine.state_db.exists():
        raise error(
            "E_STATE_NOT_FOUND",
            "state database does not exist",
            state_db=str(engine.state_db),
        )
    store = Store(engine.state_db, engine.cache_dir)
    store.initialize_readonly()
    checks: list[dict[str, Any]] = []
    try:
        run = store.get_run(engine.run_id)
        if run is None:
            raise error(
                "E_STATE_NOT_FOUND",
                "manifest has no persisted state",
                run_id=engine.run_id,
            )
        integrity = store.integrity_check()
        _check(
            checks,
            "sqlite_integrity",
            integrity == "ok",
            code="E_DIGEST_MISMATCH" if integrity != "ok" else None,
            detail=integrity,
        )
        rows = {
            str(row["task_name"]): row for row in store.tasks_for_run(engine.run_id)
        }
        if set(rows) != {task.name for task in engine.manifest.tasks}:
            missing = sorted({task.name for task in engine.manifest.tasks} - set(rows))
            extra = sorted(set(rows) - {task.name for task in engine.manifest.tasks})
            _check(
                checks,
                "task_set",
                False,
                code="E_VERIFY_FAILED",
                detail=f"missing={missing}, extra={extra}",
            )
        else:
            _check(checks, "task_set", True)

        for task in engine.manifest.topological_tasks:
            row = rows.get(task.name)
            if row is None:
                continue
            _check(
                checks,
                "task_phase",
                row["phase"] == "committed",
                task=task.name,
                code="E_VERIFY_FAILED",
                detail=f"phase={row['phase']}",
            )
            if row["phase"] != "committed":
                continue
            output_path = engine.workspace / Path(*PurePosixPath(task.output).parts)
            _check(
                checks,
                "output_exists",
                output_path.is_file(),
                task=task.name,
                code="E_DIGEST_MISMATCH",
                detail=str(output_path),
            )
            if not output_path.is_file():
                continue
            actual_output = sha256_file(output_path)
            _check(
                checks,
                "output_digest",
                actual_output == row["output_digest"],
                task=task.name,
                code="E_DIGEST_MISMATCH",
                detail=f"expected={row['output_digest']} actual={actual_output}",
            )
            try:
                output_data = output_path.read_bytes()
                output_json = json.loads(output_data.decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                output_json = None
            _check(
                checks,
                "output_json",
                isinstance(output_json, dict),
                task=task.name,
                code="E_DIGEST_MISMATCH",
                detail="output is not a JSON object",
            )
            cache_key = str(row["cache_key"] or "")
            cache_path = store.cache_path(cache_key)
            entry = store.get_cache(cache_key)
            _check(
                checks,
                "cache_metadata",
                entry is not None,
                task=task.name,
                code="E_CACHE_CORRUPT",
                detail=cache_key,
            )
            if entry is not None:
                _check(
                    checks,
                    "cache_state",
                    entry["state"] == "valid",
                    task=task.name,
                    code="E_CACHE_CORRUPT",
                    detail=str(entry["state"]),
                )
                _check(
                    checks,
                    "cache_version",
                    entry["version"] == row["version"],
                    task=task.name,
                    code="E_VERIFY_FAILED",
                    detail=f"cache={entry['version']} task={row['version']}",
                )
                _check(
                    checks,
                    "cache_file_exists",
                    cache_path.is_file(),
                    task=task.name,
                    code="E_CACHE_CORRUPT",
                    detail=str(cache_path),
                )
                if cache_path.is_file():
                    actual_cache = sha256_file(cache_path)
                    _check(
                        checks,
                        "cache_digest",
                        actual_cache == entry["result_digest"],
                        task=task.name,
                        code="E_DIGEST_MISMATCH",
                        detail=(
                            f"expected={entry['result_digest']} actual={actual_cache}"
                        ),
                    )
                try:
                    metadata_result = json.loads(entry["result_json"])
                except json.JSONDecodeError:
                    metadata_result = None
                _check(
                    checks,
                    "cache_metadata_digest",
                    isinstance(metadata_result, dict)
                    and sha256_json(metadata_result) == entry["result_digest"],
                    task=task.name,
                    code="E_DIGEST_MISMATCH",
                    detail="cache metadata result digest mismatch",
                )

            dependency_rows = {
                name: rows[name] for name in task.depends_on if name in rows
            }
            try:
                identity = engine._build_identity(store, task, dependency_rows)
            except PipelineError as exc:
                _check(
                    checks,
                    "input_digest",
                    False,
                    task=task.name,
                    code=exc.code,
                    detail=exc.message,
                )
                continue
            _check(
                checks,
                "task_version",
                row["version"] == identity.version,
                task=task.name,
                code="E_DIGEST_MISMATCH",
                detail=f"expected={identity.version} actual={row['version']}",
            )
            _check(
                checks,
                "cache_key",
                row["cache_key"] == identity.cache_key,
                task=task.name,
                code="E_DIGEST_MISMATCH",
                detail=f"expected={identity.cache_key} actual={row['cache_key']}",
            )
            try:
                environment = json.loads(row["environment_json"] or "{}")
                recomputed = serial_aggregate(
                    task,
                    identity.input_paths,
                    engine.workspace,
                    fixed_scale=engine.manifest.fixed_scale,
                    environment={
                        str(key): str(value) for key, value in environment.items()
                    },
                    kernel_version=engine.manifest.kernel_version,
                )
            except (PipelineError, json.JSONDecodeError, AttributeError) as exc:
                _check(
                    checks,
                    "deterministic_recompute",
                    False,
                    task=task.name,
                    code=getattr(exc, "code", "E_VERIFY_FAILED"),
                    detail=str(exc),
                )
                continue
            recomputed_digest = sha256_json(recomputed)
            _check(
                checks,
                "result_digest",
                recomputed_digest == row["result_digest"],
                task=task.name,
                code="E_DIGEST_MISMATCH",
                detail=(
                    f"expected={recomputed_digest} actual={row['result_digest']}"
                ),
            )
            expected_output = canonical_bytes(recomputed) + b"\n"
            if output_path.is_file():
                actual_bytes = output_path.read_bytes()
                _check(
                    checks,
                    "output_bytes",
                    actual_bytes == expected_output,
                    task=task.name,
                    code="E_DIGEST_MISMATCH",
                    detail="output bytes differ from deterministic recomputation",
                )
            output_rows = store._conn().execute(
                "SELECT * FROM outputs WHERE run_id=? AND task_name=?",
                (engine.run_id, task.name),
            ).fetchall()
            _check(
                checks,
                "persisted_output",
                len(output_rows) == 1
                and output_rows[0]["digest"] == row["output_digest"],
                task=task.name,
                code="E_DIGEST_MISMATCH",
                detail="persisted output digest mismatch",
            )

        failed = [item for item in checks if not item["ok"]]
        return {
            "schema": "pipeline-verify-v1",
            "valid": not failed,
            "run_id": engine.run_id,
            "manifest_digest": engine.manifest.raw_digest,
            "pipeline_status": run["status"],
            "logical_clock_watermark": int(run["logical_watermark"]),
            "checks": checks,
            "errors": [
                {
                    "check": item["check"],
                    "task": item["task"],
                    "code": item["code"],
                    "detail": item["detail"],
                }
                for item in failed
            ],
        }
    finally:
        store.close()

