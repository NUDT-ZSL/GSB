"""Round snapshots: atomic persistence, resume and hash verification."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from .atomicio import atomic_write_json
from .errors import E_CONFIG_MISMATCH, E_SNAPSHOT_INVALID, PipelineError

SNAPSHOT_FILENAME = "snapshot.json"
FAILURE_FILENAME = "failures.json"
SNAPSHOT_VERSION = 1


@dataclass
class Snapshot:
    version: int
    round: int
    config_hash: str
    relation_hash: str
    injection_hash: str
    sample_cursor: list[str]
    score_summary: dict[str, dict[str, Any]]
    rounds: dict[str, list[dict]]
    failed_samples: list[str]
    injection_hits: list[dict]
    failures: list[dict]
    static_dangling: list[dict]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "version": self.version,
            "round": self.round,
            "config_hash": self.config_hash,
            "relation_hash": self.relation_hash,
            "injection_hash": self.injection_hash,
            "sample_cursor": self.sample_cursor,
            "score_summary": self.score_summary,
            "rounds": self.rounds,
            "failed_samples": self.failed_samples,
            "injection_hits": self.injection_hits,
            "failures": self.failures,
            "static_dangling": self.static_dangling,
        }


def snapshot_path(work_dir: str) -> str:
    return os.path.join(work_dir, SNAPSHOT_FILENAME)


def failure_path(work_dir: str) -> str:
    return os.path.join(work_dir, FAILURE_FILENAME)


def write_snapshot(work_dir: str, snapshot: Snapshot) -> None:
    atomic_write_json(snapshot_path(work_dir), snapshot.to_dict())


def write_failures(work_dir: str, failures: list[dict],
                   injection_hits: list[dict]) -> None:
    atomic_write_json(failure_path(work_dir), {
        "failures": failures,
        "injection_hits": injection_hits,
    })


def load_snapshot(work_dir: str, config_hash: str, relation_hash: str,
                  injection_hash: str) -> Snapshot | None:
    """Load the latest snapshot or ``None``; raise on hash mismatch."""
    path = snapshot_path(work_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise PipelineError(E_SNAPSHOT_INVALID, "snapshot",
                            f"snapshot JSON malformed: {exc}") from exc

    required = ("snapshot_version", "version", "round", "config_hash",
                "relation_hash", "injection_hash", "sample_cursor",
                "score_summary", "rounds", "failed_samples",
                "injection_hits", "failures")
    missing = [key for key in required if key not in raw]
    if missing:
        raise PipelineError(E_SNAPSHOT_INVALID, "snapshot",
                            f"snapshot missing fields: {missing}")
    if raw["snapshot_version"] != SNAPSHOT_VERSION:
        raise PipelineError(E_SNAPSHOT_INVALID, "snapshot",
                            "unsupported snapshot version: "
                            f"{raw['snapshot_version']}")

    mismatches = []
    if raw["config_hash"] != config_hash:
        mismatches.append("config_hash")
    if raw["relation_hash"] != relation_hash:
        mismatches.append("relation_hash")
    if raw["injection_hash"] != injection_hash:
        mismatches.append("injection_hash")
    if mismatches:
        raise PipelineError(
            E_CONFIG_MISMATCH, "snapshot",
            "snapshot does not match current inputs: "
            + ",".join(mismatches))

    return Snapshot(
        version=raw["version"],
        round=raw["round"],
        config_hash=raw["config_hash"],
        relation_hash=raw["relation_hash"],
        injection_hash=raw["injection_hash"],
        sample_cursor=list(raw["sample_cursor"]),
        score_summary={key: dict(value)
                       for key, value in raw["score_summary"].items()},
        rounds={key: list(value) for key, value in raw["rounds"].items()},
        failed_samples=list(raw["failed_samples"]),
        injection_hits=list(raw["injection_hits"]),
        failures=list(raw["failures"]),
        static_dangling=list(raw["static_dangling"]),
    )
