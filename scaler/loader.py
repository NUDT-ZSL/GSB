from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import BusinessError, input_error
from .models import ScalerSpec


def _read_json(path: str | Path, label: str) -> Any:
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise input_error(
            f"cannot read {label} file: {exc}", path=str(file_path)
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise input_error(
            f"{label} file is not valid JSON: {exc.msg}",
            path=str(file_path),
            line=exc.lineno,
            column=exc.colno,
        ) from exc


def load_bundle(
    services_path: str | Path,
    nodes_path: str | Path,
    trace_path: str | Path,
    config_path: str | Path,
) -> ScalerSpec:
    services = _read_json(services_path, "services")
    nodes = _read_json(nodes_path, "nodes")
    trace = _read_json(trace_path, "trace")
    config = _read_json(config_path, "config")
    if isinstance(services, dict) and "services" in services:
        services = services["services"]
    if isinstance(nodes, dict) and "nodes" in nodes:
        nodes = nodes["nodes"]
    if isinstance(trace, dict) and ("windows" in trace or "services" in trace):
        pass
    elif isinstance(trace, list):
        trace = {"windows": trace}
    else:
        raise input_error("trace must contain a windows list or service series")
    if not isinstance(config, dict):
        raise input_error("config must be a JSON object")
    return ScalerSpec.from_dict(
        {
            "services": services,
            "nodes": nodes,
            "trace": trace,
            "config": config,
        }
    )


def load_spec_from_state(raw: dict[str, Any]) -> ScalerSpec:
    try:
        return ScalerSpec.from_dict(raw)
    except BusinessError:
        raise
    except Exception as exc:
        raise BusinessError(
            "E_STORAGE_ERROR", f"persisted specification is invalid: {exc}"
        ) from exc
