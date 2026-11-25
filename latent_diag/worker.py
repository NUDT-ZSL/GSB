from __future__ import annotations

import traceback
from typing import Any

from .arrayio import BlobStore
from .config import validate_config
from .errors import EngineError
from .pipeline import compute_task
from .task import TaskSpec


def run_worker(payload: dict[str, Any]) -> dict[str, Any]:
    task: TaskSpec | None = None
    try:
        task = TaskSpec.from_dict(payload["task"])
        config = validate_config(payload["config"])
        blobs = BlobStore(payload["cache_root"])
        result = compute_task(
            task,
            config,
            blobs=blobs,
            reuse=payload.get("reuse") or {},
        )
        result["task_index"] = int(payload["task_index"])
        return result
    except EngineError as exc:
        error = exc.to_dict()
    except BaseException as exc:  # pragma: no cover - defensive process boundary
        error = {
            "ok": False,
            "code": "E_TASK_EXECUTION",
            "message": f"{type(exc).__name__}: {exc}",
            "details": {"traceback": traceback.format_exc(limit=8)},
        }
    if task is not None:
        error.setdefault("task_id", task.task_id)
        error.setdefault("revision", task.revision)
        try:
            error.setdefault("input_hash", task.input_hash(validate_config(payload["config"])))
        except Exception:
            pass
    error["task_index"] = int(payload.get("task_index", -1))
    return error

