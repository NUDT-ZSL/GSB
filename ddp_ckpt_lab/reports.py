"""Stable JSON report writing and human-readable summaries."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


def write_json_report(path: str | Path, payload: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".tmp-{target.name}-{uuid.uuid4().hex}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def summary_lines(summary: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    status = summary.get("status", "UNKNOWN")
    lines.append(f"DDPLAB status={status} version={summary.get('tool_version', '')}")
    config = summary.get("config", {})
    lines.append(
        "config "
        f"seed={config.get('seed')} data_seed={config.get('data_seed')} "
        f"samples={config.get('samples')} features={config.get('features')} "
        f"hidden={config.get('hidden_dim')} workers={config.get('workers')} "
        f"batch={config.get('batch_size')} lr={config.get('learning_rate')} "
        f"momentum={config.get('momentum')} sharding={config.get('sharding')}"
    )
    for scenario in summary.get("scenarios", []):
        lines.append(
            f"scenario {scenario.get('name')} status={scenario.get('status')}"
        )
        for detail in scenario.get("summary", []):
            lines.append(f"  {detail}")
    probes = summary.get("negative_probes", [])
    if probes:
        codes = ",".join(str(item.get("code")) for item in probes)
        lines.append(f"negative_business_codes={codes}")
    lines.append(f"summary_path={summary.get('summary_path', '')}")
    return lines
