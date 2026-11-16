"""Load-parameter versioning and stale observation detection.

The load fingerprint covers exactly the parameters that change the generated
load or its pass/fail judgment: target address, ordered stage specs
(concurrency / duration / throughput limit / order) and SLO thresholds.
Sample interval and scene display name are excluded.

A version store (JSON) maps fingerprint -> monotonically increasing integer
version. A new fingerprint gets max(existing)+1. Existing stage observations
carry the version they were produced under; any version mismatch marks them
stale and excludes them from current judgment.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from .models import Scene


def fingerprint(scene: Scene) -> str:
    payload = {
        "target": {"host": scene.target.host, "port": scene.target.port},
        "stages": [
            {
                "index": s.index,
                "duration_s": s.duration_s,
                "concurrency": s.concurrency,
                "throughput_limit_rps": s.throughput_limit_rps,
            }
            for s in scene.stages
        ],
        "slo": {
            "min_success_rate_bp": scene.slo.min_success_rate_bp,
            "max_p95_latency_ms": scene.slo.max_p95_latency_ms,
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass
class VersionInfo:
    version: int
    fingerprint: str
    is_new: bool
    diff: list[str]


class VersionStore:
    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._data: dict = {"next_version": 1, "entries": {}}
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)

    def resolve(self, fp: str) -> VersionInfo:
        entries: dict = self._data["entries"]
        if fp in entries:
            return VersionInfo(entries[fp]["version"], fp, False, [])
        version = self._data["next_version"]
        entries[fp] = {"version": version, "fingerprint": fp}
        self._data["next_version"] = version + 1
        self._flush()
        return VersionInfo(version, fp, True, [])

    def _flush(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, sort_keys=True, indent=2)
        os.replace(tmp, self.path)

    def fingerprint_of(self, version: int) -> str | None:
        for entry in self._data["entries"].values():
            if entry["version"] == version:
                return entry["fingerprint"]
        return None


def diff_scenes(old: dict | None, new: Scene) -> list[str]:
    """Stable, human-readable list of load-parameter differences."""
    if not old:
        return ["initial_version(no_previous_scene)"]
    changes: list[str] = []
    old_target = old.get("target", {})
    if (old_target.get("host") != new.target.host
            or old_target.get("port") != new.target.port):
        changes.append(
            f"target:{old_target.get('host')}:{old_target.get('port')}->"
            f"{new.target.host}:{new.target.port}")
    old_stages = {s["index"]: s for s in old.get("stages", [])}
    new_indexes = [s.index for s in new.stages]
    old_indexes = sorted(old_stages)
    if old_indexes != new_indexes:
        changes.append(f"stage_order:{old_indexes}->{new_indexes}")
    for stage in new.stages:
        prev = old_stages.get(stage.index)
        if prev is None:
            changes.append(f"stage[{stage.index}]:added")
            continue
        for field_name in ("concurrency", "duration_s", "throughput_limit_rps"):
            if prev.get(field_name) != getattr(stage, field_name):
                changes.append(
                    f"stage[{stage.index}].{field_name}:"
                    f"{prev.get(field_name)}->{getattr(stage, field_name)}")
    for gone in sorted(set(old_indexes) - set(new_indexes)):
        changes.append(f"stage[{gone}]:removed")
    old_slo = old.get("slo", {})
    for field_name in ("min_success_rate_bp", "max_p95_latency_ms"):
        new_value = getattr(new.slo, field_name)
        if old_slo.get(field_name) != new_value:
            changes.append(
                f"slo.{field_name}:{old_slo.get(field_name)}->{new_value}")
    return changes
