from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from .canonical import read_json
from .selfcheck import run_selfcheck
from .store import Stage


def build_diagnostics(stage_dir: Path | str, *, version: int | None = None) -> dict[str, Any]:
    stage = Stage(stage_dir)
    manifest = stage.load_manifest(version) if version is not None else stage.current_manifest()
    if manifest is None:
        return {
            "schema_version": 1,
            "ok": False,
            "message": "no committed snapshot",
            "manifest": None,
            "recent_errors": stage.recent_errors(),
        }
    selfcheck_path = stage.root / f"selfcheck-v{manifest['version']:06d}.json"
    selfcheck = read_json(selfcheck_path) if selfcheck_path.exists() else None
    return {
        "schema_version": 1,
        "ok": True,
        "manifest": manifest,
        "topology": manifest["topology"],
        "nodes": manifest["nodes"],
        "invalidation": {
            "reused_nodes": manifest.get("reused_nodes", []),
            "recomputed_nodes": manifest.get("recomputed_nodes", []),
            "invalidated_nodes": manifest.get("invalidated_nodes", []),
            "affected_nodes": manifest.get("affected_nodes", []),
            "affected_pair_ids": manifest.get("affected_pair_ids", []),
            "raw_diff": manifest.get("raw_diff", {}),
            "causes": manifest.get("causes", []),
        },
        "conflicts": manifest.get("conflicts", []),
        "quota_targets": manifest.get("quota_targets", []),
        "underfilled_targets": manifest.get("underfilled_targets", []),
        "degradations": manifest.get("degradations", []),
        "recent_errors": stage.recent_errors(),
        "selfcheck": selfcheck,
    }


def ensure_selfcheck(stage_dir: Path | str, *, version: int | None = None) -> dict[str, Any]:
    return run_selfcheck(stage_dir, version=version, raise_on_error=False, persist=True)


def render_dashboard_html(data: dict[str, Any]) -> str:
    template = files("prefworkbench").joinpath("templates/dashboard.html").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
    payload = payload.replace("</", "<\\/")
    return template.replace("__BOOTSTRAP_JSON__", payload)


def write_dashboard(
    stage_dir: Path | str,
    output_path: Path | str,
    *,
    version: int | None = None,
) -> Path:
    data = build_diagnostics(stage_dir, version=version)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_dashboard_html(data), encoding="utf-8")
    return target
