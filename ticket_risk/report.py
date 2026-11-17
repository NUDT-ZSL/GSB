"""Diagnostic JSON report assembly and atomic output."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .state import RuntimeState
from .util import atomic_write_text


def build_report(
    state: RuntimeState,
    *,
    command_records: list[dict[str, Any]],
    last_snapshot_version: int,
    pending: dict[str, Any] | None = None,
    recovery_info: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    snapshot_error: str | None = None,
) -> dict[str, Any]:
    results = state.results()
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "state_version": state.version,
        "snapshot_version": last_snapshot_version,
        "config_hash": state.config_hash,
        "resource_fingerprint": state.resource_fingerprint,
        "base_config_hash": state.base_config_hash,
        "base_resource_fingerprint": state.base_resource_fingerprint,
        "samples_fingerprint": state.samples_fingerprint,
        "interaction_budget_ms": state.config.get("interaction_budget_ms"),
        "worker_threads": state.config.get("worker_threads"),
        "snapshot_commit_interval": state.config.get("snapshot_commit_interval"),
        "summary": state.summary(),
        "last_audit": copy.deepcopy(state.last_audit),
        "commands": copy.deepcopy(command_records),
        "pending": copy.deepcopy(pending),
        "recovery_info": copy.deepcopy(recovery_info),
        "verification": copy.deepcopy(verification),
        "snapshot_error": snapshot_error,
        "failures": [
            failure.to_dict()
            for failure in sorted(state.failures, key=lambda item: item.sort_key())
        ],
        "samples": results,
    }


def write_report(path: str | Path, report: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n",
    )
